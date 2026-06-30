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
from lance import Blob, blob_array

LANCE_SUFFIX = ".lance"
REQUIRED_COLUMNS = {"episode_index", "language_instruction"}


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
    schema_names = {field.name for field in ds.schema}
    if b"lerobot:tasks_json" in metadata or "task_index" in schema_names:
        metadata[b"lerobot:tasks_json"] = json.dumps({"0": instruction}, ensure_ascii=False).encode("utf-8")
    return metadata or None


def _episode_slices(ds: lance.LanceDataset) -> list[tuple[int, int, int]]:
    values = ds.to_table(columns=["episode_index"])["episode_index"].to_pylist()
    if not values:
        return []

    slices: list[tuple[int, int, int]] = []
    start = 0
    current = int(values[0])
    for index, value in enumerate(values[1:], start=1):
        episode = int(value)
        if episode == current:
            continue
        slices.append((start, index - start, current))
        start = index
        current = episode
    slices.append((start, len(values) - start, current))
    return slices


def _rebuild_blob_column(
    source_ds: lance.LanceDataset,
    col: str,
    *,
    offset: int,
    length: int,
    ref_id_start: int,
) -> pa.Array:
    desc = source_ds.to_table(columns=[col], limit=length, offset=offset)[col].combine_chunks()
    if len(desc) != length:
        raise RuntimeError(f"{col}: descriptor row count mismatch, expected {length}, got {len(desc)}")

    try:
        ref_ids = desc.field("ref_id").to_pylist()
        positions = desc.field("position").to_pylist()
        sizes = desc.field("size").to_pylist()
        blob_ids = desc.field("blob_id").to_pylist() if "blob_id" in desc.type else [None] * length
        blob_uris = desc.field("blob_uri").to_pylist() if "blob_uri" in desc.type else [None] * length
    except (AttributeError, KeyError):
        ref_ids = list(range(length))
        positions = list(range(length))
        sizes = [None] * length
        blob_ids = [None] * length
        blob_uris = [None] * length

    blob_keys = [
        (
            index if ref_id is None else int(ref_id),
            None if position is None else int(position),
            None if size is None else int(size),
            None if blob_id is None else int(blob_id),
            blob_uri,
        )
        for index, (ref_id, position, size, blob_id, blob_uri) in enumerate(
            zip(ref_ids, positions, sizes, blob_ids, blob_uris)
        )
    ]
    first_idx_by_key: dict[tuple[object, ...], int] = {}
    for index, key in enumerate(blob_keys):
        if key not in first_idx_by_key:
            first_idx_by_key[key] = index

    unique_keys = list(first_idx_by_key.keys())
    source_indices = [offset + first_idx_by_key[key] for key in unique_keys]
    payloads = source_ds.take_blobs_data(col, indices=source_indices)
    if len(payloads) != len(unique_keys):
        raise RuntimeError(f"{col}: take_blobs_data returned {len(payloads)} payloads, expected {len(unique_keys)}")
    payload_by_key = dict(zip(unique_keys, payloads))

    new_ref_by_old: dict[tuple[object, ...], int] = {}
    blobs = []
    next_ref = ref_id_start
    for key in blob_keys:
        if key not in new_ref_by_old:
            new_ref_by_old[key] = next_ref
            blobs.append(Blob.from_bytes(payload_by_key[key], ref_id=next_ref))
            next_ref += 1
        else:
            blobs.append(Blob.ref(new_ref_by_old[key]))
    return blob_array(blobs)


def _table_with_rewritten_instruction(
    ds: lance.LanceDataset,
    *,
    offset: int,
    length: int,
    instruction: str,
    blob_columns: list[str],
) -> pa.Table:
    names = [field.name for field in ds.schema]
    scalar_columns = [name for name in names if name not in blob_columns]
    table = ds.to_table(columns=scalar_columns, limit=length, offset=offset)
    arrays: list[pa.Array | pa.ChunkedArray] = []
    fields: list[pa.Field] = []

    for field in ds.schema:
        if field.name == "language_instruction":
            arrays.append(pa.array([instruction] * length, type=pa.string()))
            fields.append(field.with_type(pa.string()))
        elif field.name == "task_index":
            arrays.append(pa.array([0] * length, type=field.type))
            fields.append(field)
        elif field.name in blob_columns:
            arr = _rebuild_blob_column(ds, field.name, offset=offset, length=length, ref_id_start=offset + 1)
            arrays.append(arr)
            fields.append(pa.field(field.name, arr.type, nullable=field.nullable, metadata=field.metadata))
        else:
            arrays.append(table[field.name])
            fields.append(field)

    return pa.Table.from_arrays(arrays, schema=pa.schema(fields, metadata=_schema_metadata_with_instruction(ds, instruction)))


def _ensure_target_parent(target: str) -> None:
    if not is_remote_uri(target):
        Path(target).parent.mkdir(parents=True, exist_ok=True)


def _rewrite_dataset_by_rebuild(
    source_ds: lance.LanceDataset,
    target: str,
    instruction: str,
    *,
    rows: int,
    overwrite: bool,
    progress: TextIO | None,
) -> None:
    if _path_exists(target):
        if not overwrite:
            raise FileExistsError(f"target already exists: {target} (use --overwrite to replace it)")
        _remove_tree(target)
    _ensure_target_parent(target)
    blob_columns = _blob_columns(source_ds)
    episode_slices = _episode_slices(source_ds)
    report(
        progress,
        f"rebuilding dataset with blob columns ({len(blob_columns)} blob column(s)); "
        f"{len(episode_slices)} episode fragment(s)",
    )

    first = True
    processed = 0
    for index, (offset, length, episode_index) in enumerate(episode_slices, start=1):
        episode_table = _table_with_rewritten_instruction(
            source_ds,
            offset=offset,
            length=length,
            instruction=instruction,
            blob_columns=blob_columns,
        )
        lance.write_dataset(
            episode_table,
            to_lance_uri(target),
            mode="create" if first else "append",
            data_storage_version="2.2",
        )
        first = False
        processed += length
        report(
            progress,
            f"[{index}/{len(episode_slices)}] episode_index={episode_index}: "
            f"rewrote {length} row(s), total {processed}/{rows}",
        )


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
        _rewrite_dataset_by_rebuild(
            source_ds,
            target,
            instruction,
            rows=rows,
            overwrite=overwrite,
            progress=progress,
        )
    else:
        _copy_tree(source, target, overwrite=overwrite, progress=progress)
        report(progress, "updating language_instruction ...")
        target_ds = lance.dataset(to_lance_uri(target))
        _validate_rewritable_dataset(target_ds, target)
        target_ds.update({"language_instruction": sql_string_literal(instruction)})

    target_ds = lance.dataset(to_lance_uri(target))
    if int(target_ds.count_rows()) != rows:
        raise RuntimeError(f"row-count verification failed for {target}: expected {rows}, got {target_ds.count_rows()}")
    _assert_instruction_written(target_ds, instruction)
    report(progress, "verified language_instruction")

    return RewriteResult(source=source, target=target, rows=rows, episodes=episodes)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Copy Lance dataset(s) and rewrite language_instruction.")
    parser.add_argument(
        "--source", required=True, help="Source .lance URI/path, or directory/prefix containing *.lance."
    )
    parser.add_argument("--target", required=True, help="Target .lance URI/path, or output directory/prefix.")
    parser.add_argument("--instruction", required=True, help="New language_instruction value to write to every row.")
    parser.add_argument("--overwrite", action="store_true", help="Replace target dataset(s) if they already exist.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned rewrites without copying or updating.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
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
