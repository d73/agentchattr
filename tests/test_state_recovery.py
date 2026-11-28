import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from registry import RuntimeRegistry
from summaries import SummaryStore


class StateRecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def assert_quarantined(self, path, original):
        backups = list(path.parent.glob(f"{path.name}.corrupt-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)

    def test_truncated_registry_survives_fresh_registration(self):
        path = self.root / "registry.json"
        original = b'{"instances":'
        path.write_bytes(original)
        with self.assertLogs("atomic_io", level="WARNING"):
            registry = RuntimeRegistry(self.root)
        registry.seed({"codex": {}})
        registry.register("codex")
        self.assertIn("codex", json.loads(path.read_text())["instances"])
        self.assert_quarantined(path, original)

    def test_invalid_registry_section_preserves_valid_sessions(self):
        registry = RuntimeRegistry(self.root)
        registry.seed({"codex": {}})
        prior = registry.register("codex")
        path = self.root / "registry.json"
        data = json.loads(path.read_text())
        data["reclaimable"] = []
        original = json.dumps(data).encode()
        path.write_bytes(original)
        with self.assertLogs("atomic_io", level="WARNING"):
            recovered = RuntimeRegistry(self.root)
        recovered.seed({"codex": {}})
        self.assertEqual(recovered.resolve_token(prior["token"])["name"], "codex")
        self.assert_quarantined(path, original)

    def test_truncated_summaries_survive_next_save(self):
        path = self.root / "summaries.json"
        original = b'{"general":'
        path.write_bytes(original)
        with self.assertLogs("atomic_io", level="WARNING"):
            summaries = SummaryStore(path)
        summaries.write("general", "replacement", "codex")
        self.assertEqual(json.loads(path.read_text())["general"]["text"], "replacement")
        self.assert_quarantined(path, original)

    def test_failed_quarantine_preserves_original_and_blocks_the_load(self):
        path = self.root / "summaries.json"
        original = b'{"general":'
        path.write_bytes(original)
        with patch.object(Path, "replace", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                SummaryStore(path)
        self.assertEqual(path.read_bytes(), original)
