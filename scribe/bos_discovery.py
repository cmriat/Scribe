"""BOS / S3 dataset discovery for the Scribe landing page.

Given a BOS or S3 prefix, list the Lance datasets directly underneath. Two forms
are recognised, matching what `build_training_lance.py` consumes and produces:

* ``merged``       — a single ``<name>.lance`` directory (the downsample-merged
                      training output)
* ``raw_episodes`` — a directory containing ``episode_*.lance`` subdirs (the raw
                      per-episode capture format that the merge script ingests)

The listing returns one ``BosDatasetEntry`` per recognised child. A short-lived
in-process memo (``DEFAULT_CACHE_TTL_S`` seconds) avoids re-issuing the 1 + N
BOS round-trips on every landing refresh.

Authentication is taken from the standard ``AWS_*`` environment variables, the
same way ``lance.dataset(uri)`` and ``build_training_lance.py`` do. No
provider-specific code paths.
"""

from __future__ import annotations

import re
import time
import logging
import threading
from typing import Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


def _to_fsspec_uri(uri: str) -> str:
    """Rewrite ``bos://`` to ``s3://`` before handing the URI to fsspec.

    Lance's object_store accepts both schemes natively (and the team uses
    ``bos://`` interchangeably with ``s3://`` in pipelines like
    ``build_training_lance.py``). s3fs, on the other hand, parses the
    ``s3://bucket/key`` shape internally and trips on ``bos://`` — botocore
    ends up with a bucket literally named ``bos:`` and rejects it.

    We swap the scheme here at the boundary; the actual endpoint is taken
    from ``AWS_ENDPOINT_URL`` (pointed at BOS) so the network request still
    hits the right service.
    """
    if uri.startswith("bos://"):
        return "s3://" + uri[len("bos://") :]
    return uri


LANCE_SUFFIX = ".lance"
EPISODE_DIR_RE = re.compile(r"^episode_\d+\.lance$")

DEFAULT_CACHE_TTL_S = 60.0

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BosDatasetEntry:
    """One Lance dataset discovered under a BOS prefix.

    `uri` is the canonical absolute URI to hand to `LanceDataset(uri, ...)`.
    `name` is the bare last-segment label for display (and for slug derivation).
    `form` distinguishes the two layouts we know how to open.
    `episode_count` is filled for `raw_episodes` (cheap — one ls), and is None
    for `merged` (would require opening the .lance to know — not worth the RTT
    on the landing page).
    """

    uri: str
    name: str
    form: str  # "merged" | "raw_episodes"
    episode_count: Optional[int]


# ---------------------------------------------------------------------------
# URI helpers
# ---------------------------------------------------------------------------


def is_remote_uri(value: str) -> bool:
    """Match the same set of schemes Lance's object_store recognises."""
    return value.startswith(("s3://", "bos://"))


def _scheme_of(uri: str) -> str:
    return uri.split("://", 1)[0]


def _strip_trailing_slash(uri: str) -> str:
    return uri.rstrip("/")


def _basename(uri: str) -> str:
    return _strip_trailing_slash(uri).rsplit("/", 1)[-1]


def _join_uri(prefix: str, child_path: str) -> str:
    """Re-attach the URI scheme to an fsspec-returned child entry.

    fsspec strips schemes from list results (returns just ``bucket/key``), so
    callers that want to keep talking to Lance/object_store must put the
    scheme back. Matches the pattern in
    ``airbot_play_ws/scripts/build_training_lance.py:153``.
    """
    scheme = _scheme_of(prefix)
    return f"{scheme}://{child_path}"


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def _list_dir(prefix: str) -> list[str]:
    """Return absolute URIs of direct children of ``prefix``.

    Filters out the prefix itself (some object-stores echo it back) and
    obvious non-directory marker objects. Returns entries with the
    *original* scheme (so ``bos://`` input yields ``bos://`` output,
    keeping Lance's object_store happy on the downstream side).
    """
    import fsspec

    fs, fs_path = fsspec.core.url_to_fs(_to_fsspec_uri(prefix))
    children = fs.ls(fs_path, detail=False)
    out: list[str] = []
    prefix_path = fs_path.rstrip("/")
    for entry in children:
        entry_stripped = entry.rstrip("/")
        if entry_stripped == prefix_path:
            continue
        out.append(_join_uri(prefix, entry))
    return out


def _has_episode_lance_child(uri: str) -> tuple[bool, int]:
    """Return (matches, episode_count). Best-effort; failures yield (False, 0).

    We accept anything matching ``episode_\\d+\\.lance``. Lighter than opening
    each child with Lance just to confirm.
    """
    try:
        children = _list_dir(uri)
    except (OSError, FileNotFoundError, PermissionError) as exc:
        logger.debug("ls failed for %s: %s", uri, exc)
        return False, 0
    count = sum(1 for c in children if EPISODE_DIR_RE.match(_basename(c)))
    return count > 0, count


def list_bos_datasets(prefix: str) -> list[BosDatasetEntry]:
    """List recognised Lance datasets directly underneath ``prefix``.

    Cost: 1 BOS ``ls`` for the prefix, plus 1 more ``ls`` for each non-``.lance``
    child to confirm raw-episode shape. The result is small (one entry per
    dataset) so callers should cache via ``list_bos_datasets_cached`` instead
    of calling this directly per request.
    """
    if not is_remote_uri(prefix):
        raise ValueError(f"list_bos_datasets requires a bos:// or s3:// URI, got {prefix!r}")

    children = _list_dir(prefix)
    entries: list[BosDatasetEntry] = []
    for child_uri in children:
        name = _basename(child_uri)
        if not name:
            continue
        if name.endswith(LANCE_SUFFIX):
            entries.append(
                BosDatasetEntry(
                    uri=_strip_trailing_slash(child_uri),
                    name=name,
                    form="merged",
                    episode_count=None,
                )
            )
            continue
        # Non-.lance dir → maybe a raw-episode dataset folder. Cost: 1 extra ls.
        ok, count = _has_episode_lance_child(child_uri)
        if ok:
            entries.append(
                BosDatasetEntry(
                    uri=_strip_trailing_slash(child_uri),
                    name=name,
                    form="raw_episodes",
                    episode_count=count,
                )
            )
    entries.sort(key=lambda e: e.name)
    return entries


# ---------------------------------------------------------------------------
# In-process cache (landing-page friendly)
# ---------------------------------------------------------------------------


class _PrefixCache:
    def __init__(self, ttl_s: float = DEFAULT_CACHE_TTL_S) -> None:
        self._ttl = float(ttl_s)
        self._lock = threading.Lock()
        self._store: dict[str, tuple[float, list[BosDatasetEntry]]] = {}

    def get(self, prefix: str) -> list[BosDatasetEntry]:
        now = time.monotonic()
        with self._lock:
            cached = self._store.get(prefix)
            if cached is not None and (now - cached[0]) < self._ttl:
                return list(cached[1])
        # Avoid holding the lock through the network call.
        fresh = list_bos_datasets(prefix)
        with self._lock:
            self._store[prefix] = (time.monotonic(), list(fresh))
        return fresh

    def invalidate(self, prefix: Optional[str] = None) -> None:
        with self._lock:
            if prefix is None:
                self._store.clear()
            else:
                self._store.pop(prefix, None)


_default_cache = _PrefixCache()


def list_bos_datasets_cached(prefix: str) -> list[BosDatasetEntry]:
    """60s-memo wrapper. Use this from request handlers."""
    return _default_cache.get(prefix)


def invalidate_cache(prefix: Optional[str] = None) -> None:
    """Drop one prefix (or all) from the memo. Wired to the refresh button."""
    _default_cache.invalidate(prefix)


__all__ = [
    "BosDatasetEntry",
    "DEFAULT_CACHE_TTL_S",
    "invalidate_cache",
    "is_remote_uri",
    "list_bos_datasets",
    "list_bos_datasets_cached",
]
