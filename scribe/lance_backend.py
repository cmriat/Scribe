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
- `dataset.fps` equals the detected robot sampling rate. Video players seek by
  time (`frame_index / fps`); the MP4 has its own native fps and aligns in the
  time domain.
"""

from __future__ import annotations

import bisect
import logging
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable

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

LANCE_SUFFIX = ".lance"
CAMERA_KEYS_DEFAULT = ("mid", "left", "right")

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
            (k.decode() if isinstance(k, bytes) else k):
                (v.decode() if isinstance(v, bytes) else v)
            for k, v in raw_meta.items()
        }
        self._df: pd.DataFrame | None = None
        self._cam_fps: dict[str, float] = {}
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
                self._instruction = (
                    str(tbl.column("language_instruction")[0].as_py() or "") if tbl.num_rows else ""
                )
            else:
                self._instruction = ""
        return self._instruction

    @property
    def joint_names(self) -> tuple[list[str], list[str]]:
        master = [s for s in self.schema_meta.get("master_joint_names", "").split(",") if s]
        slave = [s for s in self.schema_meta.get("slave_joint_names", "").split(",") if s]
        return master, slave

    @property
    def dataframe(self) -> pd.DataFrame:
        if self._df is None:
            cols = [f.name for f in self._ds.schema if "blob" not in str(f.type).lower()]
            self._df = self._ds.to_table(columns=cols).to_pandas()
        return self._df

    def robot_fps(self) -> float:
        tbl = self._ds.to_table(columns=["timestamp"])
        ts = tbl.column("timestamp").to_pandas()
        if len(ts) < 2:
            return 100.0
        dur = (ts.iloc[-1] - ts.iloc[0]).total_seconds()
        return (len(ts) - 1) / dur if dur > 0 else 100.0

    def camera_fps(self, cam: str) -> float:
        if cam in self._cam_fps:
            return self._cam_fps[cam]
        tbl = self._ds.to_table(columns=[f"{cam}_frame_id", f"{cam}_timestamp_ns"])
        fid = tbl.column(f"{cam}_frame_id").to_numpy()
        ts = tbl.column(f"{cam}_timestamp_ns").to_pandas()
        if len(fid) == 0:
            self._cam_fps[cam] = 30.0
            return 30.0
        mask = np.concatenate(([True], fid[1:] != fid[:-1]))
        unique_ts = ts[mask].sort_values().reset_index(drop=True)
        if len(unique_ts) < 2:
            self._cam_fps[cam] = 30.0
        else:
            dur = (unique_ts.iloc[-1] - unique_ts.iloc[0]).total_seconds()
            self._cam_fps[cam] = (len(unique_ts) - 1) / dur if dur > 0 else 30.0
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

    def concatenate_gops(self, cam: str) -> bytes:
        """Return raw H264 Annex B bytes for this episode+cam (unique GOPs in order)."""
        gop_col = f"{cam}_gop_index"
        tbl = self._ds.to_table(columns=[gop_col])
        gops = tbl.column(gop_col).to_numpy()
        first_idx: dict[int, int] = {}
        for i, g in enumerate(gops):
            gi = int(g)
            if gi not in first_idx:
                first_idx[gi] = i
        ordered = sorted(first_idx.keys())
        indices = [first_idx[g] for g in ordered]
        blobs = self._ds.take_blobs(cam, indices=indices)
        return b"".join(b.read() for b in blobs)


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
                "ffprobe", "-v", "error", "-f", "h264",
                "-select_streams", "v:0", "-show_entries", "stream=width,height",
                "-of", "csv=p=0", "-",
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
        self._video_root = self._runtime_dir / "videos"
        self._video_root.mkdir(parents=True, exist_ok=True)
        self._remux_lock = threading.Lock()

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
        df = ep.dataframe
        sub = df.iloc[local_from:local_to]
        n = len(sub)

        out: dict[str, np.ndarray] = {}
        for col in cols:
            if col not in self._features:
                raise KeyError(f"LanceDataset: feature not declared: {col}")
            kind, src = FEATURE_SOURCE[col]
            if kind == "lance_col":
                if src not in sub.columns:
                    # missing velocity/effort: zero-fill to keep callers happy
                    dim = self._features[col]["shape"][0]
                    out[col] = np.zeros((n, dim), dtype=np.float32)
                    continue
                series = sub[src]
                if series.dtype == object:
                    arr = np.vstack(series.values).astype(np.float32)
                else:
                    arr = series.to_numpy(dtype=np.float32).reshape(n, -1)
                out[col] = arr
            else:  # derived
                if src == "timestamp":
                    ts = sub["timestamp"].astype("int64").to_numpy()
                    ep_first_ts = int(df["timestamp"].iloc[0].value) if hasattr(df["timestamp"].iloc[0], "value") else int(df["timestamp"].iloc[0])
                    out[col] = ((ts - ep_first_ts).astype(np.float64) / 1e9).astype(np.float32)
                elif src == "frame_index":
                    out[col] = np.arange(local_from, local_to, dtype=np.int64)
                elif src == "episode_index":
                    out[col] = np.full(n, ep_idx, dtype=np.int64)
                elif src == "index":
                    out[col] = np.arange(g_from, g_to, dtype=np.int64)
                elif src == "task_index":
                    out[col] = np.zeros(n, dtype=np.int64)
                else:  # pragma: no cover
                    raise RuntimeError(f"Unknown derived source: {src}")
        return out

    # ---- Video materialization ------------------------------------------------

    def _video_path(self, episode_index: int, cam: str) -> Path:
        chunk = episode_index // 1000
        return (
            self._video_root
            / f"chunk-{chunk:03d}"
            / f"observation.images.{cam}"
            / f"episode_{episode_index:06d}.mp4"
        )

    def _ensure_video(self, episode_index: int, cam: str) -> Path:
        out = self._video_path(episode_index, cam)
        if out.exists() and out.stat().st_size > 0:
            return out
        with self._remux_lock:
            if out.exists() and out.stat().st_size > 0:
                return out
            out.parent.mkdir(parents=True, exist_ok=True)
            ep = self._episodes[episode_index]
            fps = ep.camera_fps(cam)
            logger.info(
                "LanceDataset: remuxing ep=%d cam=%s fps=%.3f → %s",
                episode_index, cam, fps, out,
            )
            raw = ep.concatenate_gops(cam)
            tmp = out.with_suffix(".mp4.part")
            try:
                r = subprocess.run(
                    [
                        "ffmpeg", "-y", "-loglevel", "error",
                        "-f", "h264", "-framerate", f"{fps:.6f}",
                        "-i", "-",
                        "-c", "copy", "-movflags", "+faststart",
                        "-f", "mp4",
                        str(tmp),
                    ],
                    input=raw,
                    capture_output=True,
                )
                if r.returncode != 0:
                    raise RuntimeError(f"ffmpeg remux failed: {r.stderr.decode()[:500]}")
                tmp.replace(out)
            finally:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
        return out

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
