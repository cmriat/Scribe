from __future__ import annotations

import re
import json
from copy import deepcopy
from uuid import uuid4
from typing import Any
from pathlib import Path
from datetime import datetime, timezone

from lerobot.datasets.utils import IterableNamespace
from lerobot.datasets.lerobot_dataset import LeRobotDataset

ANNOTATIONS_DIRNAME = "annotations"
EPISODE_CURATION_FILENAME = "episode_curation.json"
SEGMENT_ANNOTATIONS_FILENAME = "segment_annotations.json"
FRAME_EVENTS_FILENAME = "frame_events.jsonl"
TASK_ANNOTATION_CONFIG_FILENAME = "task_annotation_config.json"

DEFAULT_SCHEME = "sparse"
SUPPORTED_SCHEMES = {DEFAULT_SCHEME}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_annotation_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


DEFAULT_TASK_ANNOTATION_STORE = {
    "version": 1,
    "active_task_name": None,
    "tasks": {
        "fold_long_horizon": {
            "display_name": "Fold Cloth",
            "match_keywords": [
                "fold",
                "laundry",
                "shirt",
                "t-shirt",
                "cloth",
                "garment",
                "sleeve",
            ],
            "allow_custom_labels": True,
            "schemes": {
                "sparse": {
                    "display_name": "Sparse",
                    "stage_order": ["pick_up", "spread", "fold", "place"],
                    "stage_metadata": {
                        "pick_up": {"title": "Pick Up", "color": "#0f766e"},
                        "spread": {"title": "Spread", "color": "#2563eb"},
                        "fold": {"title": "Fold", "color": "#9333ea"},
                        "place": {"title": "Place", "color": "#ea580c"},
                    },
                }
            },
        },
        "generic_long_horizon": {
            "display_name": "Generic Long Horizon",
            "match_keywords": [],
            "allow_custom_labels": True,
            "schemes": {
                "sparse": {
                    "display_name": "Sparse",
                    "stage_order": ["acquire", "arrange", "operate", "place"],
                    "stage_metadata": {
                        "acquire": {"title": "Acquire", "color": "#0f766e"},
                        "arrange": {"title": "Arrange", "color": "#2563eb"},
                        "operate": {"title": "Operate", "color": "#9333ea"},
                        "place": {"title": "Place", "color": "#ea580c"},
                    },
                }
            },
        },
    },
}


def _slugify_text(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return normalized or "custom"


def build_local_annotation_context(
    dataset_obj: LeRobotDataset | IterableNamespace,
    repo_id: str,
) -> dict[str, Any]:
    if isinstance(dataset_obj, LeRobotDataset):
        dataset_root = dataset_obj.root.resolve()
        annotations_dir = dataset_root / ANNOTATIONS_DIRNAME
        return {
            "enabled": True,
            "dataset_id": dataset_root.as_posix(),
            "dataset_root": dataset_root.as_posix(),
            "annotations_dir": annotations_dir.as_posix(),
            "files": {
                "episode_curation": (annotations_dir / EPISODE_CURATION_FILENAME).as_posix(),
                "segment_annotations": (annotations_dir / SEGMENT_ANNOTATIONS_FILENAME).as_posix(),
                "frame_events": (annotations_dir / FRAME_EVENTS_FILENAME).as_posix(),
                "task_config": (annotations_dir / TASK_ANNOTATION_CONFIG_FILENAME).as_posix(),
            },
        }

    return {
        "enabled": False,
        "dataset_id": repo_id,
        "dataset_root": None,
        "annotations_dir": None,
        "files": {
            "episode_curation": None,
            "segment_annotations": None,
            "frame_events": None,
            "task_config": None,
        },
    }


def _default_episode_curation_store(dataset_id: str) -> dict[str, Any]:
    now_iso = utc_now_iso()
    return {
        "version": 1,
        "dataset_id": dataset_id,
        "updated_at": now_iso,
        "items": {},
    }


def load_episode_curation_store(storage_path: Path, dataset_id: str) -> dict[str, Any]:
    if not storage_path.exists():
        return _default_episode_curation_store(dataset_id)

    try:
        content = json.loads(storage_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _default_episode_curation_store(dataset_id)

    if not isinstance(content, dict):
        return _default_episode_curation_store(dataset_id)

    items = content.get("items", {})
    if not isinstance(items, dict):
        items = {}

    return {
        "version": 1,
        "dataset_id": dataset_id,
        "updated_at": str(content.get("updated_at") or utc_now_iso()),
        "items": items,
    }


def save_episode_curation_store(storage_path: Path, store: dict[str, Any]) -> None:
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    storage_path.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")


def default_task_annotation_store(dataset_id: str) -> dict[str, Any]:
    store = deepcopy(DEFAULT_TASK_ANNOTATION_STORE)
    store["dataset_id"] = dataset_id
    store["updated_at"] = utc_now_iso()
    return store


def load_task_annotation_store(storage_path: Path, dataset_id: str) -> dict[str, Any]:
    default_store = default_task_annotation_store(dataset_id)
    if not storage_path.exists():
        return default_store

    try:
        content = json.loads(storage_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_store

    if not isinstance(content, dict):
        return default_store

    tasks = content.get("tasks", {})
    if not isinstance(tasks, dict) or not tasks:
        tasks = default_store["tasks"]

    active_task_name = content.get("active_task_name")
    if active_task_name is not None and not isinstance(active_task_name, str):
        active_task_name = None

    return {
        "version": 1,
        "dataset_id": dataset_id,
        "updated_at": str(content.get("updated_at") or utc_now_iso()),
        "active_task_name": active_task_name,
        "tasks": tasks,
    }


def ensure_task_annotation_store(storage_path: Path, dataset_id: str) -> dict[str, Any]:
    store = load_task_annotation_store(storage_path, dataset_id)
    if not storage_path.exists():
        save_task_annotation_store(storage_path, store)
    return store


def save_task_annotation_store(storage_path: Path, store: dict[str, Any]) -> None:
    store = deepcopy(store)
    store["version"] = 1
    store["updated_at"] = utc_now_iso()
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    storage_path.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")


def default_segment_annotation_store(dataset_id: str) -> dict[str, Any]:
    return {
        "version": 1,
        "dataset_id": dataset_id,
        "updated_at": utc_now_iso(),
        "items": {},
    }


def load_segment_annotation_store(storage_path: Path, dataset_id: str) -> dict[str, Any]:
    default_store = default_segment_annotation_store(dataset_id)
    if not storage_path.exists():
        return default_store

    try:
        content = json.loads(storage_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_store

    if not isinstance(content, dict):
        return default_store

    items = content.get("items", {})
    if not isinstance(items, dict):
        items = {}

    normalized_items: dict[str, Any] = {}
    for episode_key, record in items.items():
        if not isinstance(record, dict):
            continue
        try:
            episode_index = int(record.get("episode_index", episode_key))
        except (TypeError, ValueError):
            continue

        schemes = record.get("schemes", {})
        if not isinstance(schemes, dict):
            schemes = {}

        normalized_record = {
            "episode_index": episode_index,
            "task_name": str(record.get("task_name") or ""),
            "updated_at": str(record.get("updated_at") or utc_now_iso()),
            "schemes": {},
        }

        for scheme_name, segments in schemes.items():
            if scheme_name not in SUPPORTED_SCHEMES:
                continue
            if not isinstance(segments, list):
                continue
            normalized_record["schemes"][scheme_name] = sort_segments(
                [normalize_segment(segment) for segment in segments if isinstance(segment, dict)]
            )

        normalized_items[str(episode_index)] = normalized_record

    default_store["items"] = normalized_items
    default_store["updated_at"] = str(content.get("updated_at") or utc_now_iso())
    return default_store


def save_segment_annotation_store(storage_path: Path, store: dict[str, Any]) -> None:
    payload = deepcopy(store)
    payload["version"] = 1
    payload["updated_at"] = utc_now_iso()
    for record in payload.get("items", {}).values():
        record["updated_at"] = payload["updated_at"]
        schemes = record.get("schemes", {})
        for scheme_name, segments in list(schemes.items()):
            schemes[scheme_name] = sort_segments(
                [normalize_segment(segment) for segment in segments if isinstance(segment, dict)]
            )

    storage_path.parent.mkdir(parents=True, exist_ok=True)
    storage_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_frame_event_records(storage_path: Path, dataset_id: str) -> dict[str, Any]:
    if not storage_path.exists():
        return {
            "version": 1,
            "dataset_id": dataset_id,
            "updated_at": utc_now_iso(),
            "items": [],
        }

    items = []
    updated_at = utc_now_iso()
    try:
        with storage_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                raw = line.strip()
                if not raw:
                    continue
                record = json.loads(raw)
                if not isinstance(record, dict):
                    continue
                items.append(normalize_frame_event(record))
                updated_at = str(record.get("updated_at") or updated_at)
    except (OSError, json.JSONDecodeError):
        return {
            "version": 1,
            "dataset_id": dataset_id,
            "updated_at": utc_now_iso(),
            "items": [],
        }

    items.sort(
        key=lambda item: (int(item.get("episode_index", 0)), float(item.get("time_s", 0.0)), item.get("event_id", ""))
    )
    return {
        "version": 1,
        "dataset_id": dataset_id,
        "updated_at": updated_at,
        "items": items,
    }


def save_frame_event_records(storage_path: Path, dataset_id: str, items: list[dict[str, Any]]) -> None:
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    normalized = [normalize_frame_event(item) for item in items if isinstance(item, dict)]
    normalized.sort(
        key=lambda item: (int(item.get("episode_index", 0)), float(item.get("time_s", 0.0)), item.get("event_id", ""))
    )
    with storage_path.open("w", encoding="utf-8") as handle:
        for item in normalized:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")


def normalize_segment(segment: dict[str, Any]) -> dict[str, Any]:
    return {
        "segment_id": str(segment.get("segment_id") or new_annotation_id("segment")),
        "task_name": str(segment.get("task_name") or ""),
        "scheme": str(segment.get("scheme") or DEFAULT_SCHEME),
        "stage_label": str(segment.get("stage_label") or "").strip(),
        "display_label": str(segment.get("display_label") or segment.get("stage_label") or "").strip(),
        "frame_start": int(segment.get("frame_start", 0)),
        "frame_end": int(segment.get("frame_end", 0)),
        "time_start_s": float(segment.get("time_start_s", 0.0)),
        "time_end_s": float(segment.get("time_end_s", 0.0)),
        "operator": str(segment.get("operator") or "anonymous").strip() or "anonymous",
        "updated_at": str(segment.get("updated_at") or utc_now_iso()),
        "is_custom_label": bool(segment.get("is_custom_label", False)),
    }


def normalize_frame_event(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": str(record.get("event_id") or new_annotation_id("event")),
        "task_name": str(record.get("task_name") or ""),
        "episode_index": int(record.get("episode_index", 0)),
        "frame_index": int(record.get("frame_index", 0)),
        "time_s": float(record.get("time_s", 0.0)),
        "event_type": str(record.get("event_type") or "").strip(),
        "note": str(record.get("note") or "").strip(),
        "operator": str(record.get("operator") or "anonymous").strip() or "anonymous",
        "updated_at": str(record.get("updated_at") or utc_now_iso()),
    }


def sort_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        segments,
        key=lambda item: (
            int(item.get("frame_start", 0)),
            int(item.get("frame_end", 0)),
            str(item.get("segment_id", "")),
        ),
    )


def resolve_task_profile(
    task_store: dict[str, Any],
    language_instruction: str | None,
) -> tuple[str, dict[str, Any]]:
    tasks = task_store.get("tasks", {}) if isinstance(task_store, dict) else {}
    if not isinstance(tasks, dict) or not tasks:
        tasks = default_task_annotation_store("local")["tasks"]

    normalized_instruction = str(language_instruction or "").lower()

    for task_name, task_profile in tasks.items():
        keywords = task_profile.get("match_keywords", [])
        if any(
            keyword.lower() in normalized_instruction for keyword in keywords if isinstance(keyword, str) and keyword
        ):
            return task_name, task_profile

    active_task_name = task_store.get("active_task_name")
    if isinstance(active_task_name, str) and active_task_name in tasks:
        return active_task_name, tasks[active_task_name]

    if "generic_long_horizon" in tasks:
        return "generic_long_horizon", tasks["generic_long_horizon"]

    first_task_name = next(iter(tasks))
    return first_task_name, tasks[first_task_name]


def get_scheme_config(task_profile: dict[str, Any], scheme: str = DEFAULT_SCHEME) -> dict[str, Any]:
    schemes = task_profile.get("schemes", {})
    if not isinstance(schemes, dict):
        schemes = {}
    config = schemes.get(scheme, {})
    return config if isinstance(config, dict) else {}


def build_stage_catalog(task_profile: dict[str, Any], scheme: str = DEFAULT_SCHEME) -> list[dict[str, Any]]:
    scheme_config = get_scheme_config(task_profile, scheme=scheme)
    stage_order = scheme_config.get("stage_order", [])
    stage_metadata = scheme_config.get("stage_metadata", {})
    catalog = []
    for stage_label in stage_order:
        metadata = stage_metadata.get(stage_label, {}) if isinstance(stage_metadata, dict) else {}
        catalog.append(
            {
                "key": stage_label,
                "title": str(metadata.get("title") or stage_label.replace("_", " ").title()),
                "color": str(metadata.get("color") or "#2563eb"),
            }
        )
    return catalog


def build_task_context_payload(
    annotation_context: dict[str, Any],
    task_name: str,
    task_profile: dict[str, Any],
    scheme: str = DEFAULT_SCHEME,
) -> dict[str, Any]:
    scheme_config = get_scheme_config(task_profile, scheme=scheme)
    files = annotation_context.get("files", {})
    return {
        "enabled": bool(annotation_context.get("enabled")),
        "dataset_id": annotation_context.get("dataset_id"),
        "annotations_dir": annotation_context.get("annotations_dir"),
        "task_name": task_name,
        "task_display_name": str(task_profile.get("display_name") or task_name.replace("_", " ").title()),
        "scheme": scheme,
        "allow_custom_labels": bool(task_profile.get("allow_custom_labels", True)),
        "stage_order": list(scheme_config.get("stage_order", [])),
        "stage_catalog": build_stage_catalog(task_profile, scheme=scheme),
        "files": {
            "task_config": files.get("task_config"),
            "segment_annotations": files.get("segment_annotations"),
            "frame_events": files.get("frame_events"),
        },
    }


def summarize_episode_segments(
    segments: list[dict[str, Any]],
    stage_order: list[str],
    total_frames: int,
) -> dict[str, Any]:
    sorted_segments = sort_segments([normalize_segment(segment) for segment in segments if isinstance(segment, dict)])
    if not sorted_segments:
        return {
            "status": "unlabeled",
            "segment_count": 0,
            "has_custom_labels": False,
            "can_export": False,
            "coverage_ratio": 0.0,
            "errors": [],
        }

    errors: list[str] = []
    has_custom_labels = False
    covered_frames = 0
    seen_stage_labels: set[str] = set()
    stage_indices: list[int] = []

    previous_end = None
    previous_stage_index = None
    for segment in sorted_segments:
        frame_start = int(segment.get("frame_start", 0))
        frame_end = int(segment.get("frame_end", 0))
        if frame_start > frame_end:
            errors.append(f"invalid_range:{segment['segment_id']}")
            continue

        if previous_end is not None and frame_start <= previous_end:
            errors.append(f"overlap:{segment['segment_id']}")
        previous_end = frame_end
        covered_frames += max(0, frame_end - frame_start + 1)

        stage_label = str(segment.get("stage_label") or "")
        is_custom = bool(segment.get("is_custom_label"))
        if is_custom or stage_label not in stage_order:
            has_custom_labels = True
            continue

        stage_index = stage_order.index(stage_label)
        if stage_label in seen_stage_labels:
            errors.append(f"duplicate_stage:{stage_label}")
        seen_stage_labels.add(stage_label)
        stage_indices.append(stage_index)
        if previous_stage_index is not None and stage_index <= previous_stage_index:
            errors.append(f"order:{stage_label}")
        previous_stage_index = stage_index

    total_frames = max(int(total_frames), 0)
    expected_complete = stage_indices == list(range(len(stage_order))) if stage_order else False
    starts_at_zero = int(sorted_segments[0].get("frame_start", 0)) == 0
    ends_at_last = total_frames > 0 and int(sorted_segments[-1].get("frame_end", -1)) >= total_frames - 1
    coverage_ratio = 0.0 if total_frames <= 0 else min(1.0, covered_frames / max(total_frames, 1))

    can_export = (
        total_frames > 0
        and not errors
        and not has_custom_labels
        and starts_at_zero
        and ends_at_last
        and expected_complete
    )

    return {
        "status": "complete" if can_export else "partial",
        "segment_count": len(sorted_segments),
        "has_custom_labels": has_custom_labels,
        "can_export": can_export,
        "coverage_ratio": coverage_ratio,
        "errors": errors,
    }


def build_segment_summary_by_episode(
    store: dict[str, Any],
    stage_order_by_task: dict[str, list[str]],
    frame_counts_by_episode: dict[int, int],
    scheme: str = DEFAULT_SCHEME,
) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for episode_key, record in store.get("items", {}).items():
        task_name = str(record.get("task_name") or "")
        stage_order = stage_order_by_task.get(task_name, [])
        segments = record.get("schemes", {}).get(scheme, [])
        episode_index = int(record.get("episode_index", episode_key))
        episode_summary = summarize_episode_segments(
            segments,
            stage_order=stage_order,
            total_frames=frame_counts_by_episode.get(episode_index, 0),
        )
        episode_summary["task_name"] = task_name
        summary[str(episode_index)] = episode_summary
    return summary


def infer_display_label(stage_label: str, stage_catalog: list[dict[str, Any]]) -> str:
    for stage_entry in stage_catalog:
        if stage_entry.get("key") == stage_label:
            return str(stage_entry.get("title") or stage_label)
    return stage_label.replace("_", " ").title()


def infer_color(stage_label: str, stage_catalog: list[dict[str, Any]]) -> str:
    for stage_entry in stage_catalog:
        if stage_entry.get("key") == stage_label:
            return str(stage_entry.get("color") or "#2563eb")
    return "#64748b"


def build_custom_task_profile(task_name: str) -> dict[str, Any]:
    slug = _slugify_text(task_name)
    return {
        "display_name": task_name or slug.replace("_", " ").title(),
        "match_keywords": [],
        "allow_custom_labels": True,
        "schemes": {
            "sparse": {
                "display_name": "Sparse",
                "stage_order": [],
                "stage_metadata": {},
            }
        },
    }
