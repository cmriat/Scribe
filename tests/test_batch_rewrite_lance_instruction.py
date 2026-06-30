from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import lance
import pyarrow as pa

from scribe.tools.batch_rewrite_lance_instruction import load_batch_config, plan_batch_rewrites, run_batch_rewrites


def _write_tiny_lance(path: Path, instruction: str = "old") -> None:
    table = pa.table(
        {
            "episode_index": pa.array([0, 0], type=pa.int64()),
            "language_instruction": pa.array([instruction, instruction], type=pa.string()),
            "value": pa.array([1, 2], type=pa.int64()),
        }
    )
    lance.write_dataset(table, path, mode="create")


class BatchRewriteLanceInstructionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="scribe_lance_batch_rewrite_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_load_batch_config_accepts_items_object_and_default_overwrite(self) -> None:
        config = self.tmp / "batch.json"
        config.write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "source": "source.lance",
                            "target": "target",
                            "instruction": "new",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        items = load_batch_config(config, default_overwrite=True)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source, "source.lance")
        self.assertEqual(items[0].target, "target")
        self.assertEqual(items[0].instruction, "new")
        self.assertTrue(items[0].overwrite)

    def test_load_batch_config_item_overwrite_overrides_default(self) -> None:
        config = self.tmp / "batch.json"
        config.write_text(
            json.dumps(
                [
                    {
                        "source": "source.lance",
                        "target": "target",
                        "instruction": "new",
                        "overwrite": False,
                    }
                ]
            ),
            encoding="utf-8",
        )

        items = load_batch_config(config, default_overwrite=True)

        self.assertFalse(items[0].overwrite)

    def test_plan_batch_rewrites_expands_directory_source(self) -> None:
        source_dir = self.tmp / "source"
        target_dir = self.tmp / "target"
        _write_tiny_lance(source_dir / "a.lance")
        _write_tiny_lance(source_dir / "b.lance")
        config = self.tmp / "batch.json"
        config.write_text(
            json.dumps(
                [
                    {
                        "source": source_dir.as_posix(),
                        "target": target_dir.as_posix(),
                        "instruction": "new",
                    }
                ]
            ),
            encoding="utf-8",
        )

        planned = plan_batch_rewrites(load_batch_config(config))

        self.assertEqual(
            [(Path(item.source).name, Path(item.target).name, item.instruction) for item in planned],
            [("a.lance", "a.lance", "new"), ("b.lance", "b.lance", "new")],
        )

    def test_run_batch_rewrites_updates_each_item_instruction(self) -> None:
        source_a = self.tmp / "a.lance"
        source_b = self.tmp / "b.lance"
        target_dir = self.tmp / "out"
        _write_tiny_lance(source_a)
        _write_tiny_lance(source_b)
        config = self.tmp / "batch.json"
        config.write_text(
            json.dumps(
                [
                    {
                        "source": source_a.as_posix(),
                        "target": target_dir.as_posix(),
                        "instruction": "instruction A",
                    },
                    {
                        "source": source_b.as_posix(),
                        "target": target_dir.as_posix(),
                        "instruction": "instruction B",
                    },
                ]
            ),
            encoding="utf-8",
        )

        results = run_batch_rewrites(load_batch_config(config))

        self.assertEqual(len(results), 2)
        target_a_values = (
            lance.dataset(target_dir / "a.lance")
            .to_table(columns=["language_instruction"])["language_instruction"]
            .to_pylist()
        )
        target_b_values = (
            lance.dataset(target_dir / "b.lance")
            .to_table(columns=["language_instruction"])["language_instruction"]
            .to_pylist()
        )
        self.assertEqual(target_a_values, ["instruction A", "instruction A"])
        self.assertEqual(target_b_values, ["instruction B", "instruction B"])


if __name__ == "__main__":
    unittest.main()
