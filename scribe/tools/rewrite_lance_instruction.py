#!/usr/bin/env python3
"""Copy merged Lance dataset(s) and replace their language_instruction column.

Examples:
  python scripts/rewrite_lance_instruction.py \
    --source bos://bucket/source/a.lance \
    --instruction "fold the shirt" \
    --target bos://bucket/fixed/

  python scripts/rewrite_lance_instruction.py \
    --source bos://bucket/source_folder/ \
    --instruction "fold the shirt" \
    --target bos://bucket/fixed_folder/
"""

from __future__ import annotations

import sys
import shutil
import argparse
import json
from typing import TextIO
from pathlib import Path
from dataclasses import dataclass

import lance
import pyarrow as pa

LANCE_SUFFIX = ".lance"
REQUIRED_COLUMNS = {"episode_index", "language_instruction"}
TASK_INSTRUCTION_COLUMNS = {"task_json_instruction", "tasks_json_instruction"}


@dataclass(frozen=True)
class RewriteItem:
    source: str
    target: str


@dataclass(frozen=True)
class RewriteResult:
    source: str
    target: str
    rows: int
    episodes: int


def is_remote_uri(value: str) -> bool:
    return value.startswith(("bos://", "s3://"))


def to_fsspec_uri(uri: str) -> str:
    if uri.startswith("bos://"):
        return "s3://" + uri[len("bos://") :]
    return uri


def to_lance_uri(uri: str) -> str:
    if uri.startswith("bos://"):
        return "s3://" + uri[len("bos://") :]
    return uri


def strip_trailing_slash(value: str) -> str:
    return value.rstrip("/")


def basename(value: str) -> str:
    return strip_trailing_slash(value).rsplit("/", 1)[-1]


def is_lance_path(value: str) -> bool:
    return strip_trailing_slash(value).endswith(LANCE_SUFFIX)


def join_uri_or_path(parent: str, child: str) -> str:
    return strip_trailing_slash(parent) + "/" + child


def sql_string_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def format_bytes(value: int | None) -> str:
    if value is None:
        return "unknown size"
    amount = float(value)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if amount < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return f"{amount:.1f} TB"


def report(progress: TextIO | None, message: str) -> None:
    if progress is not None:
        print(message, file=progress, flush=True)


def _remote_scheme(uri: str) -> str:
    return uri.split("://", 1)[0]


def _list_direct_lance_children(source_dir: str) -> list[str]:
    if is_remote_uri(source_dir):
        import fsspec

        fs, fs_path = fsspec.core.url_to_fs(to_fsspec_uri(source_dir))
        scheme = _remote_scheme(source_dir)
        children = []
        for entry in fs.ls(fs_path.rstrip("/"), detail=False):
            child = f"{scheme}://{entry.rstrip('/')}"
            if is_lance_path(child):
                children.append(child)
        return sorted(children, key=basename)

    root = Path(source_dir)
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"source directory does not exist: {source_dir}")
    return sorted(
        (child.as_posix() for child in root.iterdir() if child.is_dir() and child.name.endswith(LANCE_SUFFIX)),
        key=basename,
    )


def plan_rewrites(source: str, target: str) -> list[RewriteItem]:
    source = strip_trailing_slash(str(source))
    target = strip_trailing_slash(str(target))
    if not source:
        raise ValueError("--source is required")
    if not target:
        raise ValueError("--target is required")

    if is_lance_path(source):
        target_dataset = target if is_lance_path(target) else join_uri_or_path(target, basename(source))
        if strip_trailing_slash(source) == strip_trailing_slash(target_dataset):
            raise ValueError("target must be different from source")
        return [RewriteItem(source=source, target=target_dataset)]

    if is_lance_path(target):
        raise ValueError("when source is a directory/prefix, target must also be a directory/prefix")

    children = _list_direct_lance_children(source)
    if not children:
        raise FileNotFoundError(f"no direct *.lance children found under source: {source}")
    return [RewriteItem(source=child, target=join_uri_or_path(target, basename(child))) for child in children]


def _path_exists(uri_or_path: str) -> bool:
    if is_remote_uri(uri_or_path):
        import fsspec

        fs, fs_path = fsspec.core.url_to_fs(to_fsspec_uri(uri_or_path))
        return bool(fs.exists(fs_path))
    return Path(uri_or_path).exists()


def _remove_tree(uri_or_path: str) -> None:
    if is_remote_uri(uri_or_path):
        import fsspec

        fs, fs_path = fsspec.core.url_to_fs(to_fsspec_uri(uri_or_path))
        if fs.exists(fs_path):
            fs.rm(fs_path, recursive=True)
        return
    shutil.rmtree(uri_or_path)


def _copy_tree(source: str, target: str, *, overwrite: bool, progress: TextIO | None = None) -> None:
    if _path_exists(target):
        if not overwrite:
            raise FileExistsError(f"target already exists: {target} (use --overwrite to replace it)")
        _remove_tree(target)

    if not is_remote_uri(source) and not is_remote_uri(target):
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        files = [path for path in Path(source).rglob("*") if path.is_file()]
        total_bytes = sum(path.stat().st_size for path in files)
        report(progress, f"copying {len(files)} file(s), {format_bytes(total_bytes)}: {source} -> {target}")
        shutil.copytree(source, target)
        for index, path in enumerate(files, start=1):
            rel = path.relative_to(source).as_posix()
            report(progress, f"[{index}/{len(files)}] copied {rel} ({format_bytes(path.stat().st_size)})")
        report(progress, f"copied {len(files)} file(s), {format_bytes(total_bytes)}")
        return

    import fsspec

    src_fs, src_path = fsspec.core.url_to_fs(to_fsspec_uri(source))
    dst_fs, dst_path = fsspec.core.url_to_fs(to_fsspec_uri(target))
    if not src_fs.exists(src_path):
        raise FileNotFoundError(f"source dataset does not exist: {source}")

    src_files = sorted(src_fs.find(src_path))
    sizes: dict[str, int | None] = {}
    total_bytes = 0
    total_known = True
    for src_file in src_files:
        try:
            size = int(src_fs.info(src_file).get("size", 0))
        except (OSError, KeyError, TypeError, ValueError):
            size = None
        sizes[src_file] = size
        if size is None:
            total_known = False
        else:
            total_bytes += size
    report(
        progress,
        f"copying {len(src_files)} file(s), {format_bytes(total_bytes) if total_known else 'unknown total size'}: "
        f"{source} -> {target}",
    )

    for index, src_file in enumerate(src_files, start=1):
        rel = src_file[len(src_path.rstrip("/")) :].lstrip("/")
        dst_file = dst_path.rstrip("/") + "/" + rel
        parent = dst_file.rsplit("/", 1)[0]
        try:
            dst_fs.makedirs(parent, exist_ok=True)
        except (AttributeError, NotImplementedError):
            pass
        with src_fs.open(src_file, "rb") as src_handle, dst_fs.open(dst_file, "wb") as dst_handle:
            shutil.copyfileobj(src_handle, dst_handle, length=8 * 1024 * 1024)
        report(progress, f"[{index}/{len(src_files)}] copied {rel} ({format_bytes(sizes[src_file])})")
    report(progress, f"copied {len(src_files)} file(s), {format_bytes(total_bytes) if total_known else 'unknown size'}")


def _validate_rewritable_dataset(ds: lance.LanceDataset, source: str) -> None:
    schema_names = {field.name for field in ds.schema}
    missing = sorted(REQUIRED_COLUMNS - schema_names)
    if missing:
        raise RuntimeError(f"{source}: required column(s) missing: {missing}")


def _count_episodes(ds: lance.LanceDataset) -> int:
    table = ds.to_table(columns=["episode_index"])
    values = table["episode_index"].to_pylist()
    return len(set(int(value) for value in values))


def _assert_instruction_written(ds: lance.LanceDataset, instruction: str) -> None:
    table = ds.to_table(columns=["language_instruction"])
    values = table["language_instruction"].to_pylist()
    bad = next((value for value in values if value != instruction), None)
    if bad is not None:
        raise RuntimeError(f"verification failed: found language_instruction={bad!r}, expected {instruction!r}")


def _assert_task_instruction_columns_written(ds: lance.LanceDataset, instruction: str) -> None:
    columns = [field.name for field in ds.schema if field.name in TASK_INSTRUCTION_COLUMNS]
    if not columns:
        return
    table = ds.to_table(columns=columns)
    for column in columns:
        values = table[column].to_pylist()
        bad = next((value for value in values if value != instruction), None)
        if bad is not None:
            raise RuntimeError(f"verification failed: found {column}={bad!r}, expected {instruction!r}")


def _assert_task_metadata_written(ds: lance.LanceDataset, instruction: str) -> None:
    metadata = ds.schema.metadata or {}
    raw_tasks = metadata.get(b"lerobot:tasks_json") or metadata.get("lerobot:tasks_json")
    if raw_tasks is None:
        raise RuntimeError("verification failed: missing lerobot:tasks_json schema metadata")
    tasks_text = raw_tasks.decode("utf-8") if isinstance(raw_tasks, bytes) else str(raw_tasks)
    tasks = json.loads(tasks_text)
    expected = {"0": instruction}
    if tasks != expected:
        raise RuntimeError(f"verification failed: lerobot:tasks_json={tasks!r}, expected {expected!r}")


def _assert_task_index_zero(ds: lance.LanceDataset) -> None:
    schema_names = {field.name for field in ds.schema}
    if "task_index" not in schema_names:
        raise RuntimeError("verification failed: missing task_index column")
    values = ds.to_table(columns=["task_index"])["task_index"].to_pylist()
    bad = next((value for value in values if int(value) != 0), None)
    if bad is not None:
        raise RuntimeError(f"verification failed: found task_index={bad!r}, expected 0")


def _blob_columns(ds: lance.LanceDataset) -> list[str]:
    out = []
    for field in ds.schema:
        if getattr(field.type, "extension_name", None) == "lance.blob.v2":
            out.append(field.name)
            continue
        metadata = field.metadata or {}
        extension_name = metadata.get(b"ARROW:extension:name") or metadata.get("ARROW:extension:name")
        blob_encoding = metadata.get(b"lance-encoding:blob") or metadata.get("lance-encoding:blob")
        if extension_name == b"lance.blob.v2" or extension_name == "lance.blob.v2" or blob_encoding == b"true":
            out.append(field.name)
    return out


def _schema_metadata_with_instruction(ds: lance.LanceDataset, instruction: str) -> dict[bytes, bytes] | None:
    metadata = dict(ds.schema.metadata or {})
    metadata[b"lerobot:tasks_json"] = json.dumps({"0": instruction}, ensure_ascii=False).encode("utf-8")
    return metadata or None


def _ensure_target_parent(target: str) -> None:
    if not is_remote_uri(target):
        Path(target).parent.mkdir(parents=True, exist_ok=True)


def _replace_column(
    target: str,
    ds: lance.LanceDataset,
    column: str,
    values: pa.Array,
    *,
    progress: TextIO | None,
) -> lance.LanceDataset:
    if column in {field.name for field in ds.schema}:
        report(progress, f"dropping {column} ...")
        ds.drop_columns([column])
        ds = lance.dataset(to_lance_uri(target))
    report(progress, f"adding {column} ...")
    ds.add_columns(pa.table({column: values}))
    return lance.dataset(to_lance_uri(target))


def _rewrite_lightweight_columns_and_metadata(
    target: str,
    instruction: str,
    *,
    rows: int,
    progress: TextIO | None,
) -> lance.LanceDataset:
    ds = lance.dataset(to_lance_uri(target))
    _validate_rewritable_dataset(ds, target)
    report(progress, "rewriting lightweight columns ...")
    text_values = pa.array([instruction] * rows, type=pa.string())
    ds = _replace_column(target, ds, "language_instruction", text_values, progress=progress)
    for column in sorted(TASK_INSTRUCTION_COLUMNS & {field.name for field in ds.schema}):
        ds = _replace_column(target, ds, column, text_values, progress=progress)
    ds = _replace_column(target, ds, "task_index", pa.array([0] * rows, type=pa.int64()), progress=progress)

    report(progress, "patching lerobot:tasks_json ...")
    ds.update_schema_metadata({"lerobot:tasks_json": json.dumps({"0": instruction}, ensure_ascii=False)})
    return lance.dataset(to_lance_uri(target))


def rewrite_instruction_dataset(
    source: str,
    target: str,
    instruction: str,
    *,
    overwrite: bool = False,
    progress: TextIO | None = None,
) -> RewriteResult:
    if not instruction:
        raise ValueError("--instruction must not be empty")
    source = strip_trailing_slash(source)
    target = strip_trailing_slash(target)
    if strip_trailing_slash(source) == strip_trailing_slash(target):
        raise ValueError("target must be different from source")

    source_ds = lance.dataset(to_lance_uri(source))
    _validate_rewritable_dataset(source_ds, source)
    rows = int(source_ds.count_rows())
    episodes = _count_episodes(source_ds)

    report(progress, f"opened source: {rows} row(s), {episodes} episode(s)")
    if _blob_columns(source_ds):
        report(progress, "detected blob columns; using lightweight column/metadata rewrite without rebuilding blobs")
    _copy_tree(source, target, overwrite=overwrite, progress=progress)
    target_ds = _rewrite_lightweight_columns_and_metadata(
        target,
        instruction,
        rows=rows,
        progress=progress,
    )

    if int(target_ds.count_rows()) != rows:
        raise RuntimeError(f"row-count verification failed for {target}: expected {rows}, got {target_ds.count_rows()}")
    _assert_instruction_written(target_ds, instruction)
    _assert_task_instruction_columns_written(target_ds, instruction)
    _assert_task_metadata_written(target_ds, instruction)
    _assert_task_index_zero(target_ds)
    report(progress, "verified language_instruction, task_index, lerobot:tasks_json")

    return RewriteResult(source=source, target=target, rows=rows, episodes=episodes)


def patch_existing_instruction_metadata(
    target: str,
    instruction: str,
    *,
    progress: TextIO | None = None,
) -> RewriteResult:
    if not instruction:
        raise ValueError("--instruction must not be empty")
    target = strip_trailing_slash(target)
    target_ds = lance.dataset(to_lance_uri(target))
    _validate_rewritable_dataset(target_ds, target)
    rows = int(target_ds.count_rows())
    episodes = _count_episodes(target_ds)

    report(progress, f"opened target: {rows} row(s), {episodes} episode(s)")
    target_ds = _rewrite_lightweight_columns_and_metadata(
        target,
        instruction,
        rows=rows,
        progress=progress,
    )

    if int(target_ds.count_rows()) != rows:
        raise RuntimeError(f"row-count verification failed for {target}: expected {rows}, got {target_ds.count_rows()}")
    _assert_instruction_written(target_ds, instruction)
    _assert_task_instruction_columns_written(target_ds, instruction)
    _assert_task_metadata_written(target_ds, instruction)
    _assert_task_index_zero(target_ds)
    report(progress, "verified language_instruction, task_index, lerobot:tasks_json")

    return RewriteResult(source=target, target=target, rows=rows, episodes=episodes)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Copy Lance dataset(s) and rewrite language_instruction.")
    parser.add_argument(
        "--source", help="Source .lance URI/path, or directory/prefix containing *.lance."
    )
    parser.add_argument("--target", required=True, help="Target .lance URI/path, or output directory/prefix.")
    parser.add_argument("--instruction", required=True, help="New language_instruction value to write to every row.")
    parser.add_argument("--overwrite", action="store_true", help="Replace target dataset(s) if they already exist.")
    parser.add_argument(
        "--patch-existing",
        action="store_true",
        help="Patch an existing target only: verify language_instruction, then write task_index and lerobot:tasks_json.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print planned rewrites without copying or updating.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.patch_existing:
        if args.source:
            raise ValueError("--source cannot be used with --patch-existing")
        print(f"patch {args.target}")
        if args.dry_run:
            return
        result = patch_existing_instruction_metadata(
            args.target,
            args.instruction,
            progress=sys.stdout,
        )
        print(f"patched {result.rows} row(s), {result.episodes} episode(s): {result.target}")
        return

    if not args.source:
        raise ValueError("--source is required unless --patch-existing is used")
    plan = plan_rewrites(args.source, args.target)
    for item in plan:
        print(f"{item.source} -> {item.target}")
    if args.dry_run:
        return
    for item in plan:
        result = rewrite_instruction_dataset(
            item.source,
            item.target,
            args.instruction,
            overwrite=args.overwrite,
            progress=sys.stdout,
        )
        print(f"rewrote {result.rows} row(s), {result.episodes} episode(s): {result.target}")


if __name__ == "__main__":
    main()
