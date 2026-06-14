"""Process-level dataset registry for the Scribe Flask app.

The original Scribe loaded exactly one dataset at process start and stuffed it
into every route via a closure variable. The BOS landing flow wants the user to
pick a dataset at runtime, so we need:

* a single source of truth for ``(namespace, name) → LanceDataset``,
* lazy construction on first request (Lance manifest reads only happen when
  someone actually clicks into the dataset),
* an LRU eviction policy so the process doesn't accumulate every dataset the
  user ever browsed,
* an interlock with :class:`scribe.bos_sync.BosSync` so eviction flushes
  pending edits and re-load short-circuits when the local cache already has
  pulled annotations.

Two kinds of registrations coexist:

* **pinned** — a dataset built once by the legacy CLI (``--root`` mode) and
  attached at process start. Never evicted, always returned as-is.
* **on-demand** — registered with just a BOS URI by the landing route. The
  registry constructs the :class:`LanceDataset` on first ``get()``; subsequent
  ``get()`` calls hit the LRU; LRU eviction calls ``sync.push`` but keeps the
  sync slot registered so an in-flight ``mark_dirty`` from a concurrent route
  handler can't be silently dropped.

For hub (non-local) datasets the registry is bypassed entirely: the legacy
``--repo-id`` flow constructs an ``IterableNamespace`` and pins it; the BOS
landing flow only ever produces ``LanceDataset`` instances.
"""

from __future__ import annotations

import re
import logging
import threading
from typing import Any, Optional
from pathlib import Path
from collections import OrderedDict
from dataclasses import dataclass

from scribe.lance_backend import LanceDataset
from scribe.annotation_store import (
    disabled_annotation_context,
    build_annotation_context_from_dir,
)

logger = logging.getLogger(__name__)


DEFAULT_LRU_SIZE = 3


_SAFE_NS_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def derive_slug(value: str) -> str:
    """URL/filesystem-safe slug derived from a display name."""
    cleaned = _SAFE_NS_CHARS.sub("-", str(value).strip()).strip("-")
    return cleaned or "dataset"


def split_namespace(repo_id: str) -> tuple[str, str]:
    repo_id = repo_id.strip().strip("/")
    if "/" in repo_id:
        ns, _, name = repo_id.partition("/")
        return ns or "local", name or "dataset"
    return "local", repo_id or "dataset"


# ---------------------------------------------------------------------------
# Entry records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RemoteEntry:
    ns: str
    name: str
    uri: str
    form: str  # "merged" | "raw_episodes" — informational; used by the landing page
    episode_count: Optional[int]


@dataclass
class _PinnedEntry:
    ns: str
    name: str
    obj: Any  # LeRobotDataset | LanceDataset | IterableNamespace
    annotation_dir: Optional[Path]  # None for hub datasets


# ---------------------------------------------------------------------------
# DatasetRegistry
# ---------------------------------------------------------------------------


class DatasetRegistry:
    """LRU-capped registry of loaded datasets, plus a directory of remote ones.

    Thread-safe. Methods that may take more than a few ms (``get`` on a miss,
    ``evict`` that triggers a push) drop the lock around the slow part so
    concurrent route handlers aren't serialised behind a single dataset load.
    """

    def __init__(
        self,
        *,
        runtime_dir: Path,
        sync: Optional["BosSync"] = None,  # noqa: F821 — string forward ref
        max_size: int = DEFAULT_LRU_SIZE,
    ) -> None:
        self._runtime_dir = Path(runtime_dir).resolve()
        self._sync = sync
        self._max = max(1, int(max_size))
        self._lock = threading.RLock()
        self._loaded: OrderedDict[tuple[str, str], LanceDataset] = OrderedDict()
        self._remote: dict[tuple[str, str], _RemoteEntry] = {}
        self._pinned: dict[tuple[str, str], _PinnedEntry] = {}

    # ----- pinned (legacy --root mode, hub mode) ---------------------------

    def pin(
        self,
        *,
        ns: str,
        name: str,
        obj: Any,
        annotation_dir: Optional[Path],
    ) -> None:
        """Register a dataset that's already loaded and must not be evicted."""
        key = (ns, name)
        with self._lock:
            if key in self._pinned:
                raise ValueError(f"already pinned: {ns}/{name}")
            self._pinned[key] = _PinnedEntry(ns=ns, name=name, obj=obj, annotation_dir=annotation_dir)
        logger.info("DatasetRegistry: pinned %s/%s (annotation_dir=%s)", ns, name, annotation_dir)

    # ----- on-demand (BOS landing) -----------------------------------------

    def register_remote(self, entry: _RemoteEntry) -> None:
        key = (entry.ns, entry.name)
        with self._lock:
            self._remote[key] = entry
        logger.debug("DatasetRegistry: registered remote %s/%s -> %s", entry.ns, entry.name, entry.uri)

    def register_remote_batch(self, entries: list[_RemoteEntry]) -> list[_RemoteEntry]:
        """Register a batch of remote datasets, disambiguating slug collisions.

        ``derive_slug`` is destructive (loses spaces, dots, etc.) so two
        distinct BOS dataset names can map to the same ``(ns, name)`` slot.
        Without disambiguation, both landing rows would route to whichever
        URI happened to win the last ``dict.__setitem__``, silently sending
        the user into the wrong dataset on click.

        For each entry whose ``(ns, name)`` slot is already taken by a
        *different* URI, we append a stable 8-char hash of the original URI
        to the name so each slug is unique. Returns the (possibly mutated)
        entries with the names actually written into the registry, so the
        caller can rebuild its display rows against the same identifiers.
        """
        import hashlib

        resolved: list[_RemoteEntry] = []
        with self._lock:
            for entry in entries:
                key = (entry.ns, entry.name)
                existing = self._remote.get(key)
                if existing is not None and existing.uri != entry.uri:
                    suffix = hashlib.sha1(entry.uri.encode("utf-8")).hexdigest()[:8]
                    disambiguated = _RemoteEntry(
                        ns=entry.ns,
                        name=f"{entry.name}-{suffix}",
                        uri=entry.uri,
                        form=entry.form,
                        episode_count=entry.episode_count,
                    )
                    logger.warning(
                        "DatasetRegistry: slug collision on %s/%s; renaming %s -> %s",
                        entry.ns,
                        entry.name,
                        entry.uri,
                        disambiguated.name,
                    )
                    self._remote[(disambiguated.ns, disambiguated.name)] = disambiguated
                    resolved.append(disambiguated)
                else:
                    self._remote[key] = entry
                    resolved.append(entry)
        return resolved

    def clear_remote_registrations(self) -> None:
        with self._lock:
            self._remote.clear()

    # ----- introspection ---------------------------------------------------

    def list_known(self) -> list[dict]:
        """Return a description of every dataset the registry recognises.

        Used by the landing page; the hot path uses ``get`` directly.
        """
        with self._lock:
            items: list[dict] = []
            for _key, pinned in self._pinned.items():
                items.append(
                    {
                        "ns": pinned.ns,
                        "name": pinned.name,
                        "source": "pinned",
                        "uri": None,
                        "form": "local",
                        "episode_count": getattr(pinned.obj, "num_episodes", None),
                        "loaded": True,
                    }
                )
            for key, remote in self._remote.items():
                items.append(
                    {
                        "ns": remote.ns,
                        "name": remote.name,
                        "source": "remote",
                        "uri": remote.uri,
                        "form": remote.form,
                        "episode_count": remote.episode_count,
                        "loaded": key in self._loaded,
                    }
                )
        items.sort(key=lambda d: (d["source"], d["ns"], d["name"]))
        return items

    def is_pinned(self, ns: str, name: str) -> bool:
        with self._lock:
            return (ns, name) in self._pinned

    def has(self, ns: str, name: str) -> bool:
        key = (ns, name)
        with self._lock:
            return key in self._pinned or key in self._remote

    # ----- get / evict -----------------------------------------------------

    def get(self, ns: str, name: str) -> Any:
        """Return the dataset object, loading from BOS on miss.

        Raises KeyError if the slot is neither pinned nor remote-registered.
        """
        key = (ns, name)
        with self._lock:
            pinned = self._pinned.get(key)
            if pinned is not None:
                return pinned.obj
            loaded = self._loaded.get(key)
            if loaded is not None:
                self._loaded.move_to_end(key)
                return loaded
            remote = self._remote.get(key)
            if remote is None:
                raise KeyError(f"unknown dataset: {ns}/{name}")
            uri = remote.uri

        # ---- slow path: build LanceDataset outside the lock -----------------
        dataset = self._build_remote_dataset(remote)
        if self._sync is not None:
            self._sync.register(ns=ns, name=name, uri=uri)
            try:
                self._sync.pull(ns=ns, name=name)
            except Exception:
                logger.exception("BosSync.pull failed for %s/%s; continuing with empty local cache", ns, name)

        with self._lock:
            # Another request may have raced us; keep the existing instance.
            existing = self._loaded.get(key)
            if existing is not None:
                self._loaded.move_to_end(key)
                return existing
            self._loaded[key] = dataset
            self._loaded.move_to_end(key)
            evicted_keys = self._evict_to_size_locked()
        for ev_key in evicted_keys:
            self._on_eviction(ev_key)
        return dataset

    def evict(self, ns: str, name: str) -> bool:
        key = (ns, name)
        with self._lock:
            if key not in self._loaded:
                return False
            del self._loaded[key]
        self._on_eviction(key)
        return True

    def _evict_to_size_locked(self) -> list[tuple[str, str]]:
        evicted: list[tuple[str, str]] = []
        while len(self._loaded) > self._max:
            old_key, _old_obj = self._loaded.popitem(last=False)
            evicted.append(old_key)
        return evicted

    def _on_eviction(self, key: tuple[str, str]) -> None:
        """Drop the LanceDataset reference; keep BosSync state alive.

        We deliberately do NOT call ``sync.unregister`` here. Two reasons:

        * If ``sync.push`` failed (network blip), unregistering and then
          re-registering on a future ``get()`` would reset ``last_pulled_at``
          back to None, and the next ``pull(force=False)`` would overwrite
          the user's unsynced sidecar with stale BOS bytes. Keeping the slot
          alive lets the next AutoSaver tick retry the upload.
        * An in-flight request may already hold the cache path (resolved
          before this eviction tick) and be in the middle of writing a
          sidecar. ``unregister`` would make the subsequent ``mark_dirty``
          a silent no-op and orphan that edit. Keeping the slot keeps the
          dirty bit reachable.

        The sync slot is small (a uri + a few timestamps + the cache dir
        path), so persisting it for the process lifetime is cheap.
        """
        ns, name = key
        if self._sync is not None:
            try:
                self._sync.push(ns=ns, name=name)
            except Exception:
                logger.exception("BosSync.push on eviction failed for %s/%s", ns, name)
        logger.info("DatasetRegistry: evicted LanceDataset %s/%s (sync slot retained)", ns, name)

    # ----- annotation directory routing ------------------------------------

    def annotation_context(self, ns: str, name: str, repo_id: str) -> dict:
        """Compute the annotation context the routes hand to the frontend.

        For pinned local datasets this is ``<root>/annotations``. For remote
        datasets this is the local sync cache slot (so annotation_store CRUD
        remains a plain ``pathlib`` write). Hub-mode and not-registered
        datasets return the disabled context.
        """
        key = (ns, name)
        with self._lock:
            pinned = self._pinned.get(key)
            remote = self._remote.get(key)

        if pinned is not None and pinned.annotation_dir is not None:
            return build_annotation_context_from_dir(
                pinned.annotation_dir,
                dataset_id=pinned.annotation_dir.parent.resolve().as_posix(),
                dataset_root=pinned.annotation_dir.parent.resolve().as_posix(),
            )
        if remote is not None and self._sync is not None:
            # `get()` may not have been called yet — the very first hit on the
            # /<ns>/<name>/api/* routes triggers load via get(); here we just
            # build the path the sync layer maintains.
            self._sync.register(ns=ns, name=name, uri=remote.uri)
            annotations_dir = self._sync.annotations_dir_for(ns, name)
            return build_annotation_context_from_dir(
                annotations_dir,
                dataset_id=remote.uri,
                dataset_root=remote.uri,
            )
        return disabled_annotation_context(repo_id)

    # ----- shutdown --------------------------------------------------------

    def flush_and_close(self) -> None:
        """Push every remaining loaded dataset's edits. Safe to call multiple
        times; used by atexit and explicit shutdown paths."""
        with self._lock:
            keys = list(self._loaded.keys())
        for key in keys:
            self._on_eviction(key)
        with self._lock:
            self._loaded.clear()

    # ----- internal: dataset construction ----------------------------------

    def _build_remote_dataset(self, remote: _RemoteEntry) -> LanceDataset:
        repo_id = f"{remote.ns}/{remote.name}"
        logger.info(
            "DatasetRegistry: loading remote dataset %s (form=%s, uri=%s)",
            repo_id,
            remote.form,
            remote.uri,
        )
        return LanceDataset(repo_id=repo_id, root=remote.uri, runtime_dir=self._runtime_dir)


__all__ = [
    "DEFAULT_LRU_SIZE",
    "DatasetRegistry",
    "_RemoteEntry",  # exported so routes can build entries from BOS discovery
    "derive_slug",
    "split_namespace",
]
