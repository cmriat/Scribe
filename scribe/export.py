#!/usr/bin/env python3
"""
Export Scribe sidecar annotations into progress and LeRobot-compatible subtask artifacts.

This script does not modify the source dataset. It produces an export directory containing:
- clip_manifest.jsonl
- progress/episode_xxxxxx.parquet
- meta/subtasks.parquet
- meta/stage_priors.json
- export_report.json
"""

from __future__ import annotations

import json
import shutil
import argparse
from typing import Any
from pathlib import Path

import numpy as np
import pandas as pd

from scribe.annotation_store import (
    DEFAULT_SCHEME,
    SEGMENT_ANNOTATIONS_FILENAME,
    TASK_ANNOTATION_CONFIG_FILENAME,
    get_scheme_config,
    build_stage_catalog,
    load_task_annotation_store,
    summarize_episode_segments,
    load_segment_annotation_store,
)

SUBTASK_EXPORT_COLUMNS = [
    "task_name",
    "scheme",
    "subtask_index",
    "subtask",
    "display_name",
    "color",
]


def load_info_json(dataset_root: Path) -> dict[str, Any]:
    info_path = dataset_root / "meta" / "info.json"
    with info_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_episodes_jsonl(dataset_root: Path) -> list[dict[str, Any]]:
    episodes_path = dataset_root / "meta" / "episodes.jsonl"
    items = []
    with episodes_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            items.append(json.loads(line))
    return items


def data_path_for_episode(dataset_root: Path, info: dict[str, Any], episode_index: int) -> Path:
    chunk_size = int(info.get("chunks_size", 1000))
    data_template = str(info.get("data_path") or "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet")
    return dataset_root / data_template.format(
        episode_chunk=episode_index // chunk_size,
        episode_index=episode_index,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export subtask annotations from dataset_visualizer sidecar files.")
    parser.add_argument("--dataset-root", type=Path, required=True, help="Path to the local LeRobot dataset root.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Export directory. Defaults to <dataset-root>/annotations/exports/subtask_export.",
    )
    parser.add_argument(
        "--scheme", type=str, default=DEFAULT_SCHEME, help="Annotation scheme to export. Default: sparse."
    )
    parser.add_argument(
        "--task-name",
        type=str,
        default=None,
        help="Optional task filter. When omitted, export all tasks with complete annotations.",
    )
    parser.add_argument("--overwrite", action="store_true", help="Overwrite the output directory if it already exists.")
    return parser.parse_args()


def ensure_output_dir(path: Path, overwrite: bool) -> None:
    if path.exists() and not path.is_dir():
        if not overwrite:
            raise FileExistsError(f"Output path already exists and is not a directory: {path}")
        path.unlink()
    elif path.exists() and any(path.iterdir()):
        if not overwrite:
            raise FileExistsError(f"Output directory already exists and is not empty: {path}")
        shutil.rmtree(path)

    path.mkdir(parents=True, exist_ok=True)
    (path / "meta").mkdir(parents=True, exist_ok=True)
    (path / "progress").mkdir(parents=True, exist_ok=True)


def make_empty_subtasks_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "task_name": pd.Series(dtype="object"),
            "scheme": pd.Series(dtype="object"),
            "subtask_index": pd.Series(dtype="int32"),
            "subtask": pd.Series(dtype="object"),
            "display_name": pd.Series(dtype="object"),
            "color": pd.Series(dtype="object"),
        },
        columns=SUBTASK_EXPORT_COLUMNS,
    )


def validate_segment_coverage(segments: list[dict[str, Any]], frame_count: int) -> str | None:
    if frame_count <= 0:
        return "invalid_frame_count"
    if not segments:
        return "missing_segments"

    next_expected_frame = 0
    for segment in sorted(segments, key=lambda item: int(item["frame_start"])):
        frame_start = int(segment["frame_start"])
        frame_end = int(segment["frame_end"])
        if frame_start != next_expected_frame:
            return "non_contiguous_coverage"
        if frame_end < frame_start:
            return "invalid_segment_range"
        next_expected_frame = frame_end + 1

    if next_expected_frame != frame_count:
        return "non_contiguous_coverage"

    return None


def main() -> None:
    args = parse_args()
    dataset_root = args.dataset_root.resolve()
    annotations_dir = dataset_root / "annotations"
    output_dir = args.output_dir.resolve() if args.output_dir else annotations_dir / "exports" / "subtask_export"
    ensure_output_dir(output_dir, overwrite=args.overwrite)

    info = load_info_json(dataset_root)
    episodes = load_episodes_jsonl(dataset_root)
    frame_counts_by_episode = {
        int(item["episode_index"]): int(item.get("length", 0)) for item in episodes if "episode_index" in item
    }

    task_config_path = annotations_dir / TASK_ANNOTATION_CONFIG_FILENAME
    segment_path = annotations_dir / SEGMENT_ANNOTATIONS_FILENAME
    task_store = load_task_annotation_store(task_config_path, dataset_root.as_posix())
    segment_store = load_segment_annotation_store(segment_path, dataset_root.as_posix())

    tasks = task_store.get("tasks", {})
    if not isinstance(tasks, dict) or not tasks:
        raise RuntimeError(f"No tasks available in {task_config_path}")

    clip_manifest_path = output_dir / "clip_manifest.jsonl"
    clip_manifest_path.write_text("", encoding="utf-8")
    subtasks_rows = []
    stage_priors: dict[str, Any] = {}
    export_report = {
        "dataset_root": dataset_root.as_posix(),
        "scheme": args.scheme,
        "task_filter": args.task_name,
        "exported_episodes": [],
        "skipped_episodes": [],
    }

    task_records: dict[str, list[dict[str, Any]]] = {}
    for episode_key, record in sorted(segment_store.get("items", {}).items(), key=lambda item: int(item[0])):
        episode_index = int(record.get("episode_index", episode_key))
        task_name = str(record.get("task_name") or "").strip()
        if args.task_name and task_name != args.task_name:
            continue
        if not task_name or task_name not in tasks:
            export_report["skipped_episodes"].append(
                {
                    "episode_index": episode_index,
                    "reason": "unknown_task_name",
                    "task_name": task_name,
                }
            )
            continue

        task_profile = tasks[task_name]
        stage_order = list(get_scheme_config(task_profile, args.scheme).get("stage_order", []))
        if not stage_order:
            export_report["skipped_episodes"].append(
                {
                    "episode_index": episode_index,
                    "reason": "missing_stage_order",
                    "task_name": task_name,
                }
            )
            continue

        segments = record.get("schemes", {}).get(args.scheme, [])
        frame_count = frame_counts_by_episode.get(episode_index, 0)
        summary = summarize_episode_segments(segments, stage_order=stage_order, total_frames=frame_count)
        coverage_error = validate_segment_coverage(segments, frame_count)
        if coverage_error is not None:
            export_report["skipped_episodes"].append(
                {
                    "episode_index": episode_index,
                    "reason": coverage_error,
                    "task_name": task_name,
                    "summary": summary,
                }
            )
            continue

        if not summary["can_export"]:
            export_report["skipped_episodes"].append(
                {
                    "episode_index": episode_index,
                    "reason": "annotation_incomplete",
                    "task_name": task_name,
                    "summary": summary,
                }
            )
            continue

        task_records.setdefault(task_name, []).append(
            {
                "episode_index": episode_index,
                "segments": segments,
                "frame_count": frame_count,
                "summary": summary,
            }
        )

    for task_name, records in task_records.items():
        task_profile = tasks[task_name]
        scheme_config = get_scheme_config(task_profile, args.scheme)
        stage_order = list(scheme_config.get("stage_order", []))
        stage_catalog = build_stage_catalog(task_profile, scheme=args.scheme)
        stage_catalog_by_key = {item["key"]: item for item in stage_catalog}

        stage_totals = {stage_label: [] for stage_label in stage_order}
        for record in records:
            frame_count = max(1, int(record["frame_count"]))
            for segment in sorted(record["segments"], key=lambda item: int(item["frame_start"])):
                stage_label = str(segment["stage_label"])
                stage_totals[stage_label].append(
                    (int(segment["frame_end"]) - int(segment["frame_start"]) + 1) / frame_count
                )

        priors = {}
        for stage_label in stage_order:
            values = stage_totals.get(stage_label, [])
            priors[stage_label] = float(sum(values) / len(values)) if values else 0.0

        priors_sum = sum(priors.values())
        if priors_sum <= 0:
            raise RuntimeError(f"Computed zero priors for task {task_name}")
        priors = {stage_label: value / priors_sum for stage_label, value in priors.items()}
        cumulative_offsets = {}
        running_total = 0.0
        for stage_label in stage_order:
            cumulative_offsets[stage_label] = running_total
            running_total += priors[stage_label]

        stage_priors[task_name] = {
            "scheme": args.scheme,
            "episode_count": len(records),
            "priors": priors,
            "stage_order": stage_order,
        }

        for stage_index, stage_label in enumerate(stage_order):
            catalog_entry = stage_catalog_by_key.get(stage_label, {})
            subtasks_rows.append(
                {
                    "task_name": task_name,
                    "scheme": args.scheme,
                    "subtask_index": stage_index,
                    "subtask": stage_label,
                    "display_name": catalog_entry.get("title", stage_label.replace("_", " ").title()),
                    "color": catalog_entry.get("color", "#64748b"),
                }
            )

        for record in records:
            episode_index = int(record["episode_index"])
            parquet_path = data_path_for_episode(dataset_root, info, episode_index)
            df = pd.read_parquet(parquet_path, columns=["timestamp"])
            frame_count = len(df)

            subtask_index = np.full(frame_count, -1, dtype=np.int32)
            subtask = np.full(frame_count, "", dtype=object)
            stage_progress = np.zeros(frame_count, dtype=np.float32)
            global_progress = np.zeros(frame_count, dtype=np.float32)
            assigned_frames = np.zeros(frame_count, dtype=bool)

            clip_entries = []
            ordered_segments = sorted(record["segments"], key=lambda item: int(item["frame_start"]))
            for stage_idx, segment in enumerate(ordered_segments):
                label = str(segment["stage_label"])
                start = int(segment["frame_start"])
                end = int(segment["frame_end"])
                segment_length = max(1, end - start + 1)
                if segment_length == 1:
                    local_progress = np.array([1.0], dtype=np.float32)
                else:
                    local_progress = np.linspace(0.0, 1.0, num=segment_length, dtype=np.float32)

                subtask_index[start : end + 1] = stage_idx
                subtask[start : end + 1] = label
                stage_progress[start : end + 1] = local_progress
                global_progress[start : end + 1] = cumulative_offsets[label] + local_progress * priors[label]
                assigned_frames[start : end + 1] = True

                clip_entries.append(
                    {
                        "task_name": task_name,
                        "scheme": args.scheme,
                        "episode_index": episode_index,
                        "clip_id": f"{task_name}_ep{episode_index:06d}_{stage_idx:02d}",
                        "subtask_index": stage_idx,
                        "stage_label": label,
                        "display_label": stage_catalog_by_key.get(label, {}).get(
                            "title", label.replace("_", " ").title()
                        ),
                        "frame_start": start,
                        "frame_end": end,
                        "time_start_s": float(segment["time_start_s"]),
                        "time_end_s": float(segment["time_end_s"]),
                    }
                )

            if not np.all(assigned_frames):
                raise RuntimeError(
                    f"Episode {episode_index} passed validation but still has uncovered frames during export."
                )

            frame_df = pd.DataFrame(
                {
                    "episode_index": np.full(frame_count, episode_index, dtype=np.int32),
                    "frame_index": np.arange(frame_count, dtype=np.int32),
                    "timestamp": df["timestamp"].to_numpy(dtype=np.float64, copy=False),
                    "task_name": np.full(frame_count, task_name, dtype=object),
                    "scheme": np.full(frame_count, args.scheme, dtype=object),
                    "subtask_index": subtask_index,
                    "subtask": subtask,
                    "stage_progress": stage_progress,
                    "global_progress": global_progress,
                }
            )
            frame_df.to_parquet(output_dir / "progress" / f"episode_{episode_index:06d}.parquet", index=False)

            with clip_manifest_path.open("a", encoding="utf-8") as handle:
                for entry in clip_entries:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

            export_report["exported_episodes"].append(
                {
                    "episode_index": episode_index,
                    "task_name": task_name,
                    "frame_count": frame_count,
                    "clip_count": len(clip_entries),
                }
            )

    subtasks_df = make_empty_subtasks_frame() if not subtasks_rows else pd.DataFrame(subtasks_rows)
    subtasks_df = subtasks_df.drop_duplicates()
    if not subtasks_df.empty:
        subtasks_df = subtasks_df.sort_values(["task_name", "subtask_index"])
    else:
        subtasks_df = subtasks_df.reindex(columns=SUBTASK_EXPORT_COLUMNS)

    subtasks_df.to_parquet(
        output_dir / "meta" / "subtasks.parquet",
        index=False,
    )
    (output_dir / "meta" / "stage_priors.json").write_text(
        json.dumps(stage_priors, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "export_report.json").write_text(
        json.dumps(export_report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Export complete: {output_dir}")
    print(f"Exported episodes: {len(export_report['exported_episodes'])}")
    print(f"Skipped episodes: {len(export_report['skipped_episodes'])}")


if __name__ == "__main__":
    main()
