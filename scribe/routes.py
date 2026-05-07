"""
Flask route handlers for the Scribe visualizer.

Contains run_server() which creates the Flask app, registers all routes,
and starts the development server.
"""

import os
import re
import json
import logging
import threading
from pathlib import Path
from datetime import datetime, timezone
from threading import Lock

import requests
from flask import Flask, abort, jsonify, request, url_for, redirect, render_template, send_from_directory
from lerobot.datasets.utils import IterableNamespace
from lerobot.datasets.lerobot_dataset import LeRobotDataset

from scribe.data import (
    LOCAL_DATASET_TYPES,
    split_repo_id,
    get_dataset_info,
    get_episode_data,
    sort_videos_by_order,
    get_episode_timestamps,
    get_episode_frame_count,
)
from scribe.lance_backend import LanceDataset
from scribe.annotation_store import (
    DEFAULT_SCHEME,
    SUPPORTED_SCHEMES,
    infer_color,
    utc_now_iso,
    get_scheme_config,
    new_annotation_id,
    normalize_segment,
    infer_display_label,
    resolve_task_profile,
    normalize_frame_event,
    load_frame_event_records,
    save_frame_event_records,
    build_task_context_payload,
    summarize_episode_segments,
    load_episode_curation_store,
    save_episode_curation_store,
    ensure_task_annotation_store,
    load_segment_annotation_store,
    save_segment_annotation_store,
    build_local_annotation_context,
    build_segment_summary_by_episode,
)

# ---------------------------------------------------------------------------
# Homepage datasets
# ---------------------------------------------------------------------------

FEATURED_DATASETS = [
    "lerobot/aloha_static_cups_open",
    "lerobot/columbia_cairlab_pusht_real",
    "lerobot/taco_play",
]

try:
    from lerobot import available_datasets
except ImportError:
    available_datasets = FEATURED_DATASETS

# ---------------------------------------------------------------------------
# Annotation locks
# ---------------------------------------------------------------------------

EPISODE_CURATION_SUPPORTED_DECISIONS = {"keep", "delete_candidate"}
_episode_curation_lock = Lock()
_segment_annotation_lock = Lock()
_frame_event_lock = Lock()
_task_annotation_lock = Lock()


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "t", "yes", "y", "on"}


# ---------------------------------------------------------------------------
# Curation / annotation context helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_episode_curation_context(
    dataset_obj: LeRobotDataset | IterableNamespace,
    repo_id: str,
) -> dict:
    annotation_context = build_local_annotation_context(dataset_obj, repo_id)
    return {
        "enabled": bool(annotation_context.get("enabled")),
        "dataset_id": annotation_context.get("dataset_id"),
        "annotations_dir": annotation_context.get("annotations_dir"),
        "storage_file": annotation_context.get("files", {}).get("episode_curation"),
    }


def _default_episode_curation_store(dataset_id: str) -> dict:
    now_iso = _utc_now_iso()
    return {
        "version": 1,
        "dataset_id": dataset_id,
        "updated_at": now_iso,
        "items": {},
    }


def _load_episode_curation_store(storage_path: Path, dataset_id: str) -> dict:
    try:
        return load_episode_curation_store(storage_path, dataset_id)
    except Exception as error:
        logging.warning("failed to read episode curation file %s: %s", storage_path, error)
        return _default_episode_curation_store(dataset_id)


def _save_episode_curation_store(storage_path: Path, store: dict) -> None:
    save_episode_curation_store(storage_path, store)


def _build_curation_response_context(curation_context: dict) -> dict:
    return {
        "enabled": bool(curation_context.get("enabled")),
        "dataset_id": curation_context.get("dataset_id"),
        "annotations_dir": curation_context.get("annotations_dir"),
    }


def _compute_action_source_track(dataset_obj, episode_id: int) -> dict | None:
    """Resolve per-episode HIL `action_source` spans across both backends.

    Returns ``None`` when the dataset has no `action_source` column (legacy
    pre-HIL data); the frontend hides the track entirely in that case.

    For Lance datasets we delegate to `LanceDataset.get_episode_action_source_track`
    which reads only the one column (its own column-presence check is reliable —
    no broad except needed). For LeRobotDataset we read the parquet via the
    hf_dataset shim (only when the v2.1 export carried `action_source`,
    which `training_lance_to_lerobot.py` does conditionally).
    """
    if isinstance(dataset_obj, LanceDataset):
        return dataset_obj.get_episode_action_source_track(episode_id)

    # LeRobotDataset path — feature presence is the explicit gate.
    features = getattr(dataset_obj, "features", {}) or {}
    if "action_source" not in features:
        return None
    # Narrow except: we only swallow the well-known shapes that mean
    # "feature is declared but not actually readable" (stale info.json,
    # column dropped after select_columns, etc.). Real bugs (IndexError,
    # NotImplementedError, IO errors, etc.) propagate as a 500 so they're
    # caught in development instead of silently disabling the track.
    try:
        index = dataset_obj.episode_data_index
        ep_from = int(index["from"][episode_id])
        ep_to = int(index["to"][episode_id])
        rows = (
            dataset_obj.hf_dataset
            .select(range(ep_from, ep_to))
            .select_columns(["action_source"])
            .with_format("python")
        )
        values = [str(r["action_source"]) for r in rows]
    except (KeyError, AttributeError) as e:
        logging.warning(
            "action_source declared in features but not readable for ep=%s: %s",
            episode_id, e,
        )
        return None

    from scribe.lance_backend import _aggregate_action_source_spans
    return _aggregate_action_source_spans(values)


def _build_annotation_response_context(
    dataset_obj: LeRobotDataset | IterableNamespace,
    repo_id: str,
    language_instruction: str | list[str] | None,
) -> dict:
    annotation_context = build_local_annotation_context(dataset_obj, repo_id)
    if not annotation_context.get("enabled"):
        return {
            "enabled": False,
            "dataset_id": annotation_context.get("dataset_id"),
            "annotations_dir": None,
            "task_name": None,
            "task_display_name": None,
            "scheme": DEFAULT_SCHEME,
            "allow_custom_labels": False,
            "stage_order": [],
            "stage_catalog": [],
            "files": annotation_context.get("files", {}),
        }

    task_config_path = Path(annotation_context["files"]["task_config"])
    with _task_annotation_lock:
        task_store = ensure_task_annotation_store(task_config_path, annotation_context["dataset_id"])

    if isinstance(language_instruction, list):
        normalized_instruction = " ".join(str(item) for item in language_instruction if item)
    else:
        normalized_instruction = str(language_instruction or "")

    task_name, task_profile = resolve_task_profile(task_store, normalized_instruction)
    return build_task_context_payload(
        annotation_context, task_name, task_profile, scheme=DEFAULT_SCHEME,
        event_types=task_store.get("event_types") or [],
    )


# ---------------------------------------------------------------------------
# Route-level helpers (extracted from run_server closure)
# ---------------------------------------------------------------------------


def _resolve_dataset_or_error(repo_id: str, dataset_obj):
    try:
        resolved_dataset = dataset_obj if dataset_obj is not None else get_dataset_info(repo_id)
    except FileNotFoundError:
        return (
            "Make sure to convert your LeRobotDataset to v2 & above. See how to convert your dataset at https://github.com/huggingface/lerobot/pull/461",
            400,
        )

    dataset_version = (
        str(resolved_dataset.meta._version)
        if isinstance(resolved_dataset, LOCAL_DATASET_TYPES)
        else resolved_dataset.codebase_version
    )
    match = re.search(r"v(\d+)\.", dataset_version)
    if match:
        major_version = int(match.group(1))
        if major_version < 2:
            return "Make sure to convert your LeRobotDataset to v2 & above."

    return resolved_dataset


def _build_stage_order_by_task(task_store: dict) -> dict[str, list[str]]:
    tasks = task_store.get("tasks", {}) if isinstance(task_store, dict) else {}
    stage_order_by_task: dict[str, list[str]] = {}
    for task_name, task_profile in tasks.items():
        scheme_config = get_scheme_config(task_profile, DEFAULT_SCHEME)
        stage_order_by_task[task_name] = list(scheme_config.get("stage_order", []))
    return stage_order_by_task


def _load_task_store_or_error(resolved_dataset, repo_id: str):
    annotation_context = build_local_annotation_context(resolved_dataset, repo_id)
    if not annotation_context["enabled"]:
        return None, None, (jsonify({"error": "annotations only support local datasets loaded with --root"}), 400)

    task_config_path = Path(annotation_context["files"]["task_config"])
    with _task_annotation_lock:
        task_store = ensure_task_annotation_store(task_config_path, annotation_context["dataset_id"])
    return annotation_context, task_store, None


# ---------------------------------------------------------------------------
# run_server — creates Flask app, registers routes, starts server
# ---------------------------------------------------------------------------


def run_server(
    dataset: LeRobotDataset | IterableNamespace | None,
    episodes: list[int] | None,
    host: str,
    port: str,
    static_folder: Path,
    template_folder: Path,
    robot_kinematic_config: dict,
    vendor_assets: dict,
):
    app = Flask(__name__, static_folder=static_folder.resolve(), template_folder=template_folder.resolve())
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

    dataset_video_root = None
    if isinstance(dataset, LOCAL_DATASET_TYPES):
        # LanceDataset carries its own video_root (runtime MP4 materialization).
        candidate = getattr(dataset, "video_root", None) or (dataset.root / "videos")
        if candidate.exists():
            dataset_video_root = candidate.resolve()

    @app.route("/local_videos/<path:video_rel_path>")
    def local_video_file(video_rel_path: str):
        if dataset_video_root is None:
            abort(404)
        return send_from_directory(dataset_video_root.as_posix(), video_rel_path, conditional=True)

    @app.route("/")
    def hommepage(dataset=dataset):
        if dataset:
            dataset_namespace, dataset_name = split_repo_id(dataset.repo_id)
            return redirect(
                url_for(
                    "show_episode",
                    dataset_namespace=dataset_namespace,
                    dataset_name=dataset_name,
                    episode_id=0,
                )
            )

        dataset_param, episode_param = None, None
        all_params = request.args
        if "dataset" in all_params:
            dataset_param = all_params["dataset"]
        if "episode" in all_params:
            episode_param = int(all_params["episode"])

        if dataset_param:
            dataset_namespace, dataset_name = split_repo_id(dataset_param)
            return redirect(
                url_for(
                    "show_episode",
                    dataset_namespace=dataset_namespace,
                    dataset_name=dataset_name,
                    episode_id=episode_param if episode_param is not None else 0,
                )
            )

        return render_template(
            "visualize_dataset_homepage.html",
            featured_datasets=FEATURED_DATASETS,
            lerobot_datasets=available_datasets,
        )

    @app.route("/<string:dataset_namespace>/<string:dataset_name>")
    def show_first_episode(dataset_namespace, dataset_name):
        first_episode_id = 0
        return redirect(
            url_for(
                "show_episode",
                dataset_namespace=dataset_namespace,
                dataset_name=dataset_name,
                episode_id=first_episode_id,
            )
        )

    def _build_episode_payload(
        dataset_obj: LeRobotDataset | IterableNamespace,
        repo_id: str,
        episode_id: int,
        episodes_value: list[int] | None,
    ) -> dict:
        curation_context = _build_episode_curation_context(dataset_obj, repo_id)
        current_preload_thread = None
        current_preload_errors: list[Exception] = []
        if isinstance(dataset_obj, LanceDataset):
            # Start materialization while building the CSV/metadata payload.
            def _preload_current_episode() -> None:
                try:
                    dataset_obj._preload_videos(episode_id)
                except Exception as error:
                    current_preload_errors.append(error)

            current_preload_thread = threading.Thread(
                target=_preload_current_episode,
                daemon=True,
            )
            current_preload_thread.start()
        episode_data_csv_str, columns, ignored_columns = get_episode_data(dataset_obj, episode_id)
        frame_count = get_episode_frame_count(dataset_obj, episode_id)

        dataset_info = {
            "repo_id": repo_id,
            "num_samples": dataset_obj.num_frames
            if isinstance(dataset_obj, LOCAL_DATASET_TYPES)
            else dataset_obj.total_frames,
            "num_episodes": dataset_obj.num_episodes
            if isinstance(dataset_obj, LOCAL_DATASET_TYPES)
            else dataset_obj.total_episodes,
            "fps": dataset_obj.fps,
        }

        if isinstance(dataset_obj, LOCAL_DATASET_TYPES):
            if current_preload_thread is not None:
                current_preload_thread.join()
                if current_preload_errors:
                    raise RuntimeError(
                        f"failed to materialize videos for episode {episode_id}"
                    ) from current_preload_errors[0]
                next_ep = episode_id + 1
                if _env_bool("LANCE_PRELOAD_NEXT", True) and next_ep < dataset_obj.num_episodes:

                    def _preload_next_episode() -> None:
                        try:
                            dataset_obj._preload_videos(next_ep)
                        except Exception:
                            logging.warning("background video materialization failed ep=%d", next_ep, exc_info=True)

                    threading.Thread(
                        target=_preload_next_episode,
                        daemon=True,
                    ).start()
            video_paths = [dataset_obj.meta.get_video_file_path(episode_id, key) for key in dataset_obj.meta.video_keys]
            videos_info = []
            for video_path in video_paths:
                normalized_video_path = Path(video_path)
                if normalized_video_path.is_absolute() and dataset_video_root is not None:
                    try:
                        normalized_video_path = normalized_video_path.relative_to(dataset_video_root)
                    except ValueError:
                        normalized_video_path = Path(video_path.name)

                video_rel_path = normalized_video_path.as_posix().replace("\\", "/")
                if video_rel_path.startswith("videos/"):
                    video_rel_path = video_rel_path[len("videos/") :]

                videos_info.append(
                    {
                        "url": url_for("local_video_file", video_rel_path=video_rel_path),
                        "filename": video_path.parent.name,
                    }
                )
            tasks = dataset_obj.meta.episodes[episode_id]["tasks"]
            video_seek_info = (
                dataset_obj.get_episode_video_seek_info(episode_id) if isinstance(dataset_obj, LanceDataset) else {}
            )
        else:
            video_keys = [key for key, ft in dataset_obj.features.items() if ft["dtype"] == "video"]
            videos_info = [
                {
                    "url": f"https://huggingface.co/datasets/{repo_id}/resolve/main/"
                    + dataset_obj.video_path.format(
                        episode_chunk=int(episode_id) // dataset_obj.chunks_size,
                        video_key=video_key,
                        episode_index=episode_id,
                    ),
                    "filename": video_key,
                }
                for video_key in video_keys
            ]

            response = requests.get(
                f"https://huggingface.co/datasets/{repo_id}/resolve/main/meta/episodes.jsonl", timeout=5
            )
            response.raise_for_status()
            tasks_jsonl = [json.loads(line) for line in response.text.splitlines() if line.strip()]
            filtered_tasks_jsonl = [row for row in tasks_jsonl if row["episode_index"] == episode_id]
            tasks = filtered_tasks_jsonl[0]["tasks"]
            video_seek_info = {}

        videos_info = sort_videos_by_order(videos_info)

        language_instruction = tasks
        if videos_info:
            videos_info[0]["language_instruction"] = tasks

        annotation_context = _build_annotation_response_context(dataset_obj, repo_id, language_instruction)

        if episodes_value is None:
            episodes_value = list(
                range(
                    dataset_obj.num_episodes
                    if isinstance(dataset_obj, LOCAL_DATASET_TYPES)
                    else dataset_obj.total_episodes
                )
            )

        action_source_track = _compute_action_source_track(dataset_obj, episode_id)

        return {
            "episode_id": episode_id,
            "episodes": episodes_value,
            "dataset_info": dataset_info,
            "videos_info": videos_info,
            "episode_data_csv_str": episode_data_csv_str,
            "columns": columns,
            "ignored_columns": ignored_columns,
            "video_seek_info": video_seek_info,
            "language_instruction": language_instruction,
            "curation_context": _build_curation_response_context(curation_context),
            "annotation_context": annotation_context,
            "action_source_track": action_source_track,
            "frame_count": frame_count,
        }

    @app.route("/<string:dataset_namespace>/<string:dataset_name>/episode_<int:episode_id>")
    def show_episode(
        dataset_namespace,
        dataset_name,
        episode_id,
        dataset=dataset,
        episodes=episodes,
        robot_kinematic_config=robot_kinematic_config,
        vendor_assets=vendor_assets,
    ):
        repo_id = f"{dataset_namespace}/{dataset_name}"
        resolved_dataset = _resolve_dataset_or_error(repo_id, dataset)
        if isinstance(resolved_dataset, tuple) or isinstance(resolved_dataset, str):
            return resolved_dataset

        payload = _build_episode_payload(resolved_dataset, repo_id, episode_id, episodes)

        return render_template(
            "visualize.html",
            episode_id=payload["episode_id"],
            episodes=payload["episodes"],
            dataset_info=payload["dataset_info"],
            videos_info=payload["videos_info"],
            episode_data_csv_str=payload["episode_data_csv_str"],
            columns=payload["columns"],
            ignored_columns=payload["ignored_columns"],
            episode_payload=payload,
            robot_kinematic_config=robot_kinematic_config,
            static_url_prefix="/static/",
            vendor_assets=vendor_assets,
            curation_context=payload["curation_context"],
            annotation_context=payload["annotation_context"],
            action_source_track=payload["action_source_track"],
        )

    @app.route("/<string:dataset_namespace>/<string:dataset_name>/episode_<int:episode_id>.json")
    def show_episode_json(dataset_namespace, dataset_name, episode_id, dataset=dataset, episodes=episodes):
        repo_id = f"{dataset_namespace}/{dataset_name}"
        resolved_dataset = _resolve_dataset_or_error(repo_id, dataset)
        if isinstance(resolved_dataset, tuple):
            message, code = resolved_dataset
            return jsonify({"error": message}), code
        if isinstance(resolved_dataset, str):
            return jsonify({"error": resolved_dataset}), 400

        payload = _build_episode_payload(resolved_dataset, repo_id, episode_id, episodes)
        return jsonify(payload)

    @app.route(
        "/<string:dataset_namespace>/<string:dataset_name>/api/episode-curation",
        methods=["GET"],
    )
    def get_episode_curation(dataset_namespace, dataset_name, dataset=dataset):
        repo_id = f"{dataset_namespace}/{dataset_name}"
        resolved_dataset = _resolve_dataset_or_error(repo_id, dataset)
        if isinstance(resolved_dataset, tuple):
            message, code = resolved_dataset
            return jsonify({"error": message}), code
        if isinstance(resolved_dataset, str):
            return jsonify({"error": resolved_dataset}), 400

        curation_context = _build_episode_curation_context(resolved_dataset, repo_id)
        if not curation_context["enabled"]:
            return jsonify({"error": "episode curation only supports local datasets loaded with --root"}), 400

        storage_path = Path(curation_context["storage_file"])
        with _episode_curation_lock:
            store = _load_episode_curation_store(storage_path, curation_context["dataset_id"])

        episode_index_raw = request.args.get("episode_index")
        if episode_index_raw is not None:
            try:
                episode_index = int(episode_index_raw)
            except ValueError:
                return jsonify({"error": "episode_index must be an integer"}), 400

            record = store["items"].get(str(episode_index))
            return jsonify(
                {
                    "curation_context": _build_curation_response_context(curation_context),
                    "episode_index": episode_index,
                    "record": record,
                }
            )

        return jsonify(
            {
                "curation_context": _build_curation_response_context(curation_context),
                "items": store["items"],
                "updated_at": store.get("updated_at"),
            }
        )

    @app.route(
        "/<string:dataset_namespace>/<string:dataset_name>/api/episode-curation",
        methods=["POST"],
    )
    def upsert_episode_curation(dataset_namespace, dataset_name, dataset=dataset):
        repo_id = f"{dataset_namespace}/{dataset_name}"
        resolved_dataset = _resolve_dataset_or_error(repo_id, dataset)
        if isinstance(resolved_dataset, tuple):
            message, code = resolved_dataset
            return jsonify({"error": message}), code
        if isinstance(resolved_dataset, str):
            return jsonify({"error": resolved_dataset}), 400

        curation_context = _build_episode_curation_context(resolved_dataset, repo_id)
        if not curation_context["enabled"]:
            return jsonify({"error": "episode curation only supports local datasets loaded with --root"}), 400

        payload = request.get_json(silent=True) or {}
        try:
            episode_index = int(payload.get("episode_index"))
        except (TypeError, ValueError):
            return jsonify({"error": "episode_index is required and must be an integer"}), 400

        decision = str(payload.get("decision") or "").strip()
        if decision not in EPISODE_CURATION_SUPPORTED_DECISIONS:
            return jsonify(
                {"error": ("decision must be one of: " + ", ".join(sorted(EPISODE_CURATION_SUPPORTED_DECISIONS)))}
            ), 400

        delete_reason = str(payload.get("delete_reason") or "").strip()
        if decision != "delete_candidate":
            delete_reason = ""

        operator = str(request.headers.get("X-Operator") or payload.get("operator") or "anonymous").strip()
        if not operator:
            operator = "anonymous"

        now_iso = _utc_now_iso()
        record = {
            "episode_index": episode_index,
            "decision": decision,
            "delete_reason": delete_reason,
            "operator": operator,
            "updated_at": now_iso,
        }

        storage_path = Path(curation_context["storage_file"])
        with _episode_curation_lock:
            store = _load_episode_curation_store(storage_path, curation_context["dataset_id"])
            store["items"][str(episode_index)] = record
            store["updated_at"] = now_iso
            _save_episode_curation_store(storage_path, store)

        return jsonify(
            {
                "ok": True,
                "curation_context": _build_curation_response_context(curation_context),
                "record": record,
            }
        )

    @app.route(
        "/<string:dataset_namespace>/<string:dataset_name>/api/episode-curation/export-delete-list",
        methods=["GET"],
    )
    def export_episode_delete_candidates(dataset_namespace, dataset_name, dataset=dataset):
        repo_id = f"{dataset_namespace}/{dataset_name}"
        resolved_dataset = _resolve_dataset_or_error(repo_id, dataset)
        if isinstance(resolved_dataset, tuple):
            message, code = resolved_dataset
            return jsonify({"error": message}), code
        if isinstance(resolved_dataset, str):
            return jsonify({"error": resolved_dataset}), 400

        curation_context = _build_episode_curation_context(resolved_dataset, repo_id)
        if not curation_context["enabled"]:
            return jsonify({"error": "episode curation only supports local datasets loaded with --root"}), 400

        storage_path = Path(curation_context["storage_file"])
        with _episode_curation_lock:
            store = _load_episode_curation_store(storage_path, curation_context["dataset_id"])

        delete_candidates = [
            record
            for _, record in sorted(
                store["items"].items(),
                key=lambda item: int(item[0]),
            )
            if record.get("decision") == "delete_candidate"
        ]

        return jsonify(
            {
                "curation_context": _build_curation_response_context(curation_context),
                "exported_at": _utc_now_iso(),
                "total_delete_candidates": len(delete_candidates),
                "delete_candidates": delete_candidates,
            }
        )

    @app.route(
        "/<string:dataset_namespace>/<string:dataset_name>/api/task-annotation-config",
        methods=["GET"],
    )
    def get_task_annotation_config(dataset_namespace, dataset_name, dataset=dataset):
        repo_id = f"{dataset_namespace}/{dataset_name}"
        resolved_dataset = _resolve_dataset_or_error(repo_id, dataset)
        if isinstance(resolved_dataset, tuple):
            message, code = resolved_dataset
            return jsonify({"error": message}), code
        if isinstance(resolved_dataset, str):
            return jsonify({"error": resolved_dataset}), 400

        annotation_context, task_store, error_response = _load_task_store_or_error(resolved_dataset, repo_id)
        if error_response is not None:
            return error_response

        return jsonify(
            {
                "annotation_context": {
                    "enabled": True,
                    "dataset_id": annotation_context["dataset_id"],
                    "annotations_dir": annotation_context["annotations_dir"],
                    "files": annotation_context["files"],
                },
                "task_store": task_store,
            }
        )

    @app.route(
        "/<string:dataset_namespace>/<string:dataset_name>/api/segment-annotations",
        methods=["GET"],
    )
    def get_segment_annotations(dataset_namespace, dataset_name, dataset=dataset):
        repo_id = f"{dataset_namespace}/{dataset_name}"
        resolved_dataset = _resolve_dataset_or_error(repo_id, dataset)
        if isinstance(resolved_dataset, tuple):
            message, code = resolved_dataset
            return jsonify({"error": message}), code
        if isinstance(resolved_dataset, str):
            return jsonify({"error": resolved_dataset}), 400

        annotation_context, task_store, error_response = _load_task_store_or_error(resolved_dataset, repo_id)
        if error_response is not None:
            return error_response

        storage_path = Path(annotation_context["files"]["segment_annotations"])
        with _segment_annotation_lock:
            store = load_segment_annotation_store(storage_path, annotation_context["dataset_id"])

        frame_counts_by_episode = {
            episode_index: get_episode_frame_count(resolved_dataset, episode_index)
            for episode_index in range(resolved_dataset.num_episodes)
        }
        summary_by_episode = build_segment_summary_by_episode(
            store,
            stage_order_by_task=_build_stage_order_by_task(task_store),
            frame_counts_by_episode=frame_counts_by_episode,
            scheme=DEFAULT_SCHEME,
        )

        episode_index_raw = request.args.get("episode_index")
        if episode_index_raw is not None:
            try:
                episode_index = int(episode_index_raw)
            except ValueError:
                return jsonify({"error": "episode_index must be an integer"}), 400

            record = store["items"].get(
                str(episode_index),
                {
                    "episode_index": episode_index,
                    "task_name": "",
                    "updated_at": utc_now_iso(),
                    "schemes": {DEFAULT_SCHEME: []},
                },
            )
            return jsonify(
                {
                    "items": store["items"],
                    "episode_record": record,
                    "episode_summary": summary_by_episode.get(
                        str(episode_index),
                        {
                            "status": "unlabeled",
                            "segment_count": 0,
                            "has_custom_labels": False,
                            "can_export": False,
                            "coverage_ratio": 0.0,
                            "errors": [],
                        },
                    ),
                    "summary_by_episode": summary_by_episode,
                }
            )

        return jsonify(
            {
                "items": store["items"],
                "summary_by_episode": summary_by_episode,
                "updated_at": store.get("updated_at"),
            }
        )

    @app.route(
        "/<string:dataset_namespace>/<string:dataset_name>/api/segment-annotations",
        methods=["POST"],
    )
    def upsert_segment_annotation(dataset_namespace, dataset_name, dataset=dataset):
        repo_id = f"{dataset_namespace}/{dataset_name}"
        resolved_dataset = _resolve_dataset_or_error(repo_id, dataset)
        if isinstance(resolved_dataset, tuple):
            message, code = resolved_dataset
            return jsonify({"error": message}), code
        if isinstance(resolved_dataset, str):
            return jsonify({"error": resolved_dataset}), 400

        annotation_context, task_store, error_response = _load_task_store_or_error(resolved_dataset, repo_id)
        if error_response is not None:
            return error_response

        payload = request.get_json(silent=True) or {}
        try:
            episode_index = int(payload.get("episode_index"))
        except (TypeError, ValueError):
            return jsonify({"error": "episode_index is required and must be an integer"}), 400

        scheme = str(payload.get("scheme") or DEFAULT_SCHEME).strip()
        if scheme not in SUPPORTED_SCHEMES:
            return jsonify({"error": f"scheme must be one of: {', '.join(sorted(SUPPORTED_SCHEMES))}"}), 400

        task_name = str(payload.get("task_name") or "").strip()
        task_profile = task_store.get("tasks", {}).get(task_name)
        if not task_profile:
            return jsonify({"error": "task_name is required and must exist in task_annotation_config.json"}), 400

        stage_catalog = build_task_context_payload(annotation_context, task_name, task_profile, scheme=scheme)[
            "stage_catalog"
        ]
        scheme_config = get_scheme_config(task_profile, scheme=scheme)
        stage_order = list(scheme_config.get("stage_order", []))
        stage_order_strict = bool(scheme_config.get("stage_order_strict", True))
        allow_custom_labels = bool(task_profile.get("allow_custom_labels", True))

        raw_stage_label = str(payload.get("stage_label") or "").strip()
        if not raw_stage_label:
            return jsonify({"error": "stage_label is required"}), 400

        is_custom_label = bool(payload.get("is_custom_label", False))
        if raw_stage_label not in stage_order:
            if not allow_custom_labels:
                return jsonify({"error": "custom labels are disabled for this task"}), 400
            is_custom_label = True
        elif is_custom_label and not allow_custom_labels:
            return jsonify({"error": "custom labels are disabled for this task"}), 400

        try:
            frame_start = int(payload.get("frame_start"))
            frame_end = int(payload.get("frame_end"))
        except (TypeError, ValueError):
            return jsonify({"error": "frame_start and frame_end are required integers"}), 400

        frame_count = get_episode_frame_count(resolved_dataset, episode_index)
        if frame_count <= 0:
            return jsonify({"error": "episode has no frames"}), 400
        if frame_start < 0 or frame_end < 0 or frame_start >= frame_count or frame_end >= frame_count:
            return jsonify({"error": f"frame range must be within [0, {frame_count - 1}]"}), 400
        if frame_start > frame_end:
            return jsonify({"error": "frame_start must be <= frame_end"}), 400

        timestamps = get_episode_timestamps(resolved_dataset, episode_index)
        time_start_s = float(timestamps[frame_start])
        time_end_s = float(timestamps[frame_end])

        display_label = str(payload.get("display_label") or "").strip()
        if not display_label:
            display_label = infer_display_label(raw_stage_label, stage_catalog)
        color = infer_color(raw_stage_label, stage_catalog)

        operator = (
            str(request.headers.get("X-Operator") or payload.get("operator") or "anonymous").strip() or "anonymous"
        )
        segment_id = str(payload.get("segment_id") or new_annotation_id("segment"))

        outcome_raw = payload.get("outcome")
        outcome = str(outcome_raw).strip() if isinstance(outcome_raw, str) and outcome_raw.strip() else None
        if outcome is not None:
            allowed_outcomes = {o.get("id") for o in (task_profile.get("outcomes") or []) if o.get("id")}
            if allowed_outcomes and outcome not in allowed_outcomes:
                return jsonify({"error": f"outcome must be one of: {', '.join(sorted(allowed_outcomes))}"}), 400

        segment_record = normalize_segment(
            {
                "segment_id": segment_id,
                "task_name": task_name,
                "scheme": scheme,
                "stage_label": raw_stage_label,
                "display_label": display_label,
                "color": color,
                "frame_start": frame_start,
                "frame_end": frame_end,
                "time_start_s": time_start_s,
                "time_end_s": time_end_s,
                "operator": operator,
                "updated_at": utc_now_iso(),
                "is_custom_label": is_custom_label,
                "outcome": outcome,
            }
        )

        storage_path = Path(annotation_context["files"]["segment_annotations"])
        with _segment_annotation_lock:
            store = load_segment_annotation_store(storage_path, annotation_context["dataset_id"])
            record = store["items"].get(str(episode_index))
            if record is None:
                record = {
                    "episode_index": episode_index,
                    "task_name": task_name,
                    "updated_at": utc_now_iso(),
                    "schemes": {scheme: []},
                }
                store["items"][str(episode_index)] = record

            existing_task_name = str(record.get("task_name") or task_name).strip() or task_name
            if existing_task_name != task_name and any(record.get("schemes", {}).get(scheme, [])):
                return jsonify({"error": "existing segment annotations already use a different task_name"}), 400

            existing_segments = [
                normalize_segment(existing)
                for existing in record.get("schemes", {}).get(scheme, [])
                if str(existing.get("segment_id") or "") != segment_id
            ]
            candidate_segments = existing_segments + [segment_record]

            # Containment: flatten_internal segment must fall inside a parent_scheme[parent_stage] segment.
            parent_scheme_name = scheme_config.get("parent_scheme")
            parent_stage_label = scheme_config.get("parent_stage")
            if parent_scheme_name and parent_stage_label:
                parent_segments = [
                    normalize_segment(s)
                    for s in record.get("schemes", {}).get(parent_scheme_name, [])
                    if str(s.get("stage_label") or "") == parent_stage_label
                ]
                contained = any(
                    int(p["frame_start"]) <= frame_start and frame_end <= int(p["frame_end"])
                    for p in parent_segments
                )
                if not contained:
                    return jsonify(
                        {
                            "error": (
                                f"{scheme} segment must be fully contained within a "
                                f"{parent_scheme_name}/{parent_stage_label} segment first"
                            )
                        }
                    ), 400

            summary = summarize_episode_segments(
                candidate_segments,
                stage_order=stage_order,
                total_frames=frame_count,
                stage_order_strict=stage_order_strict,
            )
            blocking_errors = [
                error
                for error in summary["errors"]
                if error.startswith("overlap:") or error.startswith("order:") or error.startswith("duplicate_stage:")
            ]
            if blocking_errors:
                return jsonify({"error": f"segment validation failed: {', '.join(blocking_errors)}"}), 400

            record["task_name"] = task_name
            record.setdefault("schemes", {})
            record["schemes"][scheme] = sorted(
                candidate_segments,
                key=lambda item: (int(item["frame_start"]), int(item["frame_end"]), item["segment_id"]),
            )
            record["updated_at"] = utc_now_iso()
            save_segment_annotation_store(storage_path, store)
            episode_record = store["items"][str(episode_index)]

        summary = summarize_episode_segments(
            episode_record.get("schemes", {}).get(scheme, []),
            stage_order=stage_order,
            total_frames=frame_count,
            stage_order_strict=stage_order_strict,
        )
        summary["task_name"] = task_name

        return jsonify(
            {
                "ok": True,
                "record": segment_record,
                "episode_record": episode_record,
                "episode_summary": summary,
            }
        )

    @app.route(
        "/<string:dataset_namespace>/<string:dataset_name>/api/segment-annotations/<string:segment_id>",
        methods=["DELETE"],
    )
    def delete_segment_annotation(dataset_namespace, dataset_name, segment_id, dataset=dataset):
        repo_id = f"{dataset_namespace}/{dataset_name}"
        resolved_dataset = _resolve_dataset_or_error(repo_id, dataset)
        if isinstance(resolved_dataset, tuple):
            message, code = resolved_dataset
            return jsonify({"error": message}), code
        if isinstance(resolved_dataset, str):
            return jsonify({"error": resolved_dataset}), 400

        annotation_context, task_store, error_response = _load_task_store_or_error(resolved_dataset, repo_id)
        if error_response is not None:
            return error_response

        storage_path = Path(annotation_context["files"]["segment_annotations"])
        with _segment_annotation_lock:
            store = load_segment_annotation_store(storage_path, annotation_context["dataset_id"])
            deleted_record = None
            deleted_episode_index = None
            deleted_task_name = None
            deleted_scheme = None
            for episode_key, episode_record in list(store["items"].items()):
                schemes = episode_record.get("schemes", {})
                for scheme_name in list(schemes.keys()):
                    current_segments = schemes.get(scheme_name, [])
                    remaining_segments = []
                    found_in_scheme = False
                    for segment in current_segments:
                        if str(segment.get("segment_id") or "") == segment_id:
                            deleted_record = normalize_segment(segment)
                            deleted_episode_index = int(episode_record.get("episode_index", episode_key))
                            deleted_task_name = str(episode_record.get("task_name") or "")
                            deleted_scheme = scheme_name
                            found_in_scheme = True
                            continue
                        remaining_segments.append(normalize_segment(segment))

                    if not found_in_scheme:
                        continue

                    if remaining_segments:
                        episode_record["schemes"][scheme_name] = remaining_segments
                    else:
                        episode_record["schemes"].pop(scheme_name, None)
                    episode_record["updated_at"] = utc_now_iso()
                    if not episode_record.get("schemes"):
                        store["items"].pop(episode_key, None)
                    save_segment_annotation_store(storage_path, store)
                    break

                if deleted_record is not None:
                    break

        if deleted_record is None or deleted_episode_index is None:
            return jsonify({"error": f"segment_id not found: {segment_id}"}), 404

        summary_scheme = deleted_scheme or DEFAULT_SCHEME
        stage_order = list(
            get_scheme_config(task_store.get("tasks", {}).get(deleted_task_name, {}), summary_scheme).get(
                "stage_order", []
            )
        )
        remaining_record = store.get("items", {}).get(
            str(deleted_episode_index),
            {
                "episode_index": deleted_episode_index,
                "task_name": deleted_task_name,
                "updated_at": utc_now_iso(),
                "schemes": {summary_scheme: []},
            },
        )
        summary = summarize_episode_segments(
            remaining_record.get("schemes", {}).get(summary_scheme, []),
            stage_order=stage_order,
            total_frames=get_episode_frame_count(resolved_dataset, deleted_episode_index),
        )
        summary["task_name"] = deleted_task_name
        summary["scheme"] = summary_scheme

        return jsonify(
            {
                "ok": True,
                "deleted_record": deleted_record,
                "episode_record": remaining_record,
                "episode_summary": summary,
            }
        )

    @app.route(
        "/<string:dataset_namespace>/<string:dataset_name>/api/frame-events",
        methods=["GET"],
    )
    def get_frame_events(dataset_namespace, dataset_name, dataset=dataset):
        repo_id = f"{dataset_namespace}/{dataset_name}"
        resolved_dataset = _resolve_dataset_or_error(repo_id, dataset)
        if isinstance(resolved_dataset, tuple):
            message, code = resolved_dataset
            return jsonify({"error": message}), code
        if isinstance(resolved_dataset, str):
            return jsonify({"error": resolved_dataset}), 400

        annotation_context, _, error_response = _load_task_store_or_error(resolved_dataset, repo_id)
        if error_response is not None:
            return error_response

        storage_path = Path(annotation_context["files"]["frame_events"])
        with _frame_event_lock:
            store = load_frame_event_records(storage_path, annotation_context["dataset_id"])

        episode_index_raw = request.args.get("episode_index")
        items = store["items"]
        if episode_index_raw is not None:
            try:
                episode_index = int(episode_index_raw)
            except ValueError:
                return jsonify({"error": "episode_index must be an integer"}), 400
            items = [item for item in items if int(item.get("episode_index", -1)) == episode_index]

        return jsonify({"items": items, "updated_at": store.get("updated_at")})

    @app.route(
        "/<string:dataset_namespace>/<string:dataset_name>/api/frame-events",
        methods=["POST"],
    )
    def upsert_frame_event(dataset_namespace, dataset_name, dataset=dataset):
        repo_id = f"{dataset_namespace}/{dataset_name}"
        resolved_dataset = _resolve_dataset_or_error(repo_id, dataset)
        if isinstance(resolved_dataset, tuple):
            message, code = resolved_dataset
            return jsonify({"error": message}), code
        if isinstance(resolved_dataset, str):
            return jsonify({"error": resolved_dataset}), 400

        annotation_context, task_store, error_response = _load_task_store_or_error(resolved_dataset, repo_id)
        if error_response is not None:
            return error_response

        payload = request.get_json(silent=True) or {}
        try:
            episode_index = int(payload.get("episode_index"))
            frame_index = int(payload.get("frame_index"))
        except (TypeError, ValueError):
            return jsonify({"error": "episode_index and frame_index are required integers"}), 400

        frame_count = get_episode_frame_count(resolved_dataset, episode_index)
        if frame_index < 0 or frame_index >= frame_count:
            return jsonify({"error": f"frame_index must be within [0, {frame_count - 1}]"}), 400

        event_type = str(payload.get("event_type") or "").strip()
        if not event_type:
            return jsonify({"error": "event_type is required"}), 400

        allowed_event_ids = {e.get("id") for e in (task_store.get("event_types") or []) if e.get("id")}
        if allowed_event_ids and event_type not in allowed_event_ids:
            return jsonify(
                {"error": f"event_type must be one of: {', '.join(sorted(allowed_event_ids))}"}
            ), 400

        task_name = str(payload.get("task_name") or "").strip()
        if task_name and task_name not in task_store.get("tasks", {}):
            return jsonify({"error": "task_name must exist in task_annotation_config.json"}), 400

        timestamps = get_episode_timestamps(resolved_dataset, episode_index)
        operator = (
            str(request.headers.get("X-Operator") or payload.get("operator") or "anonymous").strip() or "anonymous"
        )
        event_id = str(payload.get("event_id") or new_annotation_id("event"))

        event_record = normalize_frame_event(
            {
                "event_id": event_id,
                "task_name": task_name,
                "episode_index": episode_index,
                "frame_index": frame_index,
                "time_s": float(timestamps[frame_index]),
                "event_type": event_type,
                "note": str(payload.get("note") or "").strip(),
                "operator": operator,
                "updated_at": utc_now_iso(),
            }
        )

        storage_path = Path(annotation_context["files"]["frame_events"])
        with _frame_event_lock:
            store = load_frame_event_records(storage_path, annotation_context["dataset_id"])
            items = [item for item in store["items"] if str(item.get("event_id") or "") != event_id]
            items.append(event_record)
            save_frame_event_records(storage_path, annotation_context["dataset_id"], items)

        return jsonify({"ok": True, "record": event_record})

    @app.route(
        "/<string:dataset_namespace>/<string:dataset_name>/api/frame-events/<string:event_id>",
        methods=["DELETE"],
    )
    def delete_frame_event(dataset_namespace, dataset_name, event_id, dataset=dataset):
        repo_id = f"{dataset_namespace}/{dataset_name}"
        resolved_dataset = _resolve_dataset_or_error(repo_id, dataset)
        if isinstance(resolved_dataset, tuple):
            message, code = resolved_dataset
            return jsonify({"error": message}), code
        if isinstance(resolved_dataset, str):
            return jsonify({"error": resolved_dataset}), 400

        annotation_context, _, error_response = _load_task_store_or_error(resolved_dataset, repo_id)
        if error_response is not None:
            return error_response

        storage_path = Path(annotation_context["files"]["frame_events"])
        with _frame_event_lock:
            store = load_frame_event_records(storage_path, annotation_context["dataset_id"])
            remaining_items = []
            deleted_record = None
            for item in store["items"]:
                if str(item.get("event_id") or "") == event_id:
                    deleted_record = item
                    continue
                remaining_items.append(item)

            if deleted_record is None:
                return jsonify({"error": f"event_id not found: {event_id}"}), 404

            save_frame_event_records(storage_path, annotation_context["dataset_id"], remaining_items)

        return jsonify({"ok": True, "deleted_record": deleted_record})

    app.run(host=host, port=port)
