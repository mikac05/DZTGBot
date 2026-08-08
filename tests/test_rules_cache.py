from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dztgbot.rules import RulesStore


class RulesCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_unchanged_signature_avoids_file_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rules.txt"
            path.write_text("rules", encoding="utf-8")
            store = RulesStore(path)
            await store.initialize()
            with patch.object(store, "_read_validated", wraps=store._read_validated) as reader:
                self.assertEqual(await store.current_rules(), "rules")
                self.assertEqual(await store.current_rules(), "rules")
                self.assertEqual(reader.call_count, 0)

    async def test_oversize_change_preserves_lkg_and_is_not_reread(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rules.txt"
            path.write_text("good", encoding="utf-8")
            store = RulesStore(path, max_bytes=8)
            await store.initialize()
            path.write_text("x" * 9, encoding="utf-8")
            with patch.object(store, "_read_validated", wraps=store._read_validated) as reader:
                self.assertEqual(await store.current_rules(), "good")
                self.assertEqual(await store.current_rules(), "good")
                self.assertEqual(reader.call_count, 1)


if __name__ == "__main__":
    unittest.main()
