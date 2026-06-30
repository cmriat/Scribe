from __future__ import annotations

import shutil
import tempfile
import unittest
from io import StringIO
from pathlib import Path

import lance
import pyarrow as pa
from lance import Blob, blob_array
from scribe.tools.rewrite_lance_instruction import (
    plan_rewrites,
    sql_string_literal,
    rewrite_instruction_dataset,
)


def _write_tiny_lance(path: Path, instructions: list[str] | None = None) -> None:
    instructions = instructions or ["old", "old", "other", "other"]
    table = pa.table(
        {
            "episode_index": pa.array([0, 0, 1, 1], type=pa.int64()),
            "language_instruction": pa.array(instructions, type=pa.string()),
            "value": pa.array([1, 2, 3, 4], type=pa.int64()),
        }
    )
    lance.write_dataset(table, path, mode="create")


def _write_tiny_blob_lance(path: Path) -> None:
    for episode_index, instruction, payload, mode in [
        (0, "old", b"episode-0-gop", "create"),
        (1, "other", b"episode-1-gop", "append"),
    ]:
        table = pa.table(
            {
                "episode_index": pa.array([episode_index, episode_index], type=pa.int64()),
                "language_instruction": pa.array([instruction, instruction], type=pa.string()),
                "task_index": pa.array([episode_index, episode_index], type=pa.int64()),
                "observation_images_cam_env": blob_array([Blob.from_bytes(payload, ref_id=1), Blob.ref(1)]),
            }
        )
        lance.write_dataset(
            table,
            path,
            mode=mode,
            data_storage_version="2.2",
            schema=table.schema,
        )


class RewriteLanceInstructionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="scribe_lance_rewrite_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sql_string_literal_escapes_quotes_and_backslashes(self) -> None:
        self.assertEqual(sql_string_literal("fold user's shirt \\ neatly"), "'fold user''s shirt \\\\ neatly'")

    def test_plan_single_source_uses_target_directory_and_source_name(self) -> None:
        source = self.tmp / "source" / "merged_a.lance"
        target_dir = self.tmp / "target"
        _write_tiny_lance(source)

        plan = plan_rewrites(source.as_posix(), target_dir.as_posix())

        self.assertEqual(
            [(item.source, item.target) for item in plan],
            [(source.as_posix(), (target_dir / "merged_a.lance").as_posix())],
        )

    def test_plan_directory_source_preserves_each_lance_name(self) -> None:
        source_dir = self.tmp / "source"
        target_dir = self.tmp / "target"
        _write_tiny_lance(source_dir / "a.lance")
        _write_tiny_lance(source_dir / "b.lance")
        (source_dir / "notes.txt").write_text("ignore me", encoding="utf-8")

        plan = plan_rewrites(source_dir.as_posix(), target_dir.as_posix())

        self.assertEqual(
            [(Path(item.source).name, Path(item.target).name) for item in plan],
            [("a.lance", "a.lance"), ("b.lance", "b.lance")],
        )

    def test_rewrite_instruction_dataset_copies_source_and_updates_target_only(self) -> None:
        source = self.tmp / "source.lance"
        target = self.tmp / "target.lance"
        _write_tiny_lance(source)

        result = rewrite_instruction_dataset(
            source.as_posix(),
            target.as_posix(),
            "accurate instruction",
            overwrite=False,
        )

        self.assertEqual(result.rows, 4)
        self.assertEqual(result.episodes, 2)
        source_values = (
            lance.dataset(source).to_table(columns=["language_instruction"])["language_instruction"].to_pylist()
        )
        target_values = (
            lance.dataset(target).to_table(columns=["language_instruction"])["language_instruction"].to_pylist()
        )
        self.assertEqual(source_values, ["old", "old", "other", "other"])
        self.assertEqual(target_values, ["accurate instruction"] * 4)

    def test_rewrite_instruction_dataset_reports_progress(self) -> None:
        source = self.tmp / "source.lance"
        target = self.tmp / "target.lance"
        progress = StringIO()
        _write_tiny_lance(source)

        rewrite_instruction_dataset(
            source.as_posix(),
            target.as_posix(),
            "accurate instruction",
            overwrite=False,
            progress=progress,
        )

        output = progress.getvalue()
        self.assertIn("copying", output)
        self.assertIn("copied", output)
        self.assertIn("updating language_instruction", output)
        self.assertIn("verified language_instruction", output)

    def test_rewrite_instruction_dataset_rebuilds_blob_dataset_and_updates_column(self) -> None:
        source = self.tmp / "source.lance"
        target = self.tmp / "target.lance"
        progress = StringIO()
        _write_tiny_blob_lance(source)

        rewrite_instruction_dataset(
            source.as_posix(),
            target.as_posix(),
            "accurate instruction",
            overwrite=False,
            progress=progress,
        )

        source_ds = lance.dataset(source)
        target_ds = lance.dataset(target)
        source_values = source_ds.to_table(columns=["language_instruction"])["language_instruction"].to_pylist()
        target_values = target_ds.to_table(columns=["language_instruction"])["language_instruction"].to_pylist()
        target_task_indexes = target_ds.to_table(columns=["task_index"])["task_index"].to_pylist()
        self.assertEqual(source_values, ["old", "old", "other", "other"])
        self.assertEqual(target_values, ["accurate instruction"] * 4)
        self.assertEqual(target_task_indexes, [0, 0, 0, 0])
        self.assertEqual(target_ds.take_blobs("observation_images_cam_env", indices=[0])[0].read(), b"episode-0-gop")
        self.assertEqual(target_ds.take_blobs("observation_images_cam_env", indices=[1])[0].read(), b"episode-0-gop")
        self.assertEqual(target_ds.take_blobs("observation_images_cam_env", indices=[2])[0].read(), b"episode-1-gop")
        self.assertEqual(target_ds.take_blobs("observation_images_cam_env", indices=[3])[0].read(), b"episode-1-gop")
        self.assertIn("rebuilding dataset with blob columns", progress.getvalue())
        self.assertIn("episode fragment(s)", progress.getvalue())


if __name__ == "__main__":
    unittest.main()
