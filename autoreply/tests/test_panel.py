"""SPEC 14.7, the panel JSON (panel.build) called directly: configured A/B variants with and without rows, people
fields, totals, robots' clicks, and that it never writes."""
import dataclasses
import os
import tempfile
import unittest
from types import SimpleNamespace

import clock
import panel
import store
from tests.test_report import CFG, REEL

FLOW = SimpleNamespace(variants=(SimpleNamespace(name="A", weight=50, opening=True, gate=True, text=None),
                                 SimpleNamespace(name="B", weight=50, opening=False, gate=False, text="{link}")),
                       opening=None, gate=None, link=None, followup=None)


class Build(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.conn = store.connect(os.path.join(folder.name, "core.db"))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        # SimpleNamespace stands for config.Flow: panel.py reads it with getattr only
        self.cfg = dataclasses.replace(CFG, reels=(dataclasses.replace(REEL, flow=FLOW),))
        self.now = clock.now()

    def comment(self, cid, status, variant=None, **flow):
        stamp = clock.iso(self.now)
        with store.tx(self.conn):
            store.add_comment(self.conn, cid, REEL.media_id, "u" + cid, "user" + cid, "арена", stamp, "seen")
            self.conn.execute("UPDATE comments SET status=? WHERE comment_id=?", (status, cid))
            if status in ("sent", "done"):
                self.conn.execute("INSERT INTO attempts (comment_id, kind, detail, started_at, finished_at, outcome) "
                                  "VALUES (?, 'dm', 'fresh', ?, ?, 'ok')", (cid, stamp, stamp))
            if variant:
                self.conn.execute("INSERT INTO flow (comment_id, media_id, variant, opening, gate, state, created_at) "
                                  "VALUES (?, ?, ?, 1, 1, 'opened', ?)", (cid, REEL.media_id, variant, stamp))
                for key, value in flow.items():
                    self.conn.execute(f"UPDATE flow SET {key}=? WHERE comment_id=?", (value, cid))

    def test_configured_variants_without_rows(self):
        data = panel.build(self.conn, self.cfg, self.now)
        variants = data["reels"][0]["variants"]
        self.assertEqual([(v["name"], v["weight"], v["opening"], v["gate"], v["first_dm"]) for v in variants],
                         [("A", 50, True, True, 0), ("B", 50, False, False, 0)])
        self.assertEqual(data["reels"][0]["flow"]["variants"][1]["text"], "{link}")

    def test_funnel_per_variant(self):
        stamp = clock.iso(self.now)
        self.comment("1", "done", "A", pressed_at=stamp, link_at=stamp, state="linked", followup_at=stamp)
        self.comment("2", "sent", "A", pressed_at=stamp, state="stuck", prompts=3)
        self.comment("3", "sent", "B", link_at=stamp, state="direct", unfollowed_at=stamp)
        self.comment("4", "pending")
        self.comment("5", "dup_user")
        with store.tx(self.conn):
            self.conn.execute("INSERT INTO checks (comment_id, at, purpose, result) VALUES "
                              "('1', ?, 'gate', 'no'), ('1', ?, 'gate', 'yes'), ('2', ?, 'gate', 'no')",
                              (stamp, stamp, stamp))
            self.conn.execute("INSERT INTO links VALUES ('t1', '1', ?, 'A', 1, 'https://x', ?), "
                              "('t3', '3', ?, 'B', 1, 'https://x', ?)", (REEL.media_id, stamp, REEL.media_id, stamp))
            self.conn.execute("INSERT INTO clicks (token, at, agent) VALUES ('t1', ?, 'Instagram'), ('t1', ?, NULL), "
                              "('t3', ?, 'meta-externalagent/1.1')", (stamp, stamp, stamp))
        before = self.conn.total_changes
        data = panel.build(self.conn, self.cfg, self.now)
        self.assertEqual(self.conn.total_changes, before)
        self.assertFalse(self.conn.in_transaction)
        a, b = data["reels"][0]["variants"]

        def pick(v):
            return {k: v[k] for k in ("first_dm", "pressed", "checked_yes", "checked_no", "linked", "clicks",
                                      "clicked_people", "followup", "stuck", "unfollowed", "robot_clicks")}
        self.assertEqual(pick(a), {"first_dm": 2, "pressed": 2, "checked_yes": 1, "checked_no": 2, "linked": 1,
                                   "clicks": 2, "clicked_people": 1, "followup": 1, "stuck": 1, "unfollowed": 0,
                                   "robot_clicks": 0})
        self.assertEqual(pick(b), {"first_dm": 1, "pressed": 0, "checked_yes": 0, "checked_no": 0, "linked": 1,
                                   "clicks": 0, "clicked_people": 0, "followup": 0, "stuck": 0, "unfollowed": 1,
                                   "robot_clicks": 1})
        counts = data["reels"][0]["counts"]
        self.assertEqual((counts["requested"], counts["sent"], counts["done"], counts["pending"], counts["dup"]),
                         (4, 3, 1, 1, 1))
        totals = data["totals"]
        self.assertEqual((totals["requested"], totals["sent"], totals["pending"], totals["clicks"], totals["pressed"],
                          totals["linked"]), (4, 3, 1, 2, 2, 2))
        people = {p["comment_id"]: p for p in data["people"]}
        self.assertEqual((people["1"]["variant"], people["1"]["clicks"], people["1"]["dm_at"]), ("A", 2, stamp))
        self.assertIsNone(people["4"]["variant"])

    def test_a_reel_without_flow(self):
        cfg = dataclasses.replace(CFG, reels=(REEL,))
        self.comment("1", "sent")
        data = panel.build(self.conn, cfg, self.now)
        self.assertEqual((data["reels"][0]["variants"], data["reels"][0]["flow"]), ([], None))
        self.assertEqual(data["totals"]["sent"], 1)


if __name__ == "__main__":
    unittest.main()
