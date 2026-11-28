"""Stable display identities across wrapper exits, reconnects and server restarts."""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from registry import RuntimeRegistry


class StableIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = {"codex": {"label": "Codex", "color": "#10a37f"}}
        self.reg = self.restart()

    def restart(self):
        reg = RuntimeRegistry(self.tmp.name)
        reg.seed(self.config)
        return reg

    def test_browser_roster_follows_rename_without_exposing_token(self):
        first = self.reg.register("codex")
        self.reg.rename(first["name"], "builder")
        roster = self.reg.get_agent_config()
        self.assertNotIn(first["name"], roster)
        self.assertEqual(roster["builder"]["identity_id"], first["identity_id"])
        self.assertEqual(roster["builder"]["base"], "codex")
        self.assertNotIn("token", roster["builder"])

    def test_concurrent_instance_never_renames_or_recolors_survivor(self):
        first = self.reg.register("codex")
        second = self.reg.register("codex")
        self.assertEqual(second["name"], "codex-2")
        self.assertEqual(self.reg.resolve_token(first["token"])["name"], "codex")
        self.reg.deregister("codex")
        survivor = self.reg.resolve_token(second["token"])
        for key in ("name", "label", "color", "slot"):
            self.assertEqual(survivor[key], second[key])
        fresh = self.reg.register("codex")
        self.assertEqual(fresh["name"], "codex")
        self.assertEqual(fresh["color"], first["color"])

    def test_immediate_clean_relaunch_keeps_display_but_rotates_token(self):
        first = self.reg.register("codex")
        self.reg.deregister("codex")
        fresh = self.reg.register("codex")
        for key in ("name", "label", "color", "slot"):
            self.assertEqual(fresh[key], first[key])
        self.assertNotEqual(first["token"], fresh["token"])
        self.assertIsNone(self.reg.resolve_token(first["token"]))

    def test_custom_name_label_and_color_survive_clean_exit_and_restart(self):
        first = self.reg.register("codex")
        self.reg.rename("codex", "builder", "Team Builder")
        self.reg.deregister("builder")
        self.config["codex"]["color"] = "#abcdef"
        fresh = self.restart().register("codex")
        self.assertEqual(fresh["name"], "builder")
        self.assertEqual(fresh["label"], "Team Builder")
        self.assertEqual(fresh["color"], first["color"])
        data = json.loads((Path(self.tmp.name) / "registry.json").read_text())
        self.assertEqual(set(data["profiles"]["codex"]["1"]), {"name", "label", "color"})

    def test_stale_heartbeat_reuses_slot_before_background_sweep(self):
        first = self.reg.register("codex")
        with patch("registry.time.time", return_value=first["registered_at"] + 16):
            fresh = self.reg.register("codex")
        self.assertEqual(fresh["name"], "codex")
        self.assertEqual(fresh["_expired"], ["codex"])
        self.assertIsNone(self.reg.resolve_token(first["token"]))

    def test_authenticated_activity_keeps_live_slot(self):
        first = self.reg.register("codex")
        with patch("registry.time.time", return_value=first["registered_at"] + 14):
            self.reg.resolve_token(first["token"])
        with patch("registry.time.time", return_value=first["registered_at"] + 20):
            second = self.reg.register("codex")
        self.assertEqual(second["name"], "codex-2")
        self.assertNotIn("_expired", second)

    def test_claim_exact_primary_does_not_claim_other_pending_instance(self):
        self.reg.register("codex")
        self.reg.register("codex")
        self.reg._instances["codex-2"].state = "pending"
        self.assertEqual(self.reg.claim("codex")["name"], "codex")

    def test_legacy_slot1_migrates_on_token_reconnect(self):
        first = self.reg.register("codex")
        path = Path(self.tmp.name) / "registry.json"
        data = json.loads(path.read_text())
        inst = data["instances"].pop("codex")
        inst.update(name="codex-1", label="Codex 1")
        data["instances"]["codex-1"] = inst
        data.pop("profiles")  # pre-upgrade format
        path.write_text(json.dumps(data))
        (Path(self.tmp.name) / "renames.json").write_text(json.dumps({"codex": "codex-1"}))
        reg = self.restart()
        restored = reg.resolve_token(first["token"])
        self.assertEqual(restored["name"], "codex")
        self.assertEqual(restored["label"], "Codex")
        self.assertEqual(restored["color"], first["color"])
        self.assertEqual(reg.resolve_name("codex"), "codex")

    def test_reused_name_overrides_old_rename_chain(self):
        self.reg.register("codex")
        self.reg.rename("codex", "builder")
        self.reg.rename("builder", "codex")
        self.assertEqual(self.reg.resolve_name("codex"), "codex")

    def test_requested_name_registers_identity_and_rejects_collisions(self):
        first = self.reg.register("codex", requested_name="c2")
        self.assertEqual(first["name"], "c2")
        self.assertEqual(self.reg.resolve_token(first["token"])["name"], "c2")
        with self.assertRaises(ValueError):
            self.reg.register("codex", requested_name="c2")
        self.assertEqual(self.reg.resolve_token(first["token"])["name"], "c2")
        second = self.reg.register("codex", requested_name="codex-3")
        self.assertEqual((second["name"], second["slot"]), ("codex-3", 2))
        self.assertEqual(self.reg.register("codex")["slot"], 4)

    def test_requested_name_rejects_invalid_reserved_and_other_family(self):
        self.reg.seed({"claude": {"label": "Claude"}})
        for name in ("", "A", "x y", "../x", "system", "user", "claude-2"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.reg.register("codex", requested_name=name)
        self.assertEqual(self.reg.register("codex")["name"], "codex")

    def test_explicit_canonical_id_and_alias_work_in_both_launch_orders(self):
        for names in (("c1", "codex"), ("codex", "c1")):
            with self.subTest(names=names), tempfile.TemporaryDirectory() as tmp:
                reg = RuntimeRegistry(tmp)
                reg.seed(self.config)
                for slot, name in enumerate(names, start=1):
                    inst = reg.register("codex", requested_name=name)
                    self.assertEqual((inst["name"], inst["slot"]), (name, slot))
                self.assertEqual(reg.resolve_to_instances("codex"), ["codex"])
                with self.assertRaisesRegex(ValueError, "Name already in use: codex"):
                    reg.register("codex", requested_name="codex")

    def test_concurrent_registrations_reserve_slots_and_ids_atomically(self):
        ids = ("codex", "codex-3", "c1", "c1", None, None, None, None)
        start = Barrier(len(ids))

        def register(name):
            start.wait(timeout=5)
            try:
                return self.reg.register("codex", requested_name=name)
            except ValueError as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=len(ids)) as workers:
            # Run the automatic launches alongside the exact-ID claims. An
            # automatic launch is allowed to win either of the default IDs.
            results = list(workers.map(register, ids))
        successes = [result for result in results if isinstance(result, dict)]
        errors = [result for result in results if isinstance(result, str)]
        self.assertEqual(sum(inst["name"] == "c1" for inst in successes), 1)
        self.assertIn("Name already in use: c1", errors)
        self.assertEqual(len({inst["slot"] for inst in successes}), len(successes))
        self.assertEqual(len({inst["name"] for inst in successes}), len(successes))
        self.assertEqual(set(self.reg.get_all_names()), {inst["name"] for inst in successes})
        restarted = self.restart()
        for inst in successes:
            self.assertEqual(restarted.resolve_token(inst["token"])["slot"], inst["slot"])

    def test_saved_profile_can_use_slot_with_blocked_default_id(self):
        self.reg.register("codex", requested_name="c1")
        self.reg.register("codex", requested_name="codex")
        self.reg.deregister("c1")
        fresh = self.reg.register("codex")
        self.assertEqual((fresh["name"], fresh["slot"]), ("c1", 1))

    def test_automatic_registration_uses_default_when_saved_id_is_taken(self):
        self.reg.register("codex", requested_name="c1")
        self.reg.register("codex", requested_name="worker")
        self.reg.deregister("worker")
        self.reg.deregister("c1")
        self.reg.register("codex", requested_name="worker")
        fresh = self.reg.register("codex")
        self.assertEqual((fresh["name"], fresh["slot"]), ("codex-2", 2))

    def test_exact_numbered_id_survives_restart(self):
        for name in ("codex-1", "codex-0"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                reg = RuntimeRegistry(tmp)
                reg.seed(self.config)
                inst = reg.register("codex", requested_name=name)
                self.assertEqual((inst["name"], inst["slot"]), (name, 1))
                reg = RuntimeRegistry(tmp)
                reg.seed(self.config)
                restored = reg.resolve_token(inst["token"])
                self.assertEqual((restored["name"], restored["slot"]), (name, 1))

    def test_claim_requires_exact_id_and_leaves_ambiguous_family_untouched(self):
        self.reg.register("codex", requested_name="c1")
        self.reg.register("codex", requested_name="c2")
        before = self.reg.get_all()
        self.assertIn("exact registered chat ID", self.reg.claim("codex", "worker"))
        self.assertEqual(self.reg.get_all(), before)

    def test_requested_name_can_replace_expired_instance(self):
        first = self.reg.register("codex", requested_name="c2")
        with patch("registry.time.time", return_value=first["registered_at"] + 16):
            fresh = self.reg.register("codex", requested_name="c2")
        self.assertEqual(fresh["name"], "c2")
        self.assertIn("c2", fresh["_expired"])
        self.assertNotEqual(first["token"], fresh["token"])
        self.assertIsNone(self.reg.resolve_token(first["token"]))
        self.assertEqual(self.reg.resolve_token(fresh["token"])["name"], "c2")

    def test_expired_chat_id_can_be_reused_by_another_configured_type(self):
        self.reg.seed({"claude": {"label": "Claude"}})
        first = self.reg.register("codex", requested_name="worker")
        with patch("registry.time.time", return_value=first["registered_at"] + 16):
            fresh = self.reg.register("claude", requested_name="worker")
        self.assertEqual((fresh["name"], fresh["base"], fresh["slot"]), ("worker", "claude", 1))
        self.assertEqual(fresh["_expired"], ["worker"])
        self.assertIsNone(self.reg.resolve_token(first["token"]))

    def test_failed_name_validation_does_not_expire_other_instances(self):
        first = self.reg.register("codex", requested_name="c2")
        second = self.reg.register("codex", requested_name="builder")
        with patch("registry.time.time", return_value=first["registered_at"] + 16):
            self.reg._instances[second["name"]].last_seen = first["registered_at"] + 16
            with self.assertRaises(ValueError):
                self.reg.register("codex", requested_name="builder")
        self.assertIn("c2", self.reg._instances)
        self.assertNotIn("c2", self.reg._reclaimable)


if __name__ == "__main__":
    unittest.main()
