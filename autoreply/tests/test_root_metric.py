"""Malformed optional API counts must not interrupt an otherwise successful cycle."""
import store
from tests import test_regress_r1 as fixture


class MetricMetadata(fixture.MetaCase):
    def check_counts(self, followers, media_count, expected):
        self.boot([fixture.comment("c")], scenario={
            "account": {**fixture.ME, "followers_count": followers, "media_count": media_count}})
        self.assertEqual(self.cycle(), 1)
        self.assertEqual([r["target"] for r in self.requests("dm")], ["c"])
        self.assertEqual(store.comment(self.conn, "c")["status"], "done")
        self.assertIsNotNone(store.kv_get(self.conn, "last_cycle_ok_at"))
        self.assertIsNotNone(store.kv_get(self.conn, "metric_at"))
        self.assertEqual(tuple(self.conn.execute(
            "SELECT followers,media_count FROM metrics").fetchone()), expected)
        self.assertIsNone(store.get_account_hold(self.conn))

    def test_valid_optional_counts_are_preserved(self):
        for value in (0, 1234, 2**63 - 1, None):
            with self.subTest(value=value):
                self.check_counts(value, 7, (value, 7))

    def test_bad_optional_counts_do_not_break_cycle_after_successful_delivery(self):
        for field in ("followers_count", "media_count"):
            for value in (10**30, -(10**30), -1, True, False, "12", 1.5, {}, []):
                with self.subTest(field=field, value=value):
                    counts = {"followers_count": 1234, "media_count": 7, field: value}
                    expected = (None, 7) if field == "followers_count" else (1234, None)
                    self.check_counts(counts["followers_count"], counts["media_count"], expected)
