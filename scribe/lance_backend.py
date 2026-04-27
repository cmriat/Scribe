"""
Lance dataset backend for Scribe.

Exposes `LanceDataset`, a duck-typed stand-in for `LeRobotDataset` that reads
from Lance files produced by `airbot_play_ws/scripts/robot_to_lance.py`.

Design:
- Robot state (master/slave × position/velocity/effort, 14-dim each) stays at
  its native rate — usually 100Hz — so Dygraph curves keep full precision.
- Cameras ship as H264 Annex B GOP blobs. On first access, concatenate the
  unique GOPs for an episode+cam and remux to MP4 via ffmpeg (no re-encode),
  landing under `runtime_dir/videos/chunk-NNN/<cam>/episode_NNNNNN.mp4`. The
  existing Flask `/local_videos/<rel>` route serves them.
- `dataset.fps` equals the detected robot sampling rate for dygraph / robot
  state. Video playback uses per-camera MP4 frame-index arrays derived from
  Lance GOP metadata instead of approximating frame alignment from robot time.
"""

from __future__ import annotations

import os
import sys
import shutil
import hashlib
import logging
import threading
import subprocess
from pathlib import Path
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

try:
    import lance
except ImportError as err:  # pragma: no cover
    raise ImportError(
        "lance is required for the lance backend. "
        "Install the pre-built wheel from airbot_play_ws/third_party/ or run "
        "`bash scripts/install_pylance.sh` there."
    ) from err


logger = logging.getLogger(__name__)


def _find_bin(name: str) -> str:
    """Locate an executable, preferring the active Python env's bin dir."""
    env_bin = Path(sys.executable).parent / name
    if env_bin.exists():
        return str(env_bin)
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(f"{name!r} not found in {env_bin} or PATH. Install ffmpeg into the pixi env.")


FFMPEG_BIN = _find_bin("ffmpeg")
FFPROBE_BIN = _find_bin("ffprobe")

LANCE_SUFFIX = ".lance"
CAMERA_KEYS_DEFAULT = ("mid", "left", "right")
VIDEO_CACHE_VERSION = "h264copy_v1"


@dataclass(frozen=True)
class _GopLayout:
    first_indices: list[int]
    video_frame_indices: np.ndarray


def _dataset_runtime_namespace(root: Path, repo_id: str) -> str:
    """Build a stable, filesystem-safe runtime namespace for one lance dataset."""
    root = Path(root).resolve()
    stem = root.stem if root.suffix == LANCE_SUFFIX else root.name
    slug = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in stem).strip("_") or "dataset"
    digest = hashlib.sha1(f"{root}|{repo_id}".encode("utf-8")).hexdigest()[:12]
    return f"{slug}_{digest}_{VIDEO_CACHE_VERSION}"


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        logger.warning("invalid %s=%r; using %d", name, raw, default)
        return default


# Feature name → how to fetch from a lance row batch.
# "lance_col": direct column copy (possibly list→2D stack).
# "derived":   computed on the fly (timestamp seconds, frame_index, etc.).
FEATURE_SOURCE = {
    "observation.state": ("lance_col", "slave_position"),
    "action": ("lance_col", "master_position"),
    "observation.velocity": ("lance_col", "slave_velocity"),
    "observation.effort": ("lance_col", "slave_effort"),
    "action.velocity": ("lance_col", "master_velocity"),
    "action.effort": ("lance_col", "master_effort"),
    "timestamp": ("derived", "timestamp"),
    "frame_index": ("derived", "frame_index"),
    "episode_index": ("derived", "episode_index"),
    "index": ("derived", "index"),
    "task_index": ("derived", "task_index"),
}


# -----------------------------------------------------------------------------
# Discovery
# -----------------------------------------------------------------------------


def is_lance_root(path: Path | str | None) -> bool:
    """True if `path` is a `.lance` directory or a dir containing episode_*.lance."""
    if path is None:
        return False
    p = Path(path)
    if not p.exists() or not p.is_dir():
        return False
    if p.suffix == LANCE_SUFFIX and (p / "_versions").exists():
        return True
    try:
        for child in p.iterdir():
            if child.is_dir() and child.suffix == LANCE_SUFFIX:
                return True
    except OSError:
        return False
    return False


def discover_lance_episodes(root: Path) -> list[Path]:
    """Return episode `.lance` directories in a stable sorted order."""
    root = Path(root)
    if root.suffix == LANCE_SUFFIX:
        return [root]
    children = sorted(
        (p for p in root.iterdir() if p.is_dir() and p.suffix == LANCE_SUFFIX),
        key=lambda p: p.name,
    )
    return children


# -----------------------------------------------------------------------------
# Per-episode lance wrapper
# -----------------------------------------------------------------------------


class _EpisodeLance:
    """One episode_*.lance file with lazy metadata/dataframe caching."""

    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self._ds = lance.dataset(str(self.path))
        self._row_count = int(self._ds.count_rows())
        raw_meta = self._ds.schema.metadata or {}
        self.schema_meta: dict[str, str] = {
            (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
            for k, v in raw_meta.items()
        }
        self._cache_lock = threading.RLock()
        self._slice_cache: dict = {}
        self._df: pd.DataFrame | None = None
        self._cam_fps: dict[str, float] = {}
        self._gop_layouts: dict[str, _GopLayout] = {}
        self._video_frame_indices: dict[str, np.ndarray] = {}
        self._instruction: str | None = None

    @property
    def row_count(self) -> int:
        return self._row_count

    @property
    def ds(self) -> lance.LanceDataset:
        return self._ds

    @property
    def cameras(self) -> list[str]:
        names = {f.name for f in self._ds.schema}
        return [c for c in CAMERA_KEYS_DEFAULT if f"{c}_frame_id" in names]

    @property
    def instruction(self) -> str:
        if self._instruction is None:
            if "language_instruction" in {f.name for f in self._ds.schema}:
                tbl = self._ds.to_table(columns=["language_instruction"], limit=1)
                self._instruction = str(tbl.column("language_instruction")[0].as_py() or "") if tbl.num_rows else ""
            else:
                self._instruction = ""
        return self._instruction

    @property
    def joint_names(self) -> tuple[list[str], list[str]]:
        master = [s for s in self.schema_meta.get("master_joint_names", "").split(",") if s]
        slave = [s for s in self.schema_meta.get("slave_joint_names", "").split(",") if s]
        return master, slave

    @property
    def non_blob_columns(self) -> list[str]:
        return [f.name for f in self._ds.schema if "blob" not in str(f.type).lower()]

    def read_columns(self, columns: list[str], offset: int, length: int) -> pd.DataFrame:
        """Read a row range for the given non-blob columns. Cached per (offset, length, cols).

        lance 'fullzip' encoding reads full rows, but projecting to a few scalar
        columns still avoids materializing the blob bytes themselves.
        """
        key = (offset, length, tuple(columns))
        with self._cache_lock:
            if key in self._slice_cache:
                return self._slice_cache[key]
        tbl = self._ds.to_table(columns=list(columns), limit=length, offset=offset)
        df = tbl.to_pandas()
        with self._cache_lock:
            self._slice_cache[key] = df
            # tiny LRU to avoid unbounded growth
            if len(self._slice_cache) > 8:
                self._slice_cache.pop(next(iter(self._slice_cache)))
        return df

    def _first_last(self, column: str):
        """Fetch column value at row 0 and row (row_count-1). Cheap vs full scan
        because lance 'fullzip' reads whole rows — we only touch 2 rows."""
        if self._row_count == 0:
            return None, None
        first = self._ds.to_table(columns=[column], limit=1).column(column)[0].as_py()
        last = self._ds.to_table(columns=[column], limit=1, offset=self._row_count - 1).column(column)[0].as_py()
        return first, last

    def robot_fps(self) -> float:
        first, last = self._first_last("timestamp")
        if first is None or last is None or self._row_count < 2:
            return 100.0
        dur = (last - first).total_seconds()
        return (self._row_count - 1) / dur if dur > 0 else 100.0

    def camera_fps(self, cam: str) -> float:
        if cam in self._cam_fps:
            return self._cam_fps[cam]
        first_fid, last_fid = self._first_last(f"{cam}_frame_id")
        first_ts, last_ts = self._first_last(f"{cam}_timestamp_ns")
        if first_fid is None or last_fid is None or last_fid <= first_fid:
            self._cam_fps[cam] = 30.0
            return 30.0
        dur = (last_ts - first_ts).total_seconds()
        unique_frames = int(last_fid) - int(first_fid) + 1
        self._cam_fps[cam] = (unique_frames - 1) / dur if dur > 0 else 30.0
        return self._cam_fps[cam]

    def camera_resolution(self, cam: str) -> tuple[int, int]:
        """Peek first GOP, return (width, height) via ffprobe. Cached."""
        if not hasattr(self, "_cam_res"):
            self._cam_res: dict[str, tuple[int, int]] = {}
        if cam in self._cam_res:
            return self._cam_res[cam]
        blob = self._ds.take_blobs(cam, indices=[0])[0].read()
        w, h = _probe_h264_wh(blob) or (640, 480)
        self._cam_res[cam] = (w, h)
        return self._cam_res[cam]

    def _gop_layout(self, cam: str) -> _GopLayout:
        """Return GOP first-row indices and row->video-frame mapping for one camera."""
        with self._cache_lock:
            if cam in self._gop_layouts:
                return self._gop_layouts[cam]

        cols = [f"{cam}_gop_index", f"{cam}_frame_index_in_gop"]
        df = self.read_columns(cols, offset=0, length=self.row_count)
        gops = np.asarray(df[cols[0]], dtype=np.int64).reshape(-1)
        frame_in_gop = np.asarray(df[cols[1]], dtype=np.int64).reshape(-1)

        first_idx: dict[int, int] = {}
        gop_lengths: dict[int, int] = {}
        for i, (gop_index, local_frame_index) in enumerate(zip(gops, frame_in_gop, strict=True)):
            g = int(gop_index)
            if g not in first_idx:
                first_idx[g] = i
            gop_lengths[g] = max(gop_lengths.get(g, 0), int(local_frame_index) + 1)

        offsets: dict[int, int] = {}
        next_offset = 0
        for g in sorted(gop_lengths):
            offsets[g] = next_offset
            next_offset += gop_lengths[g]

        layout = _GopLayout(
            first_indices=[first_idx[g] for g in sorted(first_idx)],
            video_frame_indices=np.asarray(
                [offsets[int(g)] + int(i) for g, i in zip(gops, frame_in_gop, strict=True)],
                dtype=np.int64,
            ),
        )
        with self._cache_lock:
            self._gop_layouts[cam] = layout
            self._video_frame_indices[cam] = layout.video_frame_indices
        return layout

    def write_h264_gops(self, cam: str, stream) -> None:
        """Write unique H264 GOP blobs to a file-like stream without joining them in memory."""
        layout = self._gop_layout(cam)
        blobs = self._ds.take_blobs(cam, indices=layout.first_indices)
        for blob in blobs:
            stream.write(blob.read())

    def video_frame_indices_for_rows(self, cam: str) -> np.ndarray:
        """Map each robot row to the frame index in the materialized MP4.

        Lance stores per-row camera GOP id and frame index within that GOP. The
        MP4 is built by concatenating the unique GOP blobs in sorted GOP order,
        so this derived index is the precise browser seek target for each row.
        """
        with self._cache_lock:
            if cam in self._video_frame_indices:
                return self._video_frame_indices[cam]
        return self._gop_layout(cam).video_frame_indices


def _arm_joint_names_14() -> list[str]:
    """14-dim joint name layout: left arm 7 (joint1..6 + gripper) + right arm 7."""
    names: list[str] = []
    for arm in ("left", "right"):
        names.extend(f"{arm}_joint{i}" for i in range(1, 7))
        names.append(f"{arm}_gripper")
    return names


def _probe_h264_wh(h264_bytes: bytes) -> tuple[int, int] | None:
    """Run ffprobe on piped H264 bytes, return (width, height)."""
    try:
        r = subprocess.run(
            [
                FFPROBE_BIN,
                "-v",
                "error",
                "-f",
                "h264",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0",
                "-",
            ],
            input=h264_bytes,
            capture_output=True,
            timeout=10,
        )
        if r.returncode != 0:
            return None
        parts = r.stdout.decode().strip().split(",")
        if len(parts) >= 2:
            return int(parts[0]), int(parts[1])
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return None


# -----------------------------------------------------------------------------
# hf_dataset-like shim
# -----------------------------------------------------------------------------


class _LanceHFShim:
    """Chain-call shim mimicking the subset of HF datasets API scribe uses:

    dataset.hf_dataset.select(range(a, b)).select_columns([...]).with_format("numpy")[:]
    """

    def __init__(self, parent: "LanceDataset", rng: range | None = None, cols: list[str] | None = None):
        self._parent = parent
        self._rng = rng
        self._cols = cols

    def select(self, rng: range) -> "_LanceHFShim":
        return _LanceHFShim(self._parent, rng, self._cols)

    def select_columns(self, cols: list[str] | str) -> "_LanceHFShim":
        if isinstance(cols, str):
            cols = [cols]
        return _LanceHFShim(self._parent, self._rng, list(cols))

    def with_format(self, fmt: str) -> "_LanceHFShim":
        if fmt != "numpy":
            raise NotImplementedError(f"LanceDataset only supports numpy format, got {fmt!r}")
        return self

    def __getitem__(self, key):
        if self._rng is None:
            raise RuntimeError("LanceHFShim: call .select(range(a,b)) before indexing")
        if not isinstance(key, slice):
            raise NotImplementedError("LanceHFShim only supports [:] indexing")
        cols = self._cols or list(FEATURE_SOURCE.keys())
        return self._parent._read_global_range(self._rng.start, self._rng.stop, cols)


# -----------------------------------------------------------------------------
# meta shim
# -----------------------------------------------------------------------------


class _LanceMeta:
    """meta namespace matching LeRobotDataset.meta for attributes scribe reads."""

    def __init__(self, parent: "LanceDataset"):
        self._parent = parent
        self._version = "v2.1"

    @property
    def video_keys(self) -> list[str]:
        return [f"observation.images.{c}" for c in self._parent._cameras]

    @property
    def shapes(self) -> dict[str, tuple[int, ...]]:
        return {name: tuple(ft["shape"]) for name, ft in self._parent._features.items()}

    @property
    def episodes(self) -> list[dict]:
        return self._parent._episode_meta

    def get_video_file_path(self, episode_index: int, video_key: str) -> Path:
        # video_key comes in as "observation.images.mid" per our features; also tolerate bare name.
        cam = video_key.split(".")[-1]
        return self._parent._ensure_video(int(episode_index), cam)


# -----------------------------------------------------------------------------
# LanceDataset
# -----------------------------------------------------------------------------


class LanceDataset:
    """Duck-typed stand-in for LeRobotDataset, backed by episode `.lance` files."""

    def __init__(self, repo_id: str, root: Path, runtime_dir: Path):
        self.repo_id = str(repo_id)
        self._root = Path(root).resolve()
        self._runtime_dir = Path(runtime_dir).resolve()
        self._video_root = self._runtime_dir / "videos" / _dataset_runtime_namespace(self._root, self.repo_id)
        self._video_root.mkdir(parents=True, exist_ok=True)
        self._remux_lock = threading.Lock()
        self._video_locks: dict[tuple[int, str], threading.Lock] = {}
        self._video_workers = _env_int("LANCE_VIDEO_WORKERS", default=3)

        episode_paths = discover_lance_episodes(self._root)
        if not episode_paths:
            raise FileNotFoundError(
                f"No .lance episodes discovered under {self._root}. "
                "Pass either a single foo.lance directory or a directory containing episode_*.lance."
            )
        logger.info("LanceDataset: discovered %d episode(s) under %s", len(episode_paths), self._root)

        self._episodes: list[_EpisodeLance] = [_EpisodeLance(p) for p in episode_paths]

        # Per-episode row ranges (global index).
        lengths = [ep.row_count for ep in self._episodes]
        starts = np.concatenate(([0], np.cumsum(lengths)[:-1])).astype(np.int64)
        stops = np.cumsum(lengths).astype(np.int64)
        self._ep_starts = starts
        self._ep_stops = stops
        self._num_frames = int(stops[-1]) if len(stops) else 0
        self._num_episodes = len(self._episodes)

        # Detect fps / resolution from the first episode's first camera.
        probe = self._episodes[0]
        self._cameras: list[str] = probe.cameras
        if not self._cameras:
            raise RuntimeError(f"{episode_paths[0]}: no recognized camera columns (mid/left/right)")
        self._fps = float(round(probe.robot_fps(), 3))
        first_cam = self._cameras[0]
        self._cam_fps = float(round(probe.camera_fps(first_cam), 3))
        self._cam_wh = probe.camera_resolution(first_cam)

        # Joint names by position: 0..5 = {arm}_joint1..6, 6 = {arm}_gripper, same for right.
        # We don't rely on schema_meta names because they repeat between arms.
        self._slave_joint_short = _arm_joint_names_14()
        self._master_joint_short = _arm_joint_names_14()

        self._features = self._build_features()
        self._episode_meta = [
            {"episode_index": i, "tasks": [ep.instruction] if ep.instruction else [""], "length": ep.row_count}
            for i, ep in enumerate(self._episodes)
        ]

        self._meta = _LanceMeta(self)

    # ---- Duck-typed attributes -------------------------------------------------

    @property
    def root(self) -> Path:
        return self._root

    @property
    def video_root(self) -> Path:
        return self._video_root

    @property
    def features(self) -> dict:
        return self._features

    @property
    def fps(self) -> float:
        return self._fps

    @property
    def num_frames(self) -> int:
        return self._num_frames

    @property
    def num_episodes(self) -> int:
        return self._num_episodes

    @property
    def episode_data_index(self) -> dict[str, np.ndarray]:
        return {"from": self._ep_starts, "to": self._ep_stops}

    @property
    def meta(self) -> _LanceMeta:
        return self._meta

    @property
    def hf_dataset(self) -> _LanceHFShim:
        return _LanceHFShim(self)

    def get_episode_video_seek_info(self, episode_index: int) -> dict[str, dict[str, object]]:
        """Return per-camera MP4 frame-index arrays and fps for precise browser seeks."""
        ep_idx = int(episode_index)
        ep = self._episodes[ep_idx]
        if not ep.cameras:
            return {}
        out: dict[str, dict[str, object]] = {}
        for cam in ep.cameras:
            frame_indices = ep.video_frame_indices_for_rows(cam)
            out[f"observation.images.{cam}"] = {
                "fps": float(ep.camera_fps(cam)),
                "video_frame_indices": frame_indices.tolist(),
            }
        return out

    # ---- Row reading ----------------------------------------------------------

    def _locate_episode(self, global_start: int) -> int:
        idx = int(np.searchsorted(self._ep_stops, global_start, side="right"))
        return min(idx, self._num_episodes - 1)

    def _read_global_range(self, g_from: int, g_to: int, cols: list[str]) -> dict[str, np.ndarray]:
        ep_idx = self._locate_episode(g_from)
        ep = self._episodes[ep_idx]
        ep_start = int(self._ep_starts[ep_idx])
        ep_stop = int(self._ep_stops[ep_idx])
        if g_to > ep_stop:
            raise ValueError(
                f"LanceDataset: range [{g_from}, {g_to}) crosses episode boundary (ep {ep_idx}: [{ep_start}, {ep_stop}))"
            )
        local_from = g_from - ep_start
        local_to = g_to - ep_start
        n = local_to - local_from

        # Collect lance columns we actually need (+ always timestamp for derived).
        need_srcs: set[str] = set()
        derived_needs_ts = False
        for col in cols:
            if col not in self._features:
                raise KeyError(f"LanceDataset: feature not declared: {col}")
            kind, src = FEATURE_SOURCE[col]
            if kind == "lance_col":
                need_srcs.add(src)
            elif src == "timestamp":
                derived_needs_ts = True
        if derived_needs_ts:
            need_srcs.add("timestamp")

        available = set(ep.non_blob_columns)
        fetch_cols = [c for c in need_srcs if c in available]
        sub = ep.read_columns(fetch_cols, offset=local_from, length=n) if fetch_cols else pd.DataFrame()

        # First-row timestamp (episode-relative 0). Read once per episode, cached.
        ep_first_ts_ns: int | None = None
        if derived_needs_ts:
            if not hasattr(ep, "_first_ts_ns") or ep._first_ts_ns is None:
                first = ep._first_last("timestamp")[0]
                ep._first_ts_ns = int(first.value) if hasattr(first, "value") else int(first)
            ep_first_ts_ns = ep._first_ts_ns

        out: dict[str, np.ndarray] = {}
        for col in cols:
            kind, src = FEATURE_SOURCE[col]
            if kind == "lance_col":
                if src not in sub.columns:
                    dim = self._features[col]["shape"][0]
                    out[col] = np.zeros((n, dim), dtype=np.float32)
                    continue
                series = sub[src]
                if series.dtype == object:
                    arr = np.vstack(series.values).astype(np.float32)
                else:
                    arr = series.to_numpy(dtype=np.float32).reshape(n, -1)
                out[col] = arr
            else:
                if src == "timestamp":
                    ts = sub["timestamp"].astype("int64").to_numpy()
                    out[col] = ((ts - ep_first_ts_ns).astype(np.float64) / 1e9).astype(np.float32)
                elif src == "frame_index":
                    out[col] = np.arange(local_from, local_to, dtype=np.int64)
                elif src == "episode_index":
                    out[col] = np.full(n, ep_idx, dtype=np.int64)
                elif src == "index":
                    out[col] = np.arange(g_from, g_to, dtype=np.int64)
                elif src == "task_index":
                    out[col] = np.zeros(n, dtype=np.int64)
        return out

    # ---- Video materialization ------------------------------------------------

    def _video_path(self, episode_index: int, cam: str) -> Path:
        chunk = episode_index // 1000
        return (
            self._video_root / f"chunk-{chunk:03d}" / f"observation.images.{cam}" / f"episode_{episode_index:06d}.mp4"
        )

    def _ensure_video(self, episode_index: int, cam: str) -> Path:
        out = self._video_path(episode_index, cam)
        if out.exists() and out.stat().st_size > 0:
            return out
        # Per-video lock so different cameras can encode in parallel.
        lock_key = (episode_index, cam)
        with self._remux_lock:
            if lock_key not in self._video_locks:
                self._video_locks[lock_key] = threading.Lock()
            vlock = self._video_locks[lock_key]
        with vlock:
            if out.exists() and out.stat().st_size > 0:
                return out
            out.parent.mkdir(parents=True, exist_ok=True)
            ep = self._episodes[episode_index]
            fps = ep.camera_fps(cam)
            logger.info("LanceDataset: remuxing ep=%d cam=%s fps=%.3f -> %s", episode_index, cam, fps, out)
            tmp = out.with_suffix(".mp4.part")
            try:
                r = self._run_ffmpeg_with_episode_stream(
                    ep,
                    cam,
                    [
                        FFMPEG_BIN,
                        "-y",
                        "-loglevel",
                        "error",
                        "-fflags",
                        "+genpts",
                        "-f",
                        "h264",
                        "-framerate",
                        f"{fps:.6f}",
                        "-i",
                        "-",
                        "-c:v",
                        "copy",
                        "-movflags",
                        "+faststart",
                        "-f",
                        "mp4",
                        str(tmp),
                    ],
                )
                if r.returncode != 0:
                    logger.warning(
                        "copy remux failed ep=%d cam=%s: %s; falling back to intra-frame encode",
                        episode_index,
                        cam,
                        r.stderr.decode(errors="replace")[:500],
                    )
                    r = self._run_ffmpeg_with_episode_stream(
                        ep,
                        cam,
                        [
                            FFMPEG_BIN,
                            "-y",
                            "-loglevel",
                            "error",
                            "-f",
                            "h264",
                            "-framerate",
                            f"{fps:.6f}",
                            "-i",
                            "-",
                            "-c:v",
                            "libx264",
                            "-preset",
                            "ultrafast",
                            "-crf",
                            "18",
                            "-g",
                            "1",
                            "-movflags",
                            "+faststart",
                            "-f",
                            "mp4",
                            str(tmp),
                        ],
                    )
                    if r.returncode != 0:
                        raise RuntimeError(f"ffmpeg encode failed: {r.stderr.decode(errors='replace')[:500]}")
                tmp.replace(out)
            finally:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
        return out

    @staticmethod
    def _run_ffmpeg_with_episode_stream(ep: _EpisodeLance, cam: str, cmd: list[str]) -> subprocess.CompletedProcess:
        """Run ffmpeg while streaming GOP blobs to stdin to avoid one large bytes join."""
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        stderr = b""
        try:
            assert proc.stdin is not None
            ep.write_h264_gops(cam, proc.stdin)
            proc.stdin.close()
            assert proc.stderr is not None
            stderr = proc.stderr.read()
            returncode = proc.wait()
            return subprocess.CompletedProcess(cmd, returncode, stdout=b"", stderr=stderr)
        except BrokenPipeError:
            if proc.stderr is not None:
                stderr = proc.stderr.read()
            returncode = proc.wait()
            return subprocess.CompletedProcess(cmd, returncode, stdout=b"", stderr=stderr)
        except Exception:
            proc.kill()
            proc.wait()
            raise

    def _preload_videos(self, episode_index: int) -> None:
        """Materialize all cameras for an episode, with bounded concurrency."""
        errors: list[tuple[str, Exception]] = []

        def _encode(cam: str):
            try:
                self._ensure_video(episode_index, cam)
            except Exception as exc:
                errors.append((cam, exc))
                logger.warning("preload failed ep=%d cam=%s", episode_index, cam, exc_info=True)

        max_workers = min(len(self._cameras), self._video_workers)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            list(pool.map(_encode, self._cameras))
        if errors:
            failed_cameras = ", ".join(cam for cam, _ in errors)
            raise RuntimeError(
                f"video materialization failed for episode {episode_index}: {failed_cameras}"
            ) from errors[0][1]

    # ---- Feature dict construction -------------------------------------------

    def _build_features(self) -> dict:
        features: dict[str, dict] = {
            "observation.state": {
                "dtype": "float32",
                "shape": (14,),
                "names": list(self._slave_joint_short),
            },
            "action": {
                "dtype": "float32",
                "shape": (14,),
                "names": list(self._master_joint_short),
            },
            "observation.velocity": {
                "dtype": "float32",
                "shape": (14,),
                "names": list(self._slave_joint_short),
            },
            "observation.effort": {
                "dtype": "float32",
                "shape": (14,),
                "names": list(self._slave_joint_short),
            },
            "action.velocity": {
                "dtype": "float32",
                "shape": (14,),
                "names": list(self._master_joint_short),
            },
            "action.effort": {
                "dtype": "float32",
                "shape": (14,),
                "names": list(self._master_joint_short),
            },
            "timestamp": {"dtype": "float32", "shape": (1,), "names": None},
            "frame_index": {"dtype": "int64", "shape": (1,), "names": None},
            "episode_index": {"dtype": "int64", "shape": (1,), "names": None},
            "index": {"dtype": "int64", "shape": (1,), "names": None},
            "task_index": {"dtype": "int64", "shape": (1,), "names": None},
        }
        w, h = self._cam_wh
        for cam in self._cameras:
            features[f"observation.images.{cam}"] = {
                "dtype": "video",
                "shape": (h, w, 3),
                "names": ["height", "width", "channels"],
                "info": {
                    "video.fps": self._cam_fps,
                    "video.height": h,
                    "video.width": w,
                    "video.channels": 3,
                    "video.codec": "h264",
                    "has_audio": False,
                },
            }
        return features
