"""Report audit regressions: real SQLite, local notifier outbox, bounded Telegram text."""
import contextlib
import dataclasses
import io
import unittest

import clock
import config
import notify
import report
import store
from tests.test_report import Db, REEL, SECOND, WINDOW


class UnservableConfiguredReels(Db):
    def test_inactive_requests_remain_announced_and_repeat(self):
        for reason in ("no_author", "malformed"):
            with self.subTest(reason=reason):
                self.add(reason, status="seen", reason=reason)
        self.configure(reels=(REEL, SECOND))
        self.read("222")
        notifier = notify.Notifier(self.conn, lambda: ("", ""), "http://127.0.0.1:1", repeat_min=1)
        with contextlib.redirect_stderr(io.StringIO()):
            notifier.sync(report.problems(self.conn, self.cfg, self.now), self.now)
            self.configure(reels=(dataclasses.replace(REEL, active=False), SECOND))
            self.tick(61)
            notifier.sync(report.problems(self.conn, self.cfg, self.now), self.now)
        self.assertIn("unservable", self.problems())
        self.assertIn("2 просьб", self.problems()["unservable"])
        queued = [row[0] for row in self.conn.execute("SELECT text FROM outbox")]
        self.assertEqual(sum(text.startswith("⚠ unservable") for text in queued), 2)
        self.assertFalse(any("cleared: unservable" in text for text in queued))
        self.assertEqual(self.build()["verdict"], "PROBLEM: unservable")

    def test_inactive_uses_current_matcher_and_exact_window(self):
        self.configure(reels=(dataclasses.replace(REEL, active=False), SECOND))
        self.read("222")
        self.add("request", status="seen", reason="no_author", age=WINDOW - 1)
        self.add("chatter", status="seen", reason="malformed", text="привет всем")
        self.assertIn("1 просьб", self.problems().get("unservable", ""))
        self.tick(1)
        self.assertNotIn("unservable", self.problems())
        self.add("new", status="seen", reason="malformed")
        self.configure(reels=(dataclasses.replace(REEL, active=False, keywords=("книга",)), SECOND))
        self.assertNotIn("unservable", self.problems())


class UncertainManual(Db):
    def test_repeat_survives_hold_end_and_restart_until_explicit_ack(self):
        self.add("u1", status="uncertain", reason="interrupted")
        self.attempt("u1", "uncertain", reason="interrupted")
        store.set_send_hold(self.conn, "uncertain", 900)
        notifier = notify.Notifier(self.conn, lambda: ("", ""), "http://127.0.0.1:1", repeat_min=1)
        with contextlib.redirect_stderr(io.StringIO()):
            notifier.sync(report.problems(self.conn, self.cfg, self.now), self.now)
            self.tick(901)
            self.ran()
            self.read()
            reopened = store.connect(self.path)
            self.addCleanup(reopened.close)
            resumed = notify.Notifier(reopened, lambda: ("", ""), "http://127.0.0.1:1", repeat_min=1)
            resumed.sync(report.problems(reopened, self.cfg, self.now), self.now)
        self.assertIn("uncertain_manual", self.problems())
        data = self.build()
        self.assertEqual(data["uncertain_unresolved"], 1)
        self.assertEqual(data["verdict"], "PROBLEM: uncertain_manual")
        text = report.render(data)
        self.assertIn("проверьте доставку", text)
        self.assertIn("igbot ack-uncertain u1", text)
        self.assertNotIn("напишите сами", text)
        alerts = [row[0] for row in self.conn.execute("SELECT text FROM outbox")]
        self.assertEqual(sum(row.startswith("⚠ uncertain_manual:") for row in alerts), 2)
        before = list(self.conn.execute("SELECT * FROM comments")), list(self.conn.execute("SELECT * FROM attempts")), store.budget(self.conn)
        store.kv_set(self.conn, "uncertain_ack:u1", clock.iso())
        with contextlib.redirect_stderr(io.StringIO()):
            resumed.sync(report.problems(reopened, self.cfg, self.now), self.now)
        self.assertNotIn("uncertain_manual", self.problems())
        self.assertEqual(self.build()["uncertain_unresolved"], 0)
        self.assertEqual(self.build()["uncertain"], [])
        self.assertEqual(before, (list(self.conn.execute("SELECT * FROM comments")), list(self.conn.execute("SELECT * FROM attempts")), store.budget(self.conn)))
        self.assertIn("✓ cleared: uncertain_manual", [row[0] for row in self.conn.execute("SELECT text FROM outbox")])
        self.add("same-person", user="u-u1")
        self.assertEqual(store.claim_dm(self.conn, "same-person", self.cfg, {"user_id": "9000"}).kind, "skip")
        self.assertEqual(self.conn.execute("SELECT status FROM comments WHERE comment_id='same-person'").fetchone()[0], "dup_user")

    def test_all_unresolved_count_and_newest_thirty_without_acknowledged(self):
        for n in range(36):
            self.add(f"u{n:02}", status="uncertain", age=1000 - n, media="999")
        store.kv_set(self.conn, "uncertain_ack:u35", clock.iso())
        store.kv_set(self.conn, "uncertain_ack:not-this-case", clock.iso())
        data = self.build()
        self.assertEqual(data.get("uncertain_unresolved"), 35)
        self.assertEqual([row["comment_id"] for row in data["uncertain"]], [f"u{n:02}" for n in range(34, 4, -1)])
        self.assertIn("35", dict(data["problems"])["uncertain_manual"])
        self.assertIn("показаны последние 30", report.render(data))
        self.assertIn("uncertain_manual", report.digest(data))


class RecoveryReports(Db):
    def test_invalid_file_problem_uses_running_snapshot_and_is_readonly(self):
        store.kv_set_many(self.conn, {"config_running": config.dump(self.cfg), "config_hash": config.fingerprint(self.cfg), "token": "IGAA-report-test"})
        expected = self.build()
        before = list(self.conn.iterdump())
        data = report.build(self.conn, self.cfg, self.now, config_file_error="invalid IGAA-report-test value")
        self.assertEqual(data["gates"], expected["gates"])
        self.assertEqual(data["reels"], expected["reels"])
        self.assertEqual(data["verdict"], "PROBLEM: config_file_invalid")
        self.assertIn("invalid <token> value", dict(data["problems"])["config_file_invalid"])
        self.assertEqual(list(self.conn.iterdump()), before)
        self.assertNotIn("config_file_invalid", self.problems())

    def test_unreadable_comments_are_a_problem_of_the_reel_until_a_clean_full_sweep(self):
        self.read()
        store.kv_set(self.conn, "unreadable:111", 2)
        self.assertIn("в непонятном виде", self.problems().get("reel:111", ""))
        self.assertIn("остальные обрабатываются как обычно", self.problems()["reel:111"])
        store.kv_set(self.conn, "unreadable:111", None)
        self.assertNotIn("reel:111", self.problems())


class BoundedDigest(Db):
    def typos(self):
        for n, letter in enumerate("абвгдежзиклмнопрстуфх"):
            self.add(f"typo-{n}", status="seen", text=f"слово{letter}")
        self.assertEqual(len(self.build()["unmatched"]), 20)

    def check_digest(self, data):
        digest = report.digest(data)
        self.assertLessEqual(notify._units("ℹ digest: " + digest), 4096)
        for text, n in data["unmatched"]:
            self.assertIn(f"  {n} × {text[:40]}", digest)
        self.assertIn("За 24 ч: попыток", digest)
        self.assertIn("(near):", digest)
        self.assertIn("Подписчики:", digest)
        (ids, queued) = notify._batch([(123, "ℹ digest: " + digest)])
        self.assertEqual((ids, queued), ([123], "ℹ digest: " + digest))
        return digest

    def test_normal_short_summary_is_unchanged(self):
        self.assertEqual(report.digest(self.build()), "\n".join([
            "OK: никто не ждёт", "«arena»: ждут 0 чел.", "  комментариев нет",
            "За 24 ч: попыток 0, доставлено 0", "Коротких комментариев без ключевого слова нет.",
            "С ключевым словом, но не просьба (near): 0",
            "Подписчики: сейчас нет данных, сутки назад нет данных, неделю назад нет данных"]))

    def test_long_and_astral_labels_cannot_displace_twenty_typos(self):
        self.typos()
        for label in ("long-" + "я" * 5000, "emoji-" + "😀" * 2100):
            with self.subTest(label=label[:6]):
                self.configure(reels=(dataclasses.replace(REEL, name=label),))
                digest = self.check_digest(self.build())
                self.assertIn("полный отчёт: igbot report", digest)
                self.assertIn("ждут 0 чел.", digest)
                self.assertTrue(digest.startswith("OK: никто не ждёт\n"))

    def test_many_reels_keep_mandatory_blocks_and_mark_omissions(self):
        self.typos()
        self.configure(reels=tuple(dataclasses.replace(REEL, name=f"label-{n}-" + "я" * 60, media_id=str(111 + n)) for n in range(100)))
        digest = self.check_digest(self.build())
        self.assertIn("опущено строк:", digest)
        self.assertIn("полный отчёт: igbot report", digest)

    def test_twenty_thousand_problems_and_astral_typos_have_truthful_summary(self):
        data = self.build()
        data["problems"] = [[f"reel:{n}:" + "😀" * 50, "ошибка " * 300] for n in range(20000)]
        data["verdict"] = "PROBLEM: " + ", ".join(key for key, _ in data["problems"])
        data["unmatched"] = [["𐐀" * 39 + letter, 9223372036854775807] for letter in "абвгдежзиклмнопрстуфх"[:20]]
        digest = self.check_digest(data)
        self.assertTrue(digest.startswith("PROBLEM:"))
        self.assertIn("Проблем: 20000", digest)
        self.assertIn("reel:0:", digest)
        self.assertIn("полный отчёт: igbot report", digest)
        self.assertIn("сокращены", digest)


class CompletionAnchoredCap(Db):
    def test_slow_completed_response_occupies_cap_until_its_end_leaves_window(self):
        self.configure(max_sends_per_hour=2)
        self.add("waiting")
        horizon = clock.T["rate_window"] + clock.T["cap_margin"]
        self.attempt("slow", "rejected", ago=horizon + 1)
        self.conn.execute("UPDATE attempts SET finished_at=? WHERE comment_id='slow'", (self.ago(horizon - 1),))
        self.attempt("recent", "rejected", ago=31 * 60)
        self.assertEqual(self.build()["reels"][0]["waiting_reason"], "cap")
        self.assertNotIn("waiting", self.problems())
        self.tick(1)
        self.assertEqual(self.build()["reels"][0]["waiting_reason"], "stalled")
        self.assertIn("waiting", self.problems())


if __name__ == "__main__":
    unittest.main()
