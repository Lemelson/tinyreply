"""Independent real CLI stage-attribution regression; inherited fixture cases are not rerun here."""
import unittest

from tests import test_i09_cli_rate as cli_fixture


class StageRace(cli_fixture.CliRate):
    def stage(self, next_budget):
        self.world(comments=("only",), cap=60, scale=1)
        self.assertEqual(self.bot("arm", "1").returncode, 0)
        hold = self.fake.control("/hold", {"add": {"kind": "dm", "phase": "after"}})["id"]
        process = self.start("once")
        caught = self.fake.control("/hold", {"wait": hold, "timeout_real_sec": 3})
        self.assertTrue(caught["caught"])
        if next_budget is not None:
            command = ("disarm",) if next_budget == 0 else ("arm", str(next_budget))
            self.assertEqual(self.bot(*command).returncode, 0)
        self.fake.control("/hold", {"release": hold})
        self.assertEqual(self.finish(process, timeout=5), 0)
        events = self.rows("SELECT text FROM outbox WHERE key='stage_done'")
        budget = self.rows("SELECT value FROM kv WHERE key='send_budget'")[0]["value"]
        self.assertEqual(self.targets(), ["only"])
        self.assertEqual(budget, str(next_budget or 0))
        self.assertEqual(len(events), 1)
        self.assertIn("the budget of 1 is used up: ok 1", events[0]["text"])

    def test_completed_stage_without_operator_race(self):
        self.stage(None)

    def test_rearm_after_receiver_acceptance_preserves_old_stage(self):
        self.stage(7)

    def test_disarm_after_receiver_acceptance_preserves_old_stage(self):
        self.stage(0)


def load_tests(loader, tests, pattern):
    # The fixture has its own complete module; this module adds only the three new race controls.
    return unittest.TestSuite(StageRace(name) for name in (
        "test_completed_stage_without_operator_race",
        "test_rearm_after_receiver_acceptance_preserves_old_stage",
        "test_disarm_after_receiver_acceptance_preserves_old_stage",
    ))
