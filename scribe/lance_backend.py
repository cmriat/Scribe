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
import re
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


# ---------------------------------------------------------------------------
# URI helpers — dual-mode (local path / bos:// / s3://). Mirrors the pattern
# used in airbot_play_ws/scripts/build_training_lance.py; intentionally copied
# rather than shared to keep Scribe self-contained.
# ---------------------------------------------------------------------------

LanceRoot = "Path | str"  # documentation only; runtime accepts either


def _is_remote_uri(value: object) -> bool:
    return isinstance(value, str) and value.startswith(("s3://", "bos://"))


def _lance_uri(root) -> str:
    """Return a string that Lance's object_store can open.

    Lance's object_store registers schemes ``s3 / s3+ddb / gs / az / abfss /
    cos / oss / hf / file / memory`` — ``bos`` is not one of them, even though
    BOS is S3-compatible. We rewrite ``bos://`` to ``s3://``; the actual
    endpoint is taken from ``AWS_ENDPOINT_URL`` (pointed at BOS) so the
    network request still hits the right service. For local paths we just
    stringify.
    """
    if _is_remote_uri(root):
        return "s3://" + root[len("bos://") :] if root.startswith("bos://") else root
    return str(root)


def _root_str(root) -> str:
    """Stringify a root that may be a Path or a URI."""
    return root if isinstance(root, str) else str(root)


def _root_basename(root) -> str:
    """Last URI segment / Path name, with any trailing slash stripped."""
    s = _root_str(root).rstrip("/")
    return s.rsplit("/", 1)[-1] if "/" in s else s


def _root_normalize(root):
    """Resolve a local path; leave a URI untouched (after stripping trailing /)."""
    if _is_remote_uri(root):
        return root.rstrip("/")
    return Path(root).resolve()


def _root_versions_present(root) -> bool:
    """True if root looks like a Lance dataset (has a ``_versions/`` subdir).

    For URIs we go through fsspec; the call is one HEAD/LIST and fails closed
    (returns False) on any error so the caller can fall back to per-episode
    discovery.
    """
    if _is_remote_uri(root):
        try:
            import fsspec

            # Route bos:// through s3:// for s3fs's URL parser; see
            # bos_discovery._to_fsspec_uri for the rationale.
            fsspec_uri = "s3://" + root[len("bos://") :] if root.startswith("bos://") else root
            fs, fs_path = fsspec.core.url_to_fs(fsspec_uri)
            return bool(fs.exists(fs_path.rstrip("/") + "/_versions"))
        except (OSError, FileNotFoundError, PermissionError) as exc:
            logger.debug("URI _versions probe failed for %s: %s", root, exc)
            return False
    p = Path(root)
    return p.is_dir() and (p / "_versions").exists()


# Cache namespace policy slugs. Coexist on disk so switching policies doesn't
# clobber prior caches (rollback-safe).
#   copy     — `-c:v copy` remux. Preserves source bitrate (fast, same size as source).
#   reencode — `libx264 -preset fast -crf 23 -bf 0 -fps_mode passthrough` with
#              ffprobe frame-count assertion before publish. Typical 7-20× smaller
#              MP4s for sources that were recorded with `speed-preset=ultrafast`
#              + no CRF (high source bitrate). Falls back to copy on validation
#              failure so frame mapping is never silently broken.
_VIDEO_CACHE_VERSIONS = {
    "copy": "h264copy_v1",
    "reencode": "h264reencode_crf23_v1",
}
# Default = copy (matches §5.1 invariant). HIL / slow-network users opt in via
# `LANCE_VIDEO_POLICY=reencode` env var.
_DEFAULT_VIDEO_POLICY = "copy"

# Backwards-compat alias for any external callers.
VIDEO_CACHE_VERSION = _VIDEO_CACHE_VERSIONS[_DEFAULT_VIDEO_POLICY]


def _video_policy() -> str:
    raw = (os.environ.get("LANCE_VIDEO_POLICY") or _DEFAULT_VIDEO_POLICY).strip().lower()
    if raw not in _VIDEO_CACHE_VERSIONS:
        logger.warning(
            "unknown LANCE_VIDEO_POLICY=%r (allowed: %s); using %s",
            raw,
            sorted(_VIDEO_CACHE_VERSIONS),
            _DEFAULT_VIDEO_POLICY,
        )
        return _DEFAULT_VIDEO_POLICY
    return raw


def _video_cache_version() -> str:
    return _VIDEO_CACHE_VERSIONS[_video_policy()]


@dataclass(frozen=True)
class _GopLayout:
    first_indices: list[int]
    video_frame_indices: np.ndarray


def _dataset_runtime_namespace(root, repo_id: str) -> str:
    """Build a stable, filesystem-safe runtime namespace for one lance dataset.

    The trailing slug is the *current policy's* cache version, so MP4s from
    `copy` and `reencode` policies live in distinct directories — no collision,
    no half-mixed cache.

    ``root`` may be a local ``Path`` or a ``bos://`` / ``s3://`` URI; both are
    hashed in their canonical (normalised) form so two callers passing the
    same dataset by different surface representations still land in the same
    runtime namespace.
    """
    canonical = _root_normalize(root)
    canonical_str = _root_str(canonical)
    name = _root_basename(canonical_str)
    stem = name[: -len(LANCE_SUFFIX)] if name.endswith(LANCE_SUFFIX) else name
    slug = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in stem).strip("_") or "dataset"
    digest = hashlib.sha1(f"{canonical_str}|{repo_id}".encode("utf-8")).hexdigest()[:12]
    return f"{slug}_{digest}_{_video_cache_version()}"


def _ffprobe_frame_count(path: Path) -> int | None:
    """Count actual decoded frames in a video file (not the metadata `nb_frames`,
    which is unreliable). Returns None on probe failure (treat as assertion fail
    upstream)."""
    try:
        r = subprocess.run(
            [
                FFPROBE_BIN,
                "-v",
                "error",
                "-count_frames",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=nb_read_frames",
                "-of",
                "default=nokey=1:noprint_wrappers=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if r.returncode != 0:
            return None
        return int(r.stdout.strip())
    except (subprocess.SubprocessError, ValueError):
        return None


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
# "lance_col": direct column copy (possibly list→2D stack). The 2nd element is
#              the *logical* source name as it appears in raw per-episode data
#              (master_position / slave_position / mid / etc.); merged data uses
#              renamed columns and is handled via column aliases — see below.
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
# Merged dataset (single .lance for many episodes) support
# -----------------------------------------------------------------------------
# build_training_lance.py output has lerobot-renamed columns (`action`,
# `observation_state`, `observation_images_cam_*`) and packs all episodes into
# one .lance, distinguishing them by the `episode_index` column. A signature
# of three columns identifies this format. We then expose each episode as a
# read-only "view" into the shared dataset, translating logical column names
# (used by FEATURE_SOURCE and the rest of this module) back to actual schema
# columns at read time.

LANCE_MERGED_SIGNATURE_COLS = frozenset({"action", "observation_state", "episode_index"})

# Required logical scalar columns for a viable Lance episode (raw OR merged).
# Used to fail loudly at construction if a column is missing — replaces the
# silent zero-fill that the previous implementation degraded to.
_REQUIRED_LOGICAL_SCALAR_COLS = (
    "master_position",
    "slave_position",
    "timestamp",
)

_CAMERA_LOGICAL_TO_LEROBOT = {
    "mid": "observation_images_cam_env",
    "left": "observation_images_cam_left_wrist",
    "right": "observation_images_cam_right_wrist",
}


def _merged_col_aliases() -> dict[str, str]:
    """Logical-name → actual-schema-name map for merged-pipeline output.

    Logical names are the ones the rest of this module already uses (so the
    raw-mode call sites don't need to learn new spellings); actual names are
    what build_training_lance.py wrote to the schema.
    """
    aliases = {
        "master_position": "action",
        "slave_position": "observation_state",
        "master_velocity": "action_velocity",
        "master_effort": "action_effort",
        "slave_velocity": "observation_velocity",
        "slave_effort": "observation_effort",
    }
    for cam_logical, cam_lerobot in _CAMERA_LOGICAL_TO_LEROBOT.items():
        # Blob column itself
        aliases[cam_logical] = cam_lerobot
        # GOP companion columns (renamed in lockstep by build_training_lance)
        aliases[f"{cam_logical}_gop_index"] = f"{cam_lerobot}_gop_index"
        aliases[f"{cam_logical}_frame_index_in_gop"] = f"{cam_lerobot}_frame_index_in_gop"
    return aliases


def _is_merged_lance(ds: lance.LanceDataset) -> bool:
    """True if the dataset's schema has the merged-pipeline signature columns."""
    return LANCE_MERGED_SIGNATURE_COLS <= {f.name for f in ds.schema}


def _aggregate_action_source_spans(values: list[str]) -> dict:
    """Compress a per-row list of action_source labels into contiguous spans.

    Used by HIL viz: the `action_source` column flips between values like
    ``"VLA_MODE"`` / ``"HUMAN_MODE"`` over an episode; the timeline track
    only needs the run-length-encoded view (typically <100 spans / episode
    even for heavily-alternating data). Returns ``{"spans": [...], "summary": {...}}``.
    """
    if not values:
        return {"spans": [], "summary": {}}
    spans: list[dict] = []
    summary: dict[str, int] = {}
    cur_mode = values[0]
    cur_start = 0
    for i in range(1, len(values)):
        if values[i] != cur_mode:
            spans.append({"start": cur_start, "end": i - 1, "mode": cur_mode})
            summary[cur_mode] = summary.get(cur_mode, 0) + (i - cur_start)
            cur_mode = values[i]
            cur_start = i
    spans.append({"start": cur_start, "end": len(values) - 1, "mode": cur_mode})
    summary[cur_mode] = summary.get(cur_mode, 0) + (len(values) - cur_start)
    return {"spans": spans, "summary": summary}


def _split_episodes_by_index(ds: lance.LanceDataset) -> list[tuple[int, int, int]]:
    """Group rows by the `episode_index` column, return [(ep_id, row_start, row_stop)] sorted by ep_id.

    Assumes rows for one episode are contiguous within the dataset — which
    build_training_lance.py guarantees (writes one fragment per episode in
    sequence). This walks contiguous runs of the episode_index column rather
    than doing a boolean mask per episode, so it's O(rows) once.
    """
    arr = ds.to_table(columns=["episode_index"])["episode_index"].to_numpy()
    if len(arr) == 0:
        return []
    # Boundaries are positions where consecutive values differ.
    boundaries = np.flatnonzero(np.diff(arr)) + 1
    starts = np.concatenate(([0], boundaries))
    stops = np.concatenate((boundaries, [len(arr)]))
    ids = [int(arr[s]) for s in starts]
    # Reject interleaved episode_index — happens if someone manually concatenated
    # multiple datasets without re-sorting. We can't visualize that correctly
    # because each episode would be split into multiple disjoint chunks; one
    # contiguous run per episode_index is the only assumption that holds.
    if len(ids) != len(set(ids)):
        from collections import Counter

        repeats = [k for k, v in Counter(ids).items() if v > 1]
        raise RuntimeError(
            f"merged .lance has interleaved episode_index column: episode "
            f"id(s) {repeats[:5]}{'…' if len(repeats) > 5 else ''} appear in "
            f"multiple non-contiguous runs. Re-build the dataset with "
            f"contiguous per-episode rows before visualizing."
        )
    out = [(ids[i], int(starts[i]), int(stops[i])) for i in range(len(ids))]
    out.sort(key=lambda t: t[0])
    return out


# -----------------------------------------------------------------------------
# Discovery
# -----------------------------------------------------------------------------


def is_lance_root(path) -> bool:
    """True if `path` is a `.lance` directory or a dir containing episode_*.lance.

    Local-only. URI mode is handled by the caller in ``app.py``, which already
    knows the dataset is on BOS because the user passed ``--bos-prefix``; we
    keep this helper Path-only to avoid surprising network traffic during
    Scribe's CLI argument parsing.
    """
    if path is None or _is_remote_uri(path):
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


def discover_lance_episodes(root) -> list:
    """Return episode `.lance` entries (Path or URI str) in stable sorted order.

    Three shapes are supported:

    * local ``Path`` ending in ``.lance``      → return ``[root]``
    * local directory of ``episode_*.lance``   → return sorted ``Path`` list
    * ``bos://``/``s3://`` URI in either form → return sorted ``str`` list

    Per the team convention the raw-per-episode form is matched **only** as
    ``episode_<digits>.lance`` (the merge pipeline names episodes this way and
    other prefixes belong to merged outputs whose top-level is already a single
    ``.lance``).
    """
    if _is_remote_uri(root):
        return _discover_episodes_remote(root)
    root = Path(root)
    if root.suffix == LANCE_SUFFIX:
        return [root]
    children = sorted(
        (p for p in root.iterdir() if p.is_dir() and p.suffix == LANCE_SUFFIX),
        key=lambda p: p.name,
    )
    return children


_EPISODE_LANCE_RE = re.compile(r"^episode_\d+\.lance$")


def _discover_episodes_remote(root: str) -> list[str]:
    root = root.rstrip("/")
    # Single merged .lance? `discover` returns just it so downstream code can
    # treat the result uniformly. LanceDataset still calls _is_merged_lance
    # afterwards to decide between merged-view vs per-episode handling.
    name = _root_basename(root)
    if name.endswith(LANCE_SUFFIX):
        return [root]

    import fsspec

    scheme = root.split("://", 1)[0]
    # s3fs's URL parser doesn't recognise bos://; rewrite for the fsspec call
    # while keeping the original scheme on the returned URIs (Lance accepts both).
    fsspec_uri = "s3://" + root[len("bos://") :] if root.startswith("bos://") else root
    fs, fs_path = fsspec.core.url_to_fs(fsspec_uri)
    matched: list[str] = []
    for entry in fs.ls(fs_path, detail=False):
        ename = entry.rstrip("/").rsplit("/", 1)[-1]
        if _EPISODE_LANCE_RE.match(ename):
            matched.append(f"{scheme}://{entry.rstrip('/')}")
    matched.sort(key=lambda s: s.rsplit("/", 1)[-1])
    return matched


# -----------------------------------------------------------------------------
# Per-episode lance wrapper
# -----------------------------------------------------------------------------


class _EpisodeLance:
    """One Lance episode with lazy metadata/dataframe caching.

    Supports two modes via the constructor classmethods:

    * `from_path(path)` — raw per-episode `.lance` (one dataset per file).
      `_row_offset = 0`, `_col_aliases = {}` (identity translation).

    * `from_merged_view(...)` — view into a merged multi-episode `.lance`
      shared across many `_EpisodeLance` siblings. `_row_offset` is the
      episode's first global row in the shared dataset; `_col_aliases` maps
      logical names this module already uses (`master_position`, `mid`, …)
      to the actual schema column names produced by build_training_lance
      (`action`, `observation_images_cam_env`, …). The rest of `_EpisodeLance`
      always speaks in logical names; translation happens at every call site
      that touches the lance schema.
    """

    def __init__(
        self,
        path: Path | None,
        *,
        shared_ds: lance.LanceDataset | None = None,
        row_offset: int = 0,
        row_count: int | None = None,
        col_aliases: dict[str, str] | None = None,
    ):
        if path is not None and shared_ds is None:
            if _is_remote_uri(path):
                # Keep the user-facing URI on self.path (may be bos://) but
                # hand Lance's object_store a scheme it actually registers.
                self.path = path.rstrip("/")
                self._ds = lance.dataset(_lance_uri(self.path))
            else:
                self.path = Path(path).resolve()
                self._ds = lance.dataset(str(self.path))
            self._row_offset = 0
            self._row_count = int(self._ds.count_rows())
            self._col_aliases: dict[str, str] = {}
        elif shared_ds is not None and path is None:
            self.path = None
            self._ds = shared_ds
            self._row_offset = int(row_offset)
            assert row_count is not None, "row_count required for merged view"
            self._row_count = int(row_count)
            self._col_aliases = dict(col_aliases or {})
        else:
            raise TypeError(
                "_EpisodeLance: pass exactly one of `path` (raw mode) or "
                "`shared_ds` + row_offset/row_count/col_aliases (merged view)"
            )
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
        # Pre-build the inverse alias map (actual → logical) for non_blob_columns.
        self._inv_aliases: dict[str, str] = {v: k for k, v in self._col_aliases.items()}

    @classmethod
    def from_path(cls, path) -> "_EpisodeLance":
        """Raw mode: one .lance file = one episode. ``path`` may be a local
        ``Path`` or a ``bos://``/``s3://`` URI string."""
        return cls(path)

    @classmethod
    def from_merged_view(
        cls,
        shared_ds: lance.LanceDataset,
        row_offset: int,
        row_count: int,
        col_aliases: dict[str, str],
    ) -> "_EpisodeLance":
        """Merged-view mode: read [row_offset, row_offset+row_count) from a shared
        multi-episode dataset, using `col_aliases` to translate logical column
        names (master_position / mid / …) to the schema's actual names."""
        return cls(
            None,
            shared_ds=shared_ds,
            row_offset=row_offset,
            row_count=row_count,
            col_aliases=col_aliases,
        )

    def _actual(self, logical: str) -> str:
        """Logical column name → actual schema column name."""
        return self._col_aliases.get(logical, logical)

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
                # Read from this episode's first row (merged view: row_offset > 0).
                tbl = self._ds.to_table(columns=["language_instruction"], limit=1, offset=self._row_offset)
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
        """Logical (caller-facing) names of non-blob columns.

        Raw mode: identity map — actual schema names are returned.
        Merged view: actual schema names are translated back to the logical
        names this module uses (e.g. `action` → `master_position`), so callers
        looking up `master_position` find it.
        """
        actual = [f.name for f in self._ds.schema if "blob" not in str(f.type).lower()]
        if not self._inv_aliases:
            return actual
        return [self._inv_aliases.get(c, c) for c in actual]

    def read_columns(self, columns: list[str], offset: int, length: int) -> pd.DataFrame:
        """Read a row range for the given non-blob columns (logical names).

        Caller's `offset` is episode-relative; we add `_row_offset` for merged
        views. Returned DataFrame has caller's logical column names (we rename
        from actual schema names for merged mode).
        """
        key = (offset, length, tuple(columns))
        with self._cache_lock:
            if key in self._slice_cache:
                return self._slice_cache[key]
        actual_cols = [self._actual(c) for c in columns]
        tbl = self._ds.to_table(
            columns=list(actual_cols),
            limit=length,
            offset=self._row_offset + offset,
        )
        df = tbl.to_pandas()
        # Rename actual → logical so caller can index by `df["master_position"]`
        # regardless of whether we're in raw or merged mode.
        rename = {self._actual(c): c for c in columns if self._actual(c) != c}
        if rename:
            df = df.rename(columns=rename)
        with self._cache_lock:
            self._slice_cache[key] = df
            if len(self._slice_cache) > 8:
                self._slice_cache.pop(next(iter(self._slice_cache)))
        return df

    def _first_last(self, column: str):
        """Fetch column value at this episode's first and last rows. Cheap vs
        full scan: lance 'fullzip' encoding reads full rows but projection
        means we only touch 2 rows. `column` is a logical name."""
        if self._row_count == 0:
            return None, None
        actual = self._actual(column)
        first = self._ds.to_table(columns=[actual], limit=1, offset=self._row_offset).column(actual)[0].as_py()
        last = (
            self._ds.to_table(columns=[actual], limit=1, offset=self._row_offset + self._row_count - 1)
            .column(actual)[0]
            .as_py()
        )
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
        """Peek this episode's first GOP, return (width, height) via ffprobe. Cached.

        `cam` is a logical name ("mid"/"left"/"right"). We translate to the
        actual blob column and read this episode's first row (row_offset, not
        global row 0 — important for merged views).
        """
        if not hasattr(self, "_cam_res"):
            self._cam_res: dict[str, tuple[int, int]] = {}
        if cam in self._cam_res:
            return self._cam_res[cam]
        actual_blob = self._actual(cam)
        blob = self._ds.take_blobs(actual_blob, indices=[self._row_offset])[0].read()
        w, h = _probe_h264_wh(blob) or (640, 480)
        self._cam_res[cam] = (w, h)
        return self._cam_res[cam]

    def _gop_layout(self, cam: str) -> _GopLayout:
        """Return GOP first-row indices (this episode's local rows) and
        row→video-frame mapping for one camera."""
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
        """Write unique H264 GOP blobs to a file-like stream without joining them in memory.

        `_gop_layout.first_indices` are episode-local rows (0..row_count-1);
        for merged views we add `_row_offset` so `take_blobs` indexes the
        shared dataset correctly. The blob column itself is translated from
        logical (`mid`) to actual (`observation_images_cam_env`) for merged.
        """
        layout = self._gop_layout(cam)
        actual_blob = self._actual(cam)
        global_indices = [self._row_offset + i for i in layout.first_indices]
        blobs = self._ds.take_blobs(actual_blob, indices=global_indices)
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

    def __init__(self, repo_id: str, root, runtime_dir: Path):
        """``root`` is either a local ``Path`` or a ``bos://``/``s3://`` URI;
        ``runtime_dir`` is always local (materialised MP4s + caches live on
        local disk regardless of where the source Lance lives)."""
        self.repo_id = str(repo_id)
        self._root = _root_normalize(root)
        self._root_is_remote = _is_remote_uri(self._root)
        self._runtime_dir = Path(runtime_dir).resolve()
        self._video_root = self._runtime_dir / "videos" / _dataset_runtime_namespace(self._root, self.repo_id)
        self._video_root.mkdir(parents=True, exist_ok=True)
        self._remux_lock = threading.Lock()
        self._video_locks: dict[tuple[int, str], threading.Lock] = {}
        self._video_workers = _env_int("LANCE_VIDEO_WORKERS", default=3)

        # Detect merged-pipeline output at the root: a single .lance whose
        # schema has the {action, observation_state, episode_index} signature.
        # Otherwise fall back to the legacy raw-per-episode discovery (one
        # .lance per file).
        self._is_merged = False
        root_lance_uri = _lance_uri(self._root)
        if _root_versions_present(self._root):
            try:
                root_ds = lance.dataset(root_lance_uri)
                if _is_merged_lance(root_ds):
                    self._is_merged = True
                    self._merged_ds = root_ds
            except Exception as exc:
                logger.debug("LanceDataset: not a merged-pipeline root (%s)", exc)

        if self._is_merged:
            episodes_meta = _split_episodes_by_index(self._merged_ds)
            if not episodes_meta:
                raise RuntimeError(
                    f"{self._root}: merged-pipeline signature detected but episode_index "
                    "column produced 0 episodes — dataset may be empty or corrupted."
                )
            aliases = _merged_col_aliases()
            self._episodes = [
                _EpisodeLance.from_merged_view(
                    shared_ds=self._merged_ds,
                    row_offset=row_start,
                    row_count=row_stop - row_start,
                    col_aliases=aliases,
                )
                for (_ep_id, row_start, row_stop) in episodes_meta
            ]
            self._merged_episode_ids = [ep_id for (ep_id, _s, _e) in episodes_meta]
            # Bulk-prefetch per-episode language_instruction in one Lance `take`
            # instead of N separate to_table() round-trips (one per episode).
            # On a 88-episode dataset this saves ~250ms of cold-start time;
            # scales linearly with episode count.
            schema_names = {f.name for f in self._merged_ds.schema}
            if "language_instruction" in schema_names:
                start_rows = [v._row_offset for v in self._episodes]
                try:
                    instr_arr = self._merged_ds.take(start_rows, columns=["language_instruction"])[
                        "language_instruction"
                    ].to_pylist()
                    for view, instr in zip(self._episodes, instr_arr, strict=True):
                        view._instruction = str(instr or "")
                except Exception as exc:
                    logger.warning(
                        "LanceDataset: bulk instruction prefetch failed (%s); falling back to lazy per-episode reads.",
                        exc,
                    )
            logger.info(
                "LanceDataset: detected merged .lance under %s; %d episodes (episode_index range [%d..%d])",
                self._root,
                len(self._episodes),
                self._merged_episode_ids[0],
                self._merged_episode_ids[-1],
            )
        else:
            episode_paths = discover_lance_episodes(self._root)
            if not episode_paths:
                raise FileNotFoundError(
                    f"No .lance episodes discovered under {self._root}. "
                    "Pass either a single foo.lance directory or a directory containing episode_*.lance."
                )
            logger.info(
                "LanceDataset: discovered %d raw per-episode .lance file(s) under %s",
                len(episode_paths),
                self._root,
            )
            self._episodes = [_EpisodeLance.from_path(p) for p in episode_paths]
            self._merged_episode_ids = None

        # Per-episode row ranges (global index — Scribe-internal numbering,
        # 0..N-1 across all episodes regardless of source).
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
            raise RuntimeError(
                f"{self._root}: no recognized camera columns; expected `<cam>_frame_id` "
                "for at least one of mid/left/right."
            )

        # Schema validator: required logical columns must be present so that
        # `_read_global_range` can fill state/action without silently zeroing.
        # In merged mode this checks aliased names too (via non_blob_columns).
        avail_logical = set(probe.non_blob_columns)
        missing_logical = [c for c in _REQUIRED_LOGICAL_SCALAR_COLS if c not in avail_logical]
        if missing_logical:
            raise RuntimeError(
                f"{self._root}: required column(s) {missing_logical} missing from "
                f"schema (logical names). This dataset cannot drive the visualizer; "
                f"the previous behavior of silently filling zeros has been removed."
            )
        # Per-camera GOP companion columns must also be present, otherwise
        # video timeline alignment would silently fall back to per-row position.
        # Fail at construction time — better than crashing in `_gop_layout` on
        # the first user click.
        missing_video_cols: list[str] = []
        for cam in self._cameras:
            for col in (f"{cam}_gop_index", f"{cam}_frame_index_in_gop"):
                if col not in avail_logical:
                    missing_video_cols.append(col)
        if missing_video_cols:
            raise RuntimeError(
                f"{self._root}: required video companion column(s) {missing_video_cols} "
                f"missing for cameras {self._cameras}. Cannot drive video timeline."
            )

        # Merged datasets are pre-downsampled to camera fps; reading robot_fps()
        # from timestamps would give the camera rate (e.g. 30 Hz) anyway since
        # merged rows are anchor-frame-aligned. For raw per-episode (100 Hz),
        # robot_fps() gives ~100. Either way, the frontend uses this as "robot
        # signal sample rate", which matches the row rate of this dataset.
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
    def root(self):
        """The dataset root as the caller supplied it: ``Path`` for local,
        ``str`` (``bos://`` / ``s3://`` URI) for remote. Callers that need a
        portable identifier should use ``root_id`` below."""
        return self._root

    @property
    def root_id(self) -> str:
        """String form of ``root`` — safe to use as a cache key or in logs."""
        return _root_str(self._root)

    @property
    def root_is_remote(self) -> bool:
        return self._root_is_remote

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

    def get_episode_action_source_track(self, episode_index: int) -> dict | None:
        """Return per-row HIL `action_source` aggregated into contiguous spans.

        Format: ``{"spans": [{"start": int, "end": int, "mode": str}, ...],
        "summary": {<mode>: <row_count>, ...}}``. Both `start` and `end` are
        inclusive (0-based row indices within the episode).

        Returns ``None`` when the source schema lacks an ``action_source``
        column — i.e. legacy (pre-HIL) datasets — so the frontend can hide the
        track entirely instead of rendering a meaningless full-width span.
        """
        ep_idx = int(episode_index)
        ep = self._episodes[ep_idx]
        if "action_source" not in ep.non_blob_columns:
            return None
        df = ep.read_columns(["action_source"], offset=0, length=ep.row_count)
        values = df["action_source"].astype(str).tolist()
        return _aggregate_action_source_spans(values)

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
            policy = _video_policy()
            # Lower bound on output frame count: every row's mapped MP4 frame
            # index must be addressable. If re-encode produces fewer than this,
            # the seek mapping is broken — abort that path and fall back.
            row_indices = ep.video_frame_indices_for_rows(cam)
            min_required_frames = int(row_indices.max()) + 1 if row_indices.size else 0
            logger.info(
                "LanceDataset: materialize ep=%d cam=%s fps=%.3f policy=%s min_frames=%d -> %s",
                episode_index,
                cam,
                fps,
                policy,
                min_required_frames,
                out,
            )
            tmp = out.with_suffix(".mp4.part")
            try:
                # ── Tier 1: policy-preferred path ─────────────────────────────
                produced = False
                if policy == "reencode":
                    produced = self._materialize_reencode(
                        ep,
                        cam,
                        fps,
                        episode_index,
                        tmp,
                        min_required_frames,
                    )
                # ── Tier 2: -c:v copy remux (fast, original bitrate) ──────────
                # Always available as a safety fallback when reencode fails
                # frame-count validation, OR as the primary path under policy=copy.
                if not produced:
                    produced = self._materialize_copy(ep, cam, fps, episode_index, tmp)
                # ── Tier 3: intra-frame libx264 fallback (legacy behavior) ────
                # Only reached if both copy and (when applicable) reencode failed.
                if not produced:
                    self._materialize_intra_fallback(ep, cam, fps, episode_index, tmp)
                tmp.replace(out)
            finally:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
        return out

    def _materialize_reencode(
        self,
        ep: _EpisodeLance,
        cam: str,
        fps: float,
        episode_index: int,
        tmp: Path,
        min_required_frames: int,
    ) -> bool:
        """Re-encode source GOPs to a smaller, browser-friendly MP4.

        Frame-exact contract: `-fps_mode passthrough` keeps every input frame,
        `-bf 0` prevents B-frame reorder, and we ffprobe-count the output to
        ASSERT the frame mapping survived. On any failure (encode error or
        count below required), returns False so the caller falls back to copy.
        """
        gop_size = max(1, int(round(fps)))  # ~1 keyframe/sec; aligns with ~30Hz HIL
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
                "-map",
                "0:v:0",
                "-an",
                "-sn",
                "-dn",
                "-fps_mode",
                "passthrough",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                "-bf",
                "0",
                "-g",
                str(gop_size),
                "-keyint_min",
                str(gop_size),
                "-sc_threshold",
                "0",
                "-video_track_timescale",
                "90000",
                "-movflags",
                "+faststart",
                "-f",
                "mp4",
                str(tmp),
            ],
        )
        if r.returncode != 0:
            logger.warning(
                "reencode failed ep=%d cam=%s: %s; will fall back to copy",
                episode_index,
                cam,
                r.stderr.decode(errors="replace")[:500],
            )
            if tmp.exists():
                tmp.unlink()
            return False
        encoded = _ffprobe_frame_count(tmp)
        if encoded is None or encoded < min_required_frames:
            logger.warning(
                "reencode frame-count validation failed ep=%d cam=%s: encoded=%s "
                "< required=%d; falling back to copy to preserve seek mapping",
                episode_index,
                cam,
                encoded,
                min_required_frames,
            )
            if tmp.exists():
                tmp.unlink()
            return False
        logger.info(
            "reencode ok ep=%d cam=%s frames=%d (>= %d required)",
            episode_index,
            cam,
            encoded,
            min_required_frames,
        )
        return True

    def _materialize_copy(
        self,
        ep: _EpisodeLance,
        cam: str,
        fps: float,
        episode_index: int,
        tmp: Path,
    ) -> bool:
        """`-c:v copy` remux. Preserves source bitrate exactly. Returns True on success."""
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
            if tmp.exists():
                tmp.unlink()
            return False
        return True

    def _materialize_intra_fallback(
        self,
        ep: _EpisodeLance,
        cam: str,
        fps: float,
        episode_index: int,  # noqa: ARG002 — kept for symmetry with sibling _materialize_* helpers
        tmp: Path,
    ) -> None:
        """Last-resort intra-frame encode. Bigger files but always works.

        Raises RuntimeError if even this fails (no MP4 produced for this cam)."""
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
