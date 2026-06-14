"""
Data loading, caching, and episode payload building for LeRobot datasets.

This module has no Flask dependency. It provides pure data access logic
operating on LeRobotDataset / IterableNamespace objects.
"""

import logging
from io import StringIO
from threading import Lock
from collections import OrderedDict

import numpy as np
import pandas as pd
import requests
from lerobot.datasets.utils import IterableNamespace
from lerobot.datasets.lerobot_dataset import LeRobotDataset

from scribe.lance_backend import LanceDataset

# Anywhere the code used to do `isinstance(x, LeRobotDataset)` to mean
# "local dataset with .root / .hf_dataset / .episode_data_index / .meta"
# we now accept LanceDataset too.
LOCAL_DATASET_TYPES = (LeRobotDataset, LanceDataset)

# ---------------------------------------------------------------------------
# Video display order
# ---------------------------------------------------------------------------

VIDEO_DISPLAY_ORDER = [
    "left_wrist",
    "cam_env",
    "env",
    "right_wrist",
]


def sort_videos_by_order(videos_info: list[dict], order: list[str] = VIDEO_DISPLAY_ORDER) -> list[dict]:
    """Sort videos_info list according to VIDEO_DISPLAY_ORDER patterns."""

    def get_sort_key(video: dict) -> tuple:
        filename = video.get("filename", "")
        for idx, pattern in enumerate(order):
            if pattern in filename:
                return (idx, filename)
        return (len(order), filename)

    return sorted(videos_info, key=get_sort_key)


# ---------------------------------------------------------------------------
# Repo-id helpers
# ---------------------------------------------------------------------------


def split_repo_id(repo_id: str) -> tuple[str, str]:
    """Split repo_id into (namespace, name), tolerating single-segment ids."""
    cleaned = repo_id.strip().strip("/")
    if not cleaned:
        return "local", "dataset"

    if "/" in cleaned:
        namespace, name = cleaned.split("/", 1)
        if namespace and name:
            return namespace, name

    return "local", cleaned


# ---------------------------------------------------------------------------
# Episode data / timestamp LRU cache
# ---------------------------------------------------------------------------

EPISODE_DATA_CACHE_MAXSIZE = 16
_episode_data_cache: OrderedDict[tuple[str, int], tuple[str, list[dict], list[str]]] = OrderedDict()
_episode_data_cache_lock = Lock()
_episode_timestamp_cache: OrderedDict[tuple[str, int], np.ndarray] = OrderedDict()
_episode_timestamp_cache_lock = Lock()


def _get_dataset_cache_key(dataset: LeRobotDataset | IterableNamespace) -> str:
    repo_id = getattr(dataset, "repo_id", "")
    if isinstance(dataset, LOCAL_DATASET_TYPES):
        # LanceDataset.root may be a bos:// / s3:// URI string, so we route
        # through `root_id` (a portable str) instead of assuming a Path.
        root_id = getattr(dataset, "root_id", None)
        if root_id is None:
            root_id = str(dataset.root.resolve())  # LeRobotDataset (always local Path)
        return f"local:{root_id}:{repo_id}"
    return f"hub:{repo_id}"


def _get_cached_episode_data(cache_key: tuple[str, int]):
    with _episode_data_cache_lock:
        cached = _episode_data_cache.get(cache_key)
        if cached is None:
            return None
        _episode_data_cache.move_to_end(cache_key)
        return cached


def _set_cached_episode_data(
    cache_key: tuple[str, int],
    value: tuple[str, list[dict], list[str]],
):
    with _episode_data_cache_lock:
        _episode_data_cache[cache_key] = value
        _episode_data_cache.move_to_end(cache_key)
        while len(_episode_data_cache) > EPISODE_DATA_CACHE_MAXSIZE:
            _episode_data_cache.popitem(last=False)


def _get_cached_episode_timestamps(cache_key: tuple[str, int]) -> np.ndarray | None:
    with _episode_timestamp_cache_lock:
        cached = _episode_timestamp_cache.get(cache_key)
        if cached is None:
            return None
        _episode_timestamp_cache.move_to_end(cache_key)
        return cached


def _set_cached_episode_timestamps(cache_key: tuple[str, int], value: np.ndarray) -> None:
    with _episode_timestamp_cache_lock:
        _episode_timestamp_cache[cache_key] = value
        _episode_timestamp_cache.move_to_end(cache_key)
        while len(_episode_timestamp_cache) > EPISODE_DATA_CACHE_MAXSIZE:
            _episode_timestamp_cache.popitem(last=False)


# ---------------------------------------------------------------------------
# Array helpers
# ---------------------------------------------------------------------------


def _as_2d_array(values) -> np.ndarray:
    """Convert values to a 2D numpy array while preserving dtype."""
    arr = np.asarray(values)

    if arr.ndim == 1 and arr.dtype == object:
        arr = np.vstack(arr)
    elif arr.ndim == 1:
        arr = np.expand_dims(arr, axis=1)

    if arr.ndim != 2:
        raise ValueError(f"Expected a 2D array-like input, got shape={arr.shape}")

    return arr


# ---------------------------------------------------------------------------
# Core data accessors
# ---------------------------------------------------------------------------


def get_ep_csv_fname(episode_id: int):
    return f"episode_{episode_id}.csv"


def get_episode_data(dataset: LeRobotDataset | IterableNamespace, episode_index):
    """Get a csv str containing timeseries data of an episode (e.g. state and action).
    This file will be loaded by Dygraph javascript to plot data in real time."""
    cache_key = (_get_dataset_cache_key(dataset), int(episode_index))
    cached = _get_cached_episode_data(cache_key)
    if cached is not None:
        logging.debug("episode_data_cache hit dataset=%s episode=%s", cache_key[0], cache_key[1])
        return cached

    logging.debug("episode_data_cache miss dataset=%s episode=%s", cache_key[0], cache_key[1])

    columns = []

    numeric_columns = [col for col, ft in dataset.features.items() if ft["dtype"] in ["float32", "int32"]]
    selected_columns = [col for col in numeric_columns if col != "timestamp"]

    ignored_columns = []
    filtered_selected_columns = []
    for column_name in selected_columns:
        shape = dataset.features[column_name]["shape"]
        shape_dim = len(shape)
        if shape_dim > 1:
            ignored_columns.append(column_name)
            continue
        filtered_selected_columns.append(column_name)

    selected_columns = filtered_selected_columns

    # init header of csv with state and action names
    header = ["timestamp"]
    used_header_names = {"timestamp"}

    for column_name in selected_columns:
        dim_state = (
            dataset.meta.shapes[column_name][0]
            if isinstance(dataset, LOCAL_DATASET_TYPES)
            else dataset.features[column_name].shape[0]
        )

        if "names" in dataset.features[column_name] and dataset.features[column_name]["names"]:
            column_names = dataset.features[column_name]["names"]
            while not isinstance(column_names, list):
                column_names = list(column_names.values())[0]
        else:
            column_names = [f"{column_name}_{i}" for i in range(dim_state)]
        columns.append({"key": column_name, "value": column_names})

        unique_header_names = []
        for dim_name in column_names:
            candidate = dim_name
            if candidate in used_header_names:
                candidate = f"{column_name}.{dim_name}"
            suffix_index = 2
            while candidate in used_header_names:
                candidate = f"{column_name}.{dim_name}_{suffix_index}"
                suffix_index += 1
            unique_header_names.append(candidate)
            used_header_names.add(candidate)

        header += unique_header_names

    requested_columns = ["timestamp", *selected_columns]

    if isinstance(dataset, LOCAL_DATASET_TYPES):
        from_idx = dataset.episode_data_index["from"][episode_index]
        to_idx = dataset.episode_data_index["to"][episode_index]
        data = (
            dataset.hf_dataset.select(range(from_idx, to_idx)).select_columns(requested_columns).with_format("numpy")
        )[:]
    else:
        repo_id = dataset.repo_id

        url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/" + dataset.data_path.format(
            episode_chunk=int(episode_index) // dataset.chunks_size, episode_index=episode_index
        )
        df = pd.read_parquet(url, columns=requested_columns)
        data = {column_name: df[column_name].to_numpy(copy=False) for column_name in requested_columns}

    blocks = [_as_2d_array(data["timestamp"])]
    blocks.extend(_as_2d_array(data[column_name]) for column_name in selected_columns)
    matrix = np.hstack(blocks)

    csv_buffer = StringIO()
    csv_buffer.write(",".join(header))
    csv_buffer.write("\n")
    csv_float_format = "%.9g" if isinstance(dataset, LanceDataset) else "%.17g"
    np.savetxt(csv_buffer, matrix, delimiter=",", fmt=csv_float_format)
    csv_string = csv_buffer.getvalue()

    result = (csv_string, columns, ignored_columns)
    _set_cached_episode_data(cache_key, result)

    return result


def get_episode_timestamps(dataset: LeRobotDataset | IterableNamespace, episode_index: int) -> np.ndarray:
    cache_key = (_get_dataset_cache_key(dataset), int(episode_index))
    cached = _get_cached_episode_timestamps(cache_key)
    if cached is not None:
        return cached

    if isinstance(dataset, LOCAL_DATASET_TYPES):
        from_idx = dataset.episode_data_index["from"][episode_index]
        to_idx = dataset.episode_data_index["to"][episode_index]
        data = (dataset.hf_dataset.select(range(from_idx, to_idx)).select_columns(["timestamp"]).with_format("numpy"))[
            :
        ]
        timestamps = np.asarray(data["timestamp"], dtype=np.float64).reshape(-1)
    else:
        repo_id = dataset.repo_id
        url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/" + dataset.data_path.format(
            episode_chunk=int(episode_index) // dataset.chunks_size,
            episode_index=episode_index,
        )
        df = pd.read_parquet(url, columns=["timestamp"])
        timestamps = df["timestamp"].to_numpy(dtype=np.float64, copy=False)

    _set_cached_episode_timestamps(cache_key, timestamps)
    return timestamps


def get_episode_frame_count(dataset: LeRobotDataset | IterableNamespace, episode_index: int) -> int:
    if isinstance(dataset, LOCAL_DATASET_TYPES):
        episode_meta = dataset.meta.episodes[episode_index]
        length = episode_meta.get("length")
        if isinstance(length, int):
            return length
    return int(len(get_episode_timestamps(dataset, episode_index)))


def get_episode_video_paths(dataset: LeRobotDataset, ep_index: int) -> list[str]:
    first_frame_idx = dataset.episode_data_index["from"][ep_index].item()
    return [dataset.hf_dataset.select_columns(key)[first_frame_idx][key]["path"] for key in dataset.meta.video_keys]


def get_episode_language_instruction(dataset: LeRobotDataset, ep_index: int) -> list[str]:
    if "language_instruction" not in dataset.features:
        return None

    first_frame_idx = dataset.episode_data_index["from"][ep_index].item()
    language_instruction = dataset.hf_dataset[first_frame_idx]["language_instruction"]
    return language_instruction.removeprefix("tf.Tensor(b'").removesuffix("', shape=(), dtype=string)")


def get_dataset_info(repo_id: str) -> IterableNamespace:
    response = requests.get(f"https://huggingface.co/datasets/{repo_id}/resolve/main/meta/info.json", timeout=5)
    response.raise_for_status()
    dataset_info = response.json()
    dataset_info["repo_id"] = repo_id
    return IterableNamespace(dataset_info)
