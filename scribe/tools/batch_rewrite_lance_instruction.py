#!/usr/bin/env python3
"""Batch rewrite Lance language_instruction values from a JSON config."""

from __future__ import annotations

import sys
import json
import argparse
from pathlib import Path
from dataclasses import dataclass
from typing import Any

from scribe.tools.rewrite_lance_instruction import RewriteResult, plan_rewrites, rewrite_instruction_dataset


@dataclass(frozen=True)
class BatchRewriteItem:
    source: str
    target: str
    instruction: str
    overwrite: bool


@dataclass(frozen=True)
class PlannedBatchRewrite:
    source: str
    target: str
    instruction: str
    overwrite: bool


@dataclass(frozen=True)
class BatchRewriteFailure:
    source: str
    target: str
    error: str


@dataclass(frozen=True)
class BatchRewriteSummary:
    successes: list[RewriteResult]
    failures: list[BatchRewriteFailure]


def _items_from_config(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data["items"]
    raise ValueError("config must be a JSON array or an object with an 'items' array")


def load_batch_config(path: str | Path, *, default_overwrite: bool = False) -> list[BatchRewriteItem]:
    with Path(path).open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    items: list[BatchRewriteItem] = []
    for index, raw in enumerate(_items_from_config(data), start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"item {index}: expected object")

        missing = [key for key in ("source", "target", "instruction") if not str(raw.get(key, "")).strip()]
        if missing:
            raise ValueError(f"item {index}: missing required field(s): {missing}")

        overwrite = raw.get("overwrite", default_overwrite)
        if not isinstance(overwrite, bool):
            raise ValueError(f"item {index}: overwrite must be a boolean when provided")

        items.append(
            BatchRewriteItem(
                source=str(raw["source"]).strip(),
                target=str(raw["target"]).strip(),
                instruction=str(raw["instruction"]),
                overwrite=overwrite,
            )
        )
    return items


def plan_batch_rewrites(items: list[BatchRewriteItem]) -> list[PlannedBatchRewrite]:
    planned: list[PlannedBatchRewrite] = []
    for item in items:
        for rewrite in plan_rewrites(item.source, item.target):
            planned.append(
                PlannedBatchRewrite(
                    source=rewrite.source,
                    target=rewrite.target,
                    instruction=item.instruction,
                    overwrite=item.overwrite,
                )
            )
    return planned


def run_batch_rewrites(items: list[BatchRewriteItem]) -> BatchRewriteSummary:
    successes: list[RewriteResult] = []
    failures: list[BatchRewriteFailure] = []
    planned = plan_batch_rewrites(items)
    for index, item in enumerate(planned, start=1):
        print(f"[{index}/{len(planned)}] {item.source} -> {item.target}", flush=True)
        try:
            result = rewrite_instruction_dataset(
                item.source,
                item.target,
                item.instruction,
                overwrite=item.overwrite,
                progress=sys.stdout,
            )
        except Exception as exc:  # noqa: BLE001 - batch jobs should report and continue.
            error = f"{type(exc).__name__}: {exc}"
            failures.append(BatchRewriteFailure(source=item.source, target=item.target, error=error))
            print(f"[error] {item.source} -> {item.target}: {error}", file=sys.stderr, flush=True)
            continue
        successes.append(result)
        print(f"rewrote {result.rows} row(s), {result.episodes} episode(s): {result.target}", flush=True)
    return BatchRewriteSummary(successes=successes, failures=failures)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch rewrite Lance language_instruction values from JSON.")
    parser.add_argument("--config", required=True, help="JSON config path.")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Default overwrite=true for config items that omit overwrite.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print planned rewrites without writing datasets.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    items = load_batch_config(args.config, default_overwrite=args.overwrite)
    planned = plan_batch_rewrites(items)
    for item in planned:
        overwrite = "overwrite" if item.overwrite else "no-overwrite"
        print(f"{item.source} -> {item.target} [{overwrite}]")
    if args.dry_run:
        return
    summary = run_batch_rewrites(items)
    print(f"completed {len(summary.successes)} rewrite(s), {len(summary.failures)} failure(s)")
    if summary.failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
