#!/usr/bin/env python3
"""training_lance_to_lerobot.py — Convert build_training_lance output → LeRobot v2.1.

Source : a single .lance produced by `scripts/build_training_lance.py`
         (lerobot-style column names already applied; episode_index already
         remapped to 0..N-1; rows already at output fps via anchor downsample;
         schema metadata already carries lerobot:fps / lerobot:tasks_json /
         video_lance:image_height|width / master|slave_joint_names).
Target : a directory tree consumable by stock LeRobotDataset (v2.1).

Differs from sister `lance_to_lerobot.py` (which consumes raw per-episode
.lance from `robot_to_lance.py`):
  * Single source dataset, multi-episode merged.
  * Reads fps / tasks / image size / joint names directly from schema metadata
    instead of re-deriving from camera timestamps + heuristics.
  * No canonical-cam dedupe — rows are already 1:1 with target frames.
  * Column lookups use the post-rename names: `action`, `observation_state`,
    `observation_images_cam_*`, `<lerobot>_gop_index`,
    `<lerobot>_frame_index_in_gop`.

Example:
    pixi run python tools/training_lance_to_lerobot.py \\
        --lance /path/to/<dataset>_30HZ.lance \\
        --output-dir /path/to/lerobot_out \\
        --repo-id local/airbot_fold \\
        --overwrite
"""

from __future__ import annotations

import argparse
import concurrent.futures
import io
import json
import multiprocessing as mp
import os
import shutil
import sys
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import av
import lance
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

CODEBASE_VERSION = "v2.1"
ROBOT_TYPE = "airbot_play_dual_arm"
CHUNK_SIZE = 1000

# Source blob column name -> LeRobot v2.1 video feature key.
# These match the post-build_training_lance schema; older raw datasets
# (`mid` / `left` / `right`) are not handled here — use lance_to_lerobot.py.
CAM_FEATURE_KEYS = {
    "observation_images_cam_env":         "observation.images.cam_env",
    "observation_images_cam_left_wrist":  "observation.images.cam_left_wrist",
    "observation_images_cam_right_wrist": "observation.images.cam_right_wrist",
}

# Per-encoder PyAV `stream.options`. `qp=18` (NVENC) ≈ `crf=18` (libx264) in
# perceptual quality (PSNR difference typically <0.5 dB). Edit here to retune.
# `libx264.preset` is overridable via `--libx264-preset` CLI flag (e.g.
# `ultrafast` for 2-3× encode speed at marginal quality cost).
ENCODER_OPTIONS: dict[str, dict[str, str]] = {
    "libx264":    {"crf": "18", "preset": "fast"},
    "h264_nvenc": {"preset": "p4", "rc": "constqp", "qp": "18"},
}

# x264 presets in increasing speed (decreasing compression efficiency).
LIBX264_PRESETS = (
    "placebo", "veryslow", "slower", "slow", "medium",
    "fast", "faster", "veryfast", "superfast", "ultrafast",
)


def _probe_nvenc_runtime() -> bool:
    """Try to actually OPEN a tiny h264_nvenc context.

    NVIDIA "compute-only" datacenter cards (H20Z, H100 PCIe in some SKUs)
    expose `h264_nvenc` in `av.codecs_available` but fail at `avcodec_open2`
    with `OpenEncodeSessionEx: unsupported device`. We only know if NVENC
    actually works after a real open attempt.
    """
    if "h264_nvenc" not in av.codecs_available:
        return False
    try:
        codec = av.codec.Codec("h264_nvenc", "w")
        ctx = codec.create()
        ctx.width = 64
        ctx.height = 64
        ctx.pix_fmt = "yuv420p"
        ctx.time_base = Fraction(1, 30)
        ctx.open()
        ctx.close()
        return True
    except Exception:
        return False


def select_encoder(preference: str) -> str:
    """Resolve `--encoder` CLI value to an actual codec name.

    `auto` -> h264_nvenc if a runtime open succeeds (i.e. host has a
              real NVENC-capable GPU), otherwise libx264. Avoids the false
              positive on compute-only datacenter cards.
    `libx264` / `h264_nvenc` -> use as-is; clear error at `add_stream` time
              if the codec can't open on this host.
    """
    if preference != "auto":
        return preference
    return "h264_nvenc" if _probe_nvenc_runtime() else "libx264"


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def chunk_for(episode_index: int) -> int:
    return episode_index // CHUNK_SIZE


def stats_for(arr) -> dict:
    """Per-column min/max/mean/std for a numerical array; count is `[N]`."""
    a = np.asarray(arr, dtype=np.float64)
    if a.ndim == 1:
        a = a[:, None]
    return {
        "min": a.min(axis=0).tolist(),
        "max": a.max(axis=0).tolist(),
        "mean": a.mean(axis=0).tolist(),
        "std": a.std(axis=0).tolist(),
        "count": [int(a.shape[0])],
    }


def stats_for_image_samples(samples_uint8: np.ndarray) -> dict:
    """Per-channel image stats normalized to [0,1], shape `(C, 1, 1)`.

    LeRobot's `aggregate_stats` (`compute_stats._assert_type_and_shape`) hard-
    requires the trailing `(1, 1)` for image features even though the v2.1
    spec docs only show `[C, 1]`.
    """
    a = samples_uint8.astype(np.float32) / 255.0
    return {
        "min":  a.min(axis=(0, 1, 2)).reshape(-1, 1, 1).tolist(),
        "max":  a.max(axis=(0, 1, 2)).reshape(-1, 1, 1).tolist(),
        "mean": a.mean(axis=(0, 1, 2)).reshape(-1, 1, 1).tolist(),
        "std":  a.std(axis=(0, 1, 2)).reshape(-1, 1, 1).tolist(),
        "count": [int(samples_uint8.shape[0])],
    }


def parse_joint_names(meta_value: str) -> list[str]:
    """`<sn>/joint1,<sn>/joint2,...` → `['joint1', 'joint2', ...]`.

    Strips the arm-serial prefix written by robot_collector.py so we get plain
    joint names suitable for LeRobot's `names.motors` field.
    """
    parts = [p.strip() for p in meta_value.split(",") if p.strip()]
    return [p.rsplit("/", 1)[-1] for p in parts]


def read_dataset_metadata(ds: lance.LanceDataset) -> dict[str, str]:
    raw = ds.schema.metadata or {}
    return {k.decode(): v.decode(errors="replace") for k, v in raw.items()}


def parse_episodes_spec(spec: str) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


def decode_h264_gop(blob: bytes, decoder_threads: int = 1) -> list[av.VideoFrame]:
    """Decode a single H264 GOP byte payload to a list of frames.

    `decoder_threads > 1` enables PyAV's frame/slice threading. Modest gain
    on bframes=0 streams (source `record_cameras.py` forces this), but worth
    a probe at decoder_threads=2-4.
    """
    bio = io.BytesIO(blob)
    container = av.open(bio, format="h264")
    if decoder_threads > 1:
        container.streams.video[0].codec_context.thread_count = decoder_threads
        container.streams.video[0].codec_context.thread_type = "FRAME"
    frames = list(container.decode(video=0))
    container.close()
    return frames


def build_global_gop_lookup(
    ds: lance.LanceDataset,
    cam_blob_cols: list[str],
) -> dict[tuple[str, int, int], int]:
    """{(cam, episode_index, gop_index) -> first global row index in ds}.

    Lance returns rows in fragment-then-storage order; build_training_lance.py
    writes one fragment per episode in `frame_index` order, so the FIRST row
    of each (episode, gop_index) group is the `Blob.from_bytes` GOP-start
    row that `take_blobs` can decode safely. Multi-episode datasets need this
    explicit lookup since `frame_index` is per-episode, not global.
    """
    cols = ["episode_index"] + [f"{c}_gop_index" for c in cam_blob_cols]
    tbl = ds.to_table(columns=cols)
    eps = tbl["episode_index"].to_pylist()
    out: dict[tuple[str, int, int], int] = {}
    for cam in cam_blob_cols:
        gops = tbl[f"{cam}_gop_index"].to_pylist()
        for row_idx, (ep, g) in enumerate(zip(eps, gops)):
            key = (cam, int(ep), int(g))
            if key not in out:
                out[key] = row_idx
    return out


# --------------------------------------------------------------------------- #
# Per-episode conversion                                                      #
# --------------------------------------------------------------------------- #


@dataclass
class EpisodeResult:
    episode_index: int
    length: int
    task: str
    stats: dict


def encode_episode_video(
    ds: lance.LanceDataset,
    blob_col: str,
    src_episode_index: int,
    gop_per_row: list[int],
    fig_per_row: list[int],
    output_mp4: Path,
    fps: int,
    expected_height: int,
    expected_width: int,
    gop_lookup: dict[tuple[str, int, int], int],
    image_sample_buf: list,
    sample_stride: int,
    encoder_name: str = "libx264",
    encoder_threads: int = 1,
    decoder_threads: int = 1,
    encoder_options_override: dict[str, str] | None = None,
) -> tuple[int, int]:
    """Decode source GOPs lazily and emit one frame per (gop, fig) pair."""
    container_out: av.container.OutputContainer | None = None
    stream = None
    cur_gop = -1
    cur_frames: list[av.VideoFrame] = []
    pts = 0

    try:
        for i, (g, f_in_gop) in enumerate(zip(gop_per_row, fig_per_row)):
            if g != cur_gop:
                global_row = gop_lookup.get((blob_col, src_episode_index, g))
                if global_row is None:
                    raise RuntimeError(
                        f"gop_index={g} not found for episode={src_episode_index} "
                        f"on column {blob_col!r}"
                    )
                blob = ds.take_blobs(blob_col, indices=[global_row])[0].read()
                cur_frames = decode_h264_gop(blob, decoder_threads=decoder_threads)
                cur_gop = g

            if not cur_frames:
                raise RuntimeError(
                    f"GOP {g} of {blob_col} (episode={src_episode_index}) decoded to 0 frames"
                )
            if f_in_gop < 0 or f_in_gop >= len(cur_frames):
                raise RuntimeError(
                    f"{blob_col} episode={src_episode_index} gop={g}: "
                    f"frame_index_in_gop={f_in_gop} outside decoded GOP length {len(cur_frames)}"
                )
            frame = cur_frames[f_in_gop]

            if container_out is None:
                container_out = av.open(str(output_mp4), mode="w")
                stream = container_out.add_stream(encoder_name, rate=fps)
                stream.width = frame.width
                stream.height = frame.height
                stream.pix_fmt = "yuv420p"
                stream.options = encoder_options_override or ENCODER_OPTIONS.get(encoder_name, {})
                stream.codec_context.time_base = Fraction(1, fps)
                if encoder_threads > 1:
                    stream.codec_context.thread_count = encoder_threads
                    stream.codec_context.thread_type = "FRAME"

            frame.pts = pts
            frame.time_base = Fraction(1, fps)
            pts += 1

            if sample_stride > 0 and (i % sample_stride) == 0:
                image_sample_buf.append(frame.to_ndarray(format="rgb24"))

            for packet in stream.encode(frame):
                container_out.mux(packet)

        if stream is not None:
            for packet in stream.encode():
                container_out.mux(packet)
    finally:
        if container_out is not None:
            container_out.close()

    if stream is None:
        raise RuntimeError(
            f"No frames written for episode={src_episode_index} cam={blob_col}"
        )
    if (stream.height, stream.width) != (expected_height, expected_width):
        raise RuntimeError(
            f"{blob_col}: actual {stream.width}x{stream.height} differs from "
            f"metadata {expected_width}x{expected_height}; refusing to write a "
            f"dataset whose info.json shape would disagree with its video content"
        )
    return stream.width, stream.height


def convert_episode(
    ds: lance.LanceDataset,
    src_episode_index: int,
    dst_episode_index: int,
    global_index_offset: int,
    output_dir: Path,
    fps: int,
    image_height: int,
    image_width: int,
    image_sample_stride: int,
    cam_blob_cols: list[str],
    gop_lookup: dict[tuple[str, int, int], int],
    has_action_source: bool,
    encoder_name: str = "libx264",
    encoder_threads: int = 1,
    decoder_threads: int = 1,
    cams_parallel: int = 1,
    encoder_options_override: dict[str, str] | None = None,
) -> tuple[EpisodeResult, dict[str, tuple[int, int]]]:
    cols = [
        "frame_index", "episode_index", "task_index", "language_instruction",
        "action", "observation_state",
    ]
    if has_action_source:
        cols.append("action_source")
    for cam in cam_blob_cols:
        cols += [f"{cam}_gop_index", f"{cam}_frame_index_in_gop"]

    table = ds.to_table(columns=cols, filter=f"episode_index = {src_episode_index}")
    df = table.to_pandas().sort_values("frame_index").reset_index(drop=True)
    if df.empty:
        raise RuntimeError(f"No rows for episode_index={src_episode_index}")

    n_frames = len(df)
    state = np.stack(df["observation_state"].to_numpy()).astype(np.float32)
    action = np.stack(df["action"].to_numpy()).astype(np.float32)
    if state.shape[1] != action.shape[1]:
        raise RuntimeError(
            f"state dim ({state.shape[1]}) != action dim ({action.shape[1]}); "
            f"unsupported asymmetric layout"
        )

    frame_index = np.arange(n_frames, dtype=np.int64)
    timestamp_s = (frame_index / float(fps)).astype(np.float32)
    episode_index_col = np.full(n_frames, dst_episode_index, dtype=np.int64)
    index_col = frame_index + global_index_offset
    task_index_col = df["task_index"].astype(np.int64).to_numpy()
    instruction = str(df["language_instruction"].iloc[0])

    # ---- write videos & sample frames for image stats ----
    # Per-cam threading: PyAV releases the GIL during codec ops, so 3 cams in
    # 3 threads = real parallelism. Each cam writes its own MP4 path and has
    # its own image_sample_buf, so no shared mutable state. lance.take_blobs
    # is shared (one ds object) but only does immutable reads.
    sample_dims: dict[str, tuple[int, int]] = {}
    image_sample_buffers: dict[str, list] = {cam: [] for cam in cam_blob_cols}
    chunk_idx = chunk_for(dst_episode_index)

    def _encode_one_cam(cam: str) -> tuple[str, tuple[int, int]]:
        feat_key = CAM_FEATURE_KEYS[cam]
        video_dir = output_dir / "videos" / f"chunk-{chunk_idx:03d}" / feat_key
        video_dir.mkdir(parents=True, exist_ok=True)
        out_mp4 = video_dir / f"episode_{dst_episode_index:06d}.mp4"

        gop_per_row = df[f"{cam}_gop_index"].astype(int).tolist()
        fig_per_row = df[f"{cam}_frame_index_in_gop"].astype(int).tolist()
        wh = encode_episode_video(
            ds=ds,
            blob_col=cam,
            src_episode_index=src_episode_index,
            gop_per_row=gop_per_row,
            fig_per_row=fig_per_row,
            output_mp4=out_mp4,
            fps=fps,
            expected_height=image_height,
            expected_width=image_width,
            gop_lookup=gop_lookup,
            image_sample_buf=image_sample_buffers[cam],
            sample_stride=image_sample_stride,
            encoder_name=encoder_name,
            encoder_threads=encoder_threads,
            decoder_threads=decoder_threads,
            encoder_options_override=encoder_options_override,
        )
        return feat_key, wh

    n_threads = max(1, min(cams_parallel, len(cam_blob_cols)))
    if n_threads > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=n_threads) as ex:
            for feat_key, wh in ex.map(_encode_one_cam, cam_blob_cols):
                sample_dims[feat_key] = wh
    else:
        for cam in cam_blob_cols:
            feat_key, wh = _encode_one_cam(cam)
            sample_dims[feat_key] = wh

    # ---- write parquet ----
    data_dir = output_dir / "data" / f"chunk-{chunk_idx:03d}"
    data_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = data_dir / f"episode_{dst_episode_index:06d}.parquet"

    # Match stock LeRobot v2.1 layout: parquet contains scalar/state/action only;
    # video columns are resolved at __getitem__ time from `video_path` template
    # + frame_index in info.json, NOT stored in parquet.
    parquet_cols: dict[str, pa.Array] = {
        "observation.state": pa.array(list(state), type=pa.list_(pa.float32())),
        "action": pa.array(list(action), type=pa.list_(pa.float32())),
        "timestamp": pa.array(timestamp_s, type=pa.float32()),
        "frame_index": pa.array(frame_index, type=pa.int64()),
        "episode_index": pa.array(episode_index_col, type=pa.int64()),
        "index": pa.array(index_col, type=pa.int64()),
        "task_index": pa.array(task_index_col, type=pa.int64()),
    }
    if has_action_source:
        # Per-frame VLA / human teleop provenance, written by HIL collections.
        # Kept as raw string for grep-ability; downstream filters can map to int.
        parquet_cols["action_source"] = pa.array(
            df["action_source"].astype(str).tolist(), type=pa.string()
        )
    pq.write_table(pa.table(parquet_cols), parquet_path)

    # ---- per-episode stats ----
    eps_stats: dict = {
        "observation.state": stats_for(state),
        "action": stats_for(action),
        "timestamp": stats_for(timestamp_s),
        "frame_index": stats_for(frame_index),
        "episode_index": stats_for(episode_index_col),
        "index": stats_for(index_col),
        "task_index": stats_for(task_index_col),
    }
    for cam in cam_blob_cols:
        feat_key = CAM_FEATURE_KEYS[cam]
        if image_sample_buffers[cam]:
            eps_stats[feat_key] = stats_for_image_samples(
                np.stack(image_sample_buffers[cam])
            )

    return EpisodeResult(dst_episode_index, n_frames, instruction, eps_stats), sample_dims


# --------------------------------------------------------------------------- #
# Multiprocessing worker (module-level for spawn pickling)                    #
# --------------------------------------------------------------------------- #


def _convert_episode_worker(args):
    """Pickle-safe wrapper for `convert_episode`.

    Each worker opens its OWN `lance.dataset()` instance (lance is fork-unsafe;
    we run with `mp.get_context("spawn")` so this is the canonical pattern).

    `args` shape: (lance_path_str, src_ep, dst_idx, global_offset, kwargs_dict).
    Returns (dst_idx, EpisodeResult, dims) so the main process can sort
    `imap_unordered` results back to deterministic order.
    """
    lance_path, src_ep, dst_idx, global_offset, kwargs = args
    ds = lance.dataset(lance_path)
    result, dims = convert_episode(
        ds=ds,
        src_episode_index=src_ep,
        dst_episode_index=dst_idx,
        global_index_offset=global_offset,
        **kwargs,
    )
    return dst_idx, result, dims


# --------------------------------------------------------------------------- #
# Meta writers                                                                #
# --------------------------------------------------------------------------- #


def write_meta(
    output_dir: Path,
    repo_id: str,
    fps: int,
    episodes: list[EpisodeResult],
    tasks_json: dict[str, str],
    sample_dims: dict[str, tuple[int, int]],
    cam_blob_cols: list[str],
    state_names: list[str],
    image_stats_present: bool,
    has_action_source: bool,
    encoder_name: str = "libx264",
):
    meta = output_dir / "meta"
    meta.mkdir(parents=True, exist_ok=True)

    with (meta / "tasks.jsonl").open("w") as f:
        for ti_str in sorted(tasks_json.keys(), key=int):
            f.write(json.dumps(
                {"task_index": int(ti_str), "task": tasks_json[ti_str]},
                ensure_ascii=False,
            ) + "\n")

    with (meta / "episodes.jsonl").open("w") as f:
        for e in episodes:
            f.write(json.dumps({
                "episode_index": e.episode_index,
                "tasks": [e.task],
                "length": e.length,
            }, ensure_ascii=False) + "\n")

    with (meta / "episodes_stats.jsonl").open("w") as f:
        for e in episodes:
            f.write(json.dumps(
                {"episode_index": e.episode_index, "stats": e.stats},
                ensure_ascii=False,
            ) + "\n")

    state_dim = len(state_names)
    features: dict = {
        "observation.state": {
            "dtype": "float32", "shape": [state_dim],
            "names": {"motors": state_names},
        },
        "action": {
            "dtype": "float32", "shape": [state_dim],
            "names": {"motors": state_names},
        },
    }
    for cam in cam_blob_cols:
        feat_key = CAM_FEATURE_KEYS[cam]
        w, h = sample_dims[feat_key]
        features[feat_key] = {
            "dtype": "video",
            "shape": [h, w, 3],
            "names": ["height", "width", "channels"],
            "info": {
                "video.fps": float(fps),
                "video.height": h,
                "video.width": w,
                "video.channels": 3,
                "video.codec": encoder_name,
                "video.pix_fmt": "yuv420p",
                "video.is_depth_map": False,
                "has_audio": False,
            },
        }
    for k, dt in [
        ("timestamp", "float32"), ("frame_index", "int64"),
        ("episode_index", "int64"), ("index", "int64"),
        ("task_index", "int64"),
    ]:
        features[k] = {"dtype": dt, "shape": [1], "names": None}
    if has_action_source:
        features["action_source"] = {"dtype": "string", "shape": [1], "names": None}

    total_frames = sum(e.length for e in episodes)
    total_episodes = len(episodes)
    info = {
        "codebase_version": CODEBASE_VERSION,
        "robot_type": ROBOT_TYPE,
        "total_episodes": total_episodes,
        "total_frames": total_frames,
        "total_tasks": len(tasks_json),
        "total_videos": total_episodes * len(cam_blob_cols),
        "total_chunks": chunk_for(episodes[-1].episode_index) + 1,
        "chunks_size": CHUNK_SIZE,
        "fps": fps,
        "splits": {"train": f"0:{total_episodes}"},
        "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
        "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
        "features": features,
    }
    (meta / "info.json").write_text(json.dumps(info, indent=4))

    # Soft sanity: image stats are nice-to-have for LeRobot v2.1; warn if any
    # episode lacks them so callers know stats are partial. Kept as warning,
    # not error, so light/preview runs (sample_stride=0) still produce valid
    # parquet/info even when image stats are deliberately skipped.
    if not image_stats_present:
        print("[warn] no image stats written (sample_stride=0 or no samples decoded); "
              "per-image-feature stats will be missing in episodes_stats.jsonl")


# --------------------------------------------------------------------------- #
# Driver                                                                      #
# --------------------------------------------------------------------------- #


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--lance", required=True, type=Path,
                   help="Path to a single .lance produced by build_training_lance.py.")
    p.add_argument("--output-dir", required=True, type=Path)
    p.add_argument("--repo-id", default="local/airbot_dataset")
    p.add_argument("--episodes", default=None,
                   help="Subset spec on SOURCE episode_index, e.g. '0-4,10' "
                        "(default: all). Output episode_index is enumerated 0..M-1.")
    p.add_argument("--image-sample-stride", type=int, default=30,
                   help="For image-stats sampling: keep every Nth decoded frame. "
                        "0 = skip image stats entirely. Default 30 (≈1 sample/sec @ 30Hz).")
    p.add_argument("--encoder", default="auto",
                   choices=["auto", "libx264", "h264_nvenc"],
                   help="Output video encoder. `auto` = h264_nvenc when PyAV "
                        "actually opens it (real NVENC GPU present), else libx264. "
                        "Compute-only datacenter cards (H20Z, some H100 SKUs) lack "
                        "NVENC and auto correctly falls back.")
    p.add_argument("--num-workers", type=int, default=1,
                   help="Per-episode parallelism (mp.spawn). 1 = serial (default, "
                        "matches original behavior). >1 = N parallel processes; "
                        "video re-encode is CPU-bound, so scaling is near-linear "
                        "until disk I/O / GPU encode session caps. 8-16 is a sane "
                        "range on a many-core box with libx264.")
    p.add_argument("--cams-parallel", type=int, default=3,
                   help="Per-episode camera concurrency via ThreadPoolExecutor. "
                        "Default 3 = all cams in parallel (PyAV releases the GIL "
                        "during codec ops). 1 = serial. Cumulative speedup with "
                        "--num-workers (e.g. 8 workers × 3 cams = 24 codec ops).")
    p.add_argument("--encoder-threads", type=int, default=0,
                   help="libx264 internal threading (FRAME mode). 0 = AUTO "
                        "(default): cap to `cpu_count // (num_workers × cams_parallel)` "
                        "to avoid oversubscription, but at least 1. Pass an explicit "
                        "N >= 1 to force.")
    p.add_argument("--decoder-threads", type=int, default=2,
                   help="PyAV h264 decoder threading. Modest gain (source has "
                        "bframes=0, so frame-parallel decode is limited). Default 2.")
    p.add_argument("--libx264-preset", default="fast",
                   choices=LIBX264_PRESETS,
                   help="libx264 speed preset. `fast` (default) = quality target. "
                        "`ultrafast` = ~2-3× faster encode at slight compression "
                        "loss; useful for big batch jobs where encode is the wall "
                        "bottleneck. Has no effect with --encoder h264_nvenc.")
    p.add_argument("--overwrite", action="store_true",
                   help="Wipe output_dir before writing.")
    args = p.parse_args()
    encoder_name = select_encoder(args.encoder)
    if args.num_workers < 1:
        sys.exit(f"--num-workers must be >= 1 (got {args.num_workers})")
    if args.cams_parallel < 1:
        sys.exit(f"--cams-parallel must be >= 1 (got {args.cams_parallel})")
    if args.encoder_threads < 0:
        sys.exit(f"--encoder-threads must be >= 0 (got {args.encoder_threads})")
    if args.decoder_threads < 1:
        sys.exit(f"--decoder-threads must be >= 1 (got {args.decoder_threads})")

    # Auto-cap encoder_threads to avoid CPU oversubscription. With 32 workers ×
    # 3 cams × 4 encoder threads = 384 codec threads on a 180-core box, libx264
    # cache pressure can DEGRADE throughput. The cap = cores / (workers × cams),
    # floored at 1.
    if args.encoder_threads == 0:
        cores = os.cpu_count() or 1
        denom = max(1, args.num_workers * args.cams_parallel)
        args.encoder_threads = max(1, cores // denom)
        print(f"[info] auto encoder_threads = {args.encoder_threads} "
              f"(cpu={cores} / (workers={args.num_workers} × cams={args.cams_parallel}))")

    # Build encoder options dict with CLI overrides. Pass through workers via
    # shared_kwargs so spawn-pool processes see the user's preset choice.
    encoder_options_override: dict[str, str] | None = None
    if encoder_name == "libx264" and args.libx264_preset != ENCODER_OPTIONS["libx264"]["preset"]:
        encoder_options_override = dict(ENCODER_OPTIONS["libx264"])
        encoder_options_override["preset"] = args.libx264_preset
        print(f"[info] libx264 preset override: {args.libx264_preset}")

    out: Path = args.output_dir
    if out.exists():
        if args.overwrite:
            shutil.rmtree(out)
        elif any(out.iterdir()):
            sys.exit(f"{out} exists and is non-empty (use --overwrite)")
    out.mkdir(parents=True, exist_ok=True)

    ds = lance.dataset(str(args.lance.resolve()))
    meta = read_dataset_metadata(ds)
    required_meta = [
        "lerobot:fps", "lerobot:tasks_json",
        "video_lance:image_height", "video_lance:image_width",
        "master_joint_names", "slave_joint_names",
    ]
    missing = [k for k in required_meta if k not in meta]
    if missing:
        sys.exit(
            f"source lance is missing required schema metadata: {missing}\n"
            f"  (was it produced by build_training_lance.py?)"
        )

    fps = int(meta["lerobot:fps"])
    image_height = int(meta["video_lance:image_height"])
    image_width = int(meta["video_lance:image_width"])
    tasks_json = json.loads(meta["lerobot:tasks_json"])

    slave_joints = parse_joint_names(meta["slave_joint_names"])
    if len(slave_joints) % 2 != 0:
        sys.exit(f"slave_joint_names yielded {len(slave_joints)} joints; expected an even count")
    half = len(slave_joints) // 2
    state_names = (
        [f"left_{n}" for n in slave_joints[:half]]
        + [f"right_{n}" for n in slave_joints[half:]]
    )

    schema_names = {f.name for f in ds.schema}
    cam_blob_cols = [c for c in CAM_FEATURE_KEYS if c in schema_names]
    if not cam_blob_cols:
        sys.exit(f"no camera blob columns found in source lance (looked for {list(CAM_FEATURE_KEYS)})")
    # `action_source` is HIL-only metadata; pure teleop datasets don't have it.
    has_action_source = "action_source" in schema_names

    print(f"[info] cameras: {cam_blob_cols}")
    print(f"[info] fps={fps}, image={image_width}x{image_height}, "
          f"state_dim={len(state_names)}, tasks={len(tasks_json)}, "
          f"action_source={'yes' if has_action_source else 'no'}, "
          f"encoder={encoder_name}, num_workers={args.num_workers}, "
          f"cams_parallel={args.cams_parallel}, "
          f"encoder_threads={args.encoder_threads}, "
          f"decoder_threads={args.decoder_threads}, "
          f"image_sample_stride={args.image_sample_stride}")

    all_eps = sorted(set(
        ds.to_table(columns=["episode_index"])["episode_index"].to_pylist()
    ))
    if args.episodes:
        wanted = parse_episodes_spec(args.episodes)
        eps_to_convert = [e for e in all_eps if e in wanted]
        missing_eps = wanted - set(all_eps)
        if missing_eps:
            print(f"[warn] requested episodes not in source: {sorted(missing_eps)}")
    else:
        eps_to_convert = all_eps
    if not eps_to_convert:
        sys.exit("no episodes selected")

    print(f"[info] {len(eps_to_convert)} of {len(all_eps)} episodes -> {out}")

    # One global pre-scan for (cam, ep, gop) -> first global row index. Cheap
    # vs the per-episode video re-encode that follows.
    print("[info] building global GOP -> row-index lookup ...")
    gop_lookup = build_global_gop_lookup(ds, cam_blob_cols)
    print(f"[info] gop_lookup entries: {len(gop_lookup)}")

    # Pre-compute episode lengths so we can derive deterministic
    # global_index_offset for each episode regardless of completion order
    # (parallel workers may finish out-of-order). One scan over the
    # episode_index column + Counter; previously this was N separate
    # count_rows(filter=...) calls which was O(N) FUSE round-trips on
    # JuiceFS — the dominant bottleneck for large datasets (200 ep was
    # ~40-100 s of pre-scan vs ~1 s now).
    print("[info] pre-computing per-episode row counts for index offsets ...")
    from collections import Counter
    ep_index_pylist = ds.to_table(columns=["episode_index"])["episode_index"].to_pylist()
    ep_counts = Counter(int(e) for e in ep_index_pylist)
    ep_lengths = {src_ep: ep_counts[src_ep] for src_ep in eps_to_convert}
    ep_offsets: dict[int, int] = {}
    running = 0
    for dst_idx, src_ep in enumerate(eps_to_convert):
        ep_offsets[src_ep] = running
        running += ep_lengths[src_ep]

    # kwargs that are identical for every episode worker.
    shared_kwargs = dict(
        output_dir=out,
        fps=fps,
        image_height=image_height,
        image_width=image_width,
        image_sample_stride=args.image_sample_stride,
        cam_blob_cols=cam_blob_cols,
        gop_lookup=gop_lookup,
        has_action_source=has_action_source,
        encoder_name=encoder_name,
        encoder_threads=args.encoder_threads,
        decoder_threads=args.decoder_threads,
        cams_parallel=args.cams_parallel,
        encoder_options_override=encoder_options_override,
    )

    n = len(eps_to_convert)
    results_by_dst: dict[int, EpisodeResult] = {}
    dims_by_dst: dict[int, dict[str, tuple[int, int]]] = {}

    if args.num_workers > 1:
        # Parallel path. mp.spawn is required because lance is fork-unsafe.
        lance_path = str(args.lance.resolve())
        worker_args = [
            (lance_path, src_ep, dst_idx, ep_offsets[src_ep], shared_kwargs)
            for dst_idx, src_ep in enumerate(eps_to_convert)
        ]
        ctx = mp.get_context("spawn")
        completed = 0
        with ctx.Pool(args.num_workers) as pool:
            for dst_idx, result, dims in pool.imap_unordered(
                _convert_episode_worker, worker_args, chunksize=1
            ):
                completed += 1
                results_by_dst[dst_idx] = result
                dims_by_dst[dst_idx] = dims
                print(f"  [{completed}/{n}] dst_idx={dst_idx} done "
                      f"({result.length} rows)", flush=True)
    else:
        # Serial path (preserves original behavior bit-for-bit).
        for dst_idx, src_ep in enumerate(eps_to_convert):
            print(f"  [{dst_idx + 1}/{n}] src_ep={src_ep} -> "
                  f"episode_{dst_idx:06d}", flush=True)
            result, dims = convert_episode(
                ds=ds,
                src_episode_index=src_ep,
                dst_episode_index=dst_idx,
                global_index_offset=ep_offsets[src_ep],
                **shared_kwargs,
            )
            results_by_dst[dst_idx] = result
            dims_by_dst[dst_idx] = dims

    # Re-order results by dst_idx (parallel completion may be out-of-order).
    results: list[EpisodeResult] = [results_by_dst[i] for i in range(n)]
    sample_dims: dict[str, tuple[int, int]] = {}
    image_stats_present = False
    for i in range(n):
        sample_dims.update(dims_by_dst[i])
        if any(CAM_FEATURE_KEYS[c] in results_by_dst[i].stats for c in cam_blob_cols):
            image_stats_present = True

    write_meta(
        output_dir=out,
        repo_id=args.repo_id,
        fps=fps,
        episodes=results,
        tasks_json=tasks_json,
        sample_dims=sample_dims,
        cam_blob_cols=cam_blob_cols,
        state_names=state_names,
        image_stats_present=image_stats_present,
        has_action_source=has_action_source,
        encoder_name=encoder_name,
    )
    total_frames = sum(r.length for r in results)
    print(f"[done] {len(results)} episodes, {total_frames} frames, fps={fps} -> {out}")


if __name__ == "__main__":
    main()
