"""Factual claim timing and whole diagnostic lines for a shared subprocess output file."""
import dataclasses
import io
import os
import tempfile
import time
import unittest
from datetime import timedelta
from unittest import mock

import clock
import engine
import meta
import store
from tests.test_store import CFG, ME, DM_OK


class Interleaved(io.StringIO):
    """Model another process writing to the shared output between partial print writes."""
    def write(self, text):
        written = super().write(text)
        if not text.endswith("\n") and ("pid=" in text or "call=" in text):
            super().write("STATE_DIR=/synthetic/other-process\n")
        return written


class WholeDiagnostics(unittest.TestCase):
    def test_engine_diagnostic_cannot_be_split_by_another_process(self):
        output = Interleaved()
        with mock.patch.object(engine.sys, "stderr", output):
            engine.log("INFO", "claim_committed", pid=7, monotonic=10.5, attempt=1, kind="dm")
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].endswith("pid=7 monotonic=10.5 attempt=1 kind=dm"), lines)

    def test_http_completion_cannot_be_split_by_another_process(self):
        output = Interleaved()
        with mock.patch.object(meta.sys, "stderr", output):
            meta._http_complete(7, "GET", "read_me", "http://127.0.0.1/v/me?access_token=synthetic", 200)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].endswith("call=7 method=GET kind=read_me route=/v/me http=200"), lines)
        self.assertNotIn("access_token", lines[0])


class NativeClaimPoint(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.conn = store.connect(os.path.join(folder, "core.db"))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.cfg = dataclasses.replace(CFG, sending=True, public_reply=True)
        store.arm(self.conn, 1)
        store.add_comment(self.conn, "c", "111", "person", "person", "арена",
                          clock.iso(clock.now() - timedelta(seconds=60)), "pending")
        self.sql_times = []
        self.conn.set_trace_callback(lambda sql: self.sql_times.append((sql, time.monotonic())))

    def assert_point(self, claim, before, after, kind="dm"):
        self.assertEqual(claim.kind, "claimed")
        point = getattr(claim, "monotonic", None)
        self.assertIsInstance(point, float)
        self.assertLessEqual(before, point)
        self.assertLessEqual(point, after)
        self.assertFalse(self.conn.in_transaction)
        row = self.conn.execute("SELECT id, started_at FROM attempts WHERE id=?", (claim.attempt_id,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(getattr(claim, "started_at", None), row["started_at"])
        gate = "SELECT COUNT(*) FROM attempts" if kind == "dm" else "SELECT value FROM kv WHERE key='last_phrase'"
        last_gate = [at for sql, at in self.sql_times if sql.startswith(gate)][-1]
        insert = [at for sql, at in self.sql_times if sql.startswith("INSERT INTO attempts")][-1]
        self.assertLessEqual(last_gate, point)
        self.assertLessEqual(point, insert)

    def test_dm_claim_returns_real_monotonic_point_of_committed_attempt(self):
        before = time.monotonic()
        claim = store.claim_dm(self.conn, "c", self.cfg, ME)
        self.assert_point(claim, before, time.monotonic())
        self.assertEqual(store.budget(self.conn), 0)

    def test_public_claim_has_its_own_point_and_needs_no_remaining_budget(self):
        dm = store.claim_dm(self.conn, "c", self.cfg, ME)
        store.record_dm(self.conn, dm.attempt_id, DM_OK)
        before = time.monotonic()
        claim = store.claim_public(self.conn, "c", self.cfg, ME)
        self.assert_point(claim, before, time.monotonic(), kind="public")
        self.assertEqual(store.budget(self.conn), 0)
