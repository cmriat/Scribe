"""BOS / S3 annotation sidecar sync layer.

Scribe edits annotation sidecars purely against local files for two reasons:
(1) hot-path latency — every POST goes through a write — must stay sub-ms;
(2) the existing annotation_store CRUD is plain ``pathlib`` IO and we'd like
to keep it that way.

This module is the bridge to BOS. For each ``(namespace, name)`` dataset
opened via the landing page:

* :py:meth:`BosSync.pull` downloads the remote ``annotations/`` directory into
  the per-dataset slot under ``local_cache_root``, **once** at dataset open
  time. After that the annotation CRUD targets the local copy only.
* :py:meth:`BosSync.push` uploads everything back to BOS. Triggered by:
  - the user clicking Save (immediate),
  - the periodic :class:`AutoSaver` thread (interval configurable),
  - dataset eviction from the registry LRU,
  - process shutdown / atexit.
* Multi-user concurrency is **not** implemented (single-user MVP per the spec),
  but :py:meth:`BosSync.push` runs through ``_check_remote_changed_since`` to
  leave a single insertion point for a future ETag / mtime probe. The current
  hook just returns ``False`` (always overwrite).

The directory layout under ``local_cache_root`` is::

    <local_cache_root>/
        <namespace>__<name>/
            annotations/
                episode_curation.json
                segment_annotations.json
                frame_events.jsonl
                task_annotation_config.json
                task_annotation_config.yaml   (optional, dataset override)

We use ``__`` as the separator so existing fs-safe rules from
``_dataset_runtime_namespace`` carry over (only alnum / hyphen / underscore).
"""

from __future__ import annotations

import re
import shutil
import logging
import threading
from typing import Optional
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import field, dataclass

from scribe.bos_discovery import _to_fsspec_uri
from scribe.annotation_store import ANNOTATIONS_DIRNAME

logger = logging.getLogger(__name__)


DEFAULT_AUTOSAVE_INTERVAL_S = 60


# ---------------------------------------------------------------------------
# Slug helpers (namespace / name → filesystem-safe segment)
# ---------------------------------------------------------------------------


_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def _slug_segment(value: str) -> str:
    s = _SAFE_CHARS.sub("_", str(value).strip()).strip("_") or "_"
    # Belt-and-braces guard against pathological inputs that would still look
    # like a parent reference after sanitisation.
    if s in {".", ".."}:
        s = f"_{s}_"
    return s


def cache_slot(ns: str, name: str) -> str:
    return f"{_slug_segment(ns)}__{_slug_segment(name)}"


# ---------------------------------------------------------------------------
# State per registered dataset
# ---------------------------------------------------------------------------


@dataclass
class _DatasetState:
    uri: str
    ns: str
    name: str
    cache_dir: Path  # the dataset's slot — annotations live at cache_dir / "annotations"
    dirty: bool = False
    last_pulled_at: Optional[str] = None
    last_pushed_at: Optional[str] = None
    last_push_error: Optional[str] = None
    # Reserved for the future multi-user collision check. Populated on pull
    # so we can compare against remote mtime on next push.
    last_pull_remote_signature: Optional[str] = field(default=None)


# ---------------------------------------------------------------------------
# BosSync
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class BosSync:
    """Pull/push annotation sidecars between a BOS dataset URI and a local
    cache slot. One instance per Scribe process; thread-safe."""

    def __init__(self, local_cache_root: Path) -> None:
        self.local_cache_root = Path(local_cache_root).resolve()
        self.local_cache_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._state: dict[tuple[str, str], _DatasetState] = {}

    # -- registration --------------------------------------------------------

    def register(self, *, ns: str, name: str, uri: str) -> _DatasetState:
        """Idempotent: returns existing state if already registered, else
        creates a fresh slot dir on disk and returns the new state. Does NOT
        pull — callers do that explicitly so the timing is visible in logs."""
        key = (ns, name)
        with self._lock:
            existing = self._state.get(key)
            if existing is not None:
                if existing.uri != uri:
                    raise ValueError(
                        f"slot {ns}/{name} already registered with a different URI "
                        f"(have {existing.uri!r}, requested {uri!r})"
                    )
                return existing
            slot = self.local_cache_root / cache_slot(ns, name)
            slot.mkdir(parents=True, exist_ok=True)
            state = _DatasetState(uri=uri, ns=ns, name=name, cache_dir=slot)
            self._state[key] = state
            return state

    def unregister(self, *, ns: str, name: str) -> None:
        """Drop in-memory state. Local cache files are kept on disk so a
        re-open can short-circuit pulling."""
        with self._lock:
            self._state.pop((ns, name), None)

    def cache_dir_for(self, ns: str, name: str) -> Path:
        with self._lock:
            state = self._state.get((ns, name))
            if state is None:
                raise KeyError(f"dataset not registered: {ns}/{name}")
            return state.cache_dir

    def annotations_dir_for(self, ns: str, name: str) -> Path:
        return self.cache_dir_for(ns, name) / ANNOTATIONS_DIRNAME

    # -- dirty bits ----------------------------------------------------------

    def mark_dirty(self, ns: str, name: str) -> None:
        with self._lock:
            state = self._state.get((ns, name))
            if state is not None:
                state.dirty = True

    def is_dirty(self, ns: str, name: str) -> bool:
        with self._lock:
            state = self._state.get((ns, name))
            return bool(state and state.dirty)

    def dirty_keys(self) -> list[tuple[str, str]]:
        with self._lock:
            return [k for k, s in self._state.items() if s.dirty]

    def status(self, ns: str, name: str) -> dict:
        with self._lock:
            state = self._state.get((ns, name))
            if state is None:
                return {"registered": False}
            return {
                "registered": True,
                "uri": state.uri,
                "dirty": state.dirty,
                "last_pulled_at": state.last_pulled_at,
                "last_pushed_at": state.last_pushed_at,
                "last_push_error": state.last_push_error,
            }

    # -- pull / push ---------------------------------------------------------

    def pull(self, *, ns: str, name: str, force: bool = False) -> Path:
        """Mirror ``<uri>/annotations/`` into the local cache slot.

        - If the remote ``annotations/`` directory does not exist, we leave
          the local slot empty (or as-is). This is the normal "first-time
          opened" state.
        - If ``force=False`` and the slot already has files AND we have
          already pulled in this process, this is a no-op so users don't
          lose unsaved edits when bouncing around the LRU.
        - If ``force=True``, the existing slot is wiped before re-downloading;
          used by an explicit "reset to BOS" action (not yet wired into UI).
        """
        with self._lock:
            state = self._state.get((ns, name))
            if state is None:
                raise KeyError(f"dataset not registered: {ns}/{name}")
        annotations_dir = state.cache_dir / ANNOTATIONS_DIRNAME
        if state.last_pulled_at is not None and not force:
            logger.debug("BosSync.pull: %s/%s already pulled, skipping", ns, name)
            return annotations_dir
        if force and annotations_dir.exists():
            shutil.rmtree(annotations_dir)
        annotations_dir.mkdir(parents=True, exist_ok=True)

        remote_dir = state.uri.rstrip("/") + "/" + ANNOTATIONS_DIRNAME
        downloaded = _download_dir(remote_dir, annotations_dir)
        now = _utc_now_iso()
        with self._lock:
            state.last_pulled_at = now
            state.last_pull_remote_signature = downloaded.get("signature")
            # A fresh pull cancels any stale dirty bit.
            state.dirty = False
            state.last_push_error = None
        logger.info(
            "BosSync.pull: %s/%s downloaded %d file(s) from %s",
            ns,
            name,
            downloaded.get("count", 0),
            remote_dir,
        )
        return annotations_dir

    def push(self, *, ns: str, name: str, force: bool = False) -> bool:
        """Upload the local annotations slot back to ``<uri>/annotations/``.

        Race-safe contract:

        1. Under the lock we *clear* the dirty bit before releasing it. A
           concurrent writer that marks dirty during our upload will therefore
           set the bit on top of the cleared one and remain visible to the
           next push tick — we never silently swallow new edits made during
           the network round-trip.
        2. If the upload itself fails we re-mark dirty before returning False,
           so the next AutoSaver tick (or Save click) retries.
        3. ``state.dirty`` is the only piece of edit liveness we maintain on a
           "the upload may have lost a concurrent write" worst case we accept
           a redundant push, not a lost edit.

        Returns True on success (including the not-dirty-and-not-force no-op);
        False on upload failure. The error message is stashed on the state for
        the UI to surface.
        """
        with self._lock:
            state = self._state.get((ns, name))
            if state is None:
                raise KeyError(f"dataset not registered: {ns}/{name}")
            if not state.dirty and not force:
                return True
            uri = state.uri
            last_signature = state.last_pull_remote_signature
            # Clear early so any concurrent writer can set dirty again
            # *during* the upload without us wiping that signal afterwards.
            state.dirty = False
        if self._check_remote_changed_since(uri, last_signature):
            # Single-user MVP: log a loud warning. Future multi-user version
            # would surface a conflict dialog to the operator.
            logger.warning(
                "BosSync.push: remote %s changed since last pull; overwriting (single-user policy)",
                uri,
            )
        annotations_dir = state.cache_dir / ANNOTATIONS_DIRNAME
        remote_dir = uri.rstrip("/") + "/" + ANNOTATIONS_DIRNAME
        try:
            uploaded = _upload_dir(annotations_dir, remote_dir)
        except Exception as exc:
            logger.exception("BosSync.push: %s/%s failed", ns, name)
            with self._lock:
                # Restore dirty so the next tick retries. We OR with the
                # current value in case a concurrent writer raised the bit
                # again during the upload.
                state.dirty = True
                state.last_push_error = str(exc)
            return False
        now = _utc_now_iso()
        with self._lock:
            state.last_pushed_at = now
            state.last_push_error = None
        logger.info(
            "BosSync.push: %s/%s uploaded %d file(s) to %s",
            ns,
            name,
            uploaded.get("count", 0),
            remote_dir,
        )
        return True

    def push_all_dirty(self) -> dict[tuple[str, str], bool]:
        """Push every currently-dirty dataset. Returns per-key success."""
        results: dict[tuple[str, str], bool] = {}
        for ns, name in self.dirty_keys():
            results[(ns, name)] = self.push(ns=ns, name=name)
        return results

    # -- multi-user collision hook (stub) ------------------------------------

    def _check_remote_changed_since(
        self,
        uri: str,  # noqa: ARG002 — reserved for the future multi-user version
        last_pull_signature: Optional[str],  # noqa: ARG002 — reserved
    ) -> bool:
        """Return True if the remote ``annotations/`` has been modified by
        another writer since our last pull. Currently always False (MVP).

        Future implementation: list the remote dir, compute a signature
        (concatenated ETags or max mtime), compare against
        ``last_pull_signature``. The hook contract is intentionally
        single-method so a subclass / replacement can swap it out without
        touching push().
        """
        return False


# ---------------------------------------------------------------------------
# Directory mirror helpers (fsspec only; pure stdlib for the local side)
# ---------------------------------------------------------------------------


def _download_dir(remote_dir: str, local_dir: Path) -> dict:
    """Download every direct child file from ``remote_dir`` into ``local_dir``.

    The sidecar set is flat (no subdirs), so we list one level and stream the
    bytes. Missing remote dir is silently treated as empty.
    """
    import fsspec

    fs, fs_path = fsspec.core.url_to_fs(_to_fsspec_uri(remote_dir))
    if not fs.exists(fs_path):
        return {"count": 0, "signature": None}
    entries = fs.ls(fs_path, detail=True)
    count = 0
    sig_parts: list[str] = []
    for entry in entries:
        # fsspec returns dicts with "name", "type", "size", "ETag" / "mtime"
        if entry.get("type") != "file":
            continue
        remote_path = entry["name"]
        leaf = remote_path.rstrip("/").rsplit("/", 1)[-1]
        target = local_dir / leaf
        with fs.open(remote_path, "rb") as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        count += 1
        # Best-effort signature for the multi-user collision hook.
        tag = entry.get("ETag") or entry.get("etag") or entry.get("mtime") or ""
        sig_parts.append(f"{leaf}:{tag}")
    return {"count": count, "signature": "|".join(sig_parts) if sig_parts else None}


def _upload_dir(local_dir: Path, remote_dir: str) -> dict:
    """Upload every direct child file of ``local_dir`` into ``remote_dir``.

    Skips ``local_dir`` itself if it doesn't exist. Subdirectories are
    ignored — annotation sidecars are flat. ``remote_dir`` is created
    implicitly by writing into a path under it.
    """
    if not local_dir.exists():
        return {"count": 0}
    import fsspec

    fs, fs_remote = fsspec.core.url_to_fs(_to_fsspec_uri(remote_dir))
    count = 0
    fs.makedirs(fs_remote, exist_ok=True)
    for child in sorted(local_dir.iterdir()):
        if not child.is_file():
            continue
        target = f"{fs_remote.rstrip('/')}/{child.name}"
        with child.open("rb") as src, fs.open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
        count += 1
    return {"count": count}


# ---------------------------------------------------------------------------
# AutoSaver — background flusher
# ---------------------------------------------------------------------------


class AutoSaver:
    """Background thread that periodically pushes every dirty dataset.

    Use ``start()`` / ``stop()`` to manage the lifecycle. ``interval_s`` is
    capped to a minimum of 5s to avoid hammering BOS if someone sets a
    pathological value via env var.
    """

    def __init__(self, sync: BosSync, interval_s: int = DEFAULT_AUTOSAVE_INTERVAL_S) -> None:
        self.sync = sync
        self.interval_s = max(5, int(interval_s))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="bos-autosave", daemon=True)
        self._thread.start()
        logger.info("AutoSaver started: interval=%ds", self.interval_s)

    def stop(self, *, flush: bool = True) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2 * self.interval_s)
            self._thread = None
        if flush:
            self.sync.push_all_dirty()

    def _run(self) -> None:
        while not self._stop.is_set():
            # Sleep first so registered-but-clean datasets aren't immediately
            # pinged in the first second after startup.
            if self._stop.wait(timeout=self.interval_s):
                break
            try:
                self.sync.push_all_dirty()
            except Exception:
                logger.exception("AutoSaver tick failed; will retry next interval")


__all__ = [
    "AutoSaver",
    "BosSync",
    "DEFAULT_AUTOSAVE_INTERVAL_S",
    "cache_slot",
]
