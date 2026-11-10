"""Read-only check preflight chooses the token the next Engine cycle would use."""
import os
import unittest

import clock
import store
from tests.test_cli import CliCase, TOKEN


class CheckPreflight(CliCase):
    def writer(self, token, seen):
        conn = store.connect(os.path.join(self.dir, "core.db"))
        store.init(conn)
        store.kv_set_many(conn, {"token": token, "token_env_seen": seen, "token_refreshed_at": clock.iso()})
        self.addCleanup(conn.close)
        return conn

    def test_new_env_token_is_probed_without_adoption_then_once_adopts_it(self):
        self.world()
        conn = self.writer("old-invalid-token", "old-env-token")
        before = list(conn.iterdump())
        code, out, err = self.bot("check", "--online")
        self.assertEqual(code, 0, out + err)
        self.assertEqual([r["kind"] for r in self.requests()], ["me", "telegram"])
        self.assertTrue(self.requests("me")[0]["token_ok"])
        self.assertEqual(list(conn.iterdump()), before)
        self.assertEqual(store.kv_get(conn, "token"), "old-invalid-token")
        self.assertEqual(store.kv_get(conn, "token_env_seen"), "old-env-token")
        code, _, err = self.bot("once")
        self.assertEqual(code, 0, err)
        self.assertEqual(store.kv_get(conn, "token"), TOKEN)
        self.assertEqual(store.kv_get(conn, "token_env_seen"), TOKEN)
        self.assertIsNone(store.get_account_hold(conn))

    def test_unchanged_seen_env_keeps_the_valid_refreshed_stored_token(self):
        self.world()
        self.write(".env", "IG_ACCESS_TOKEN=old-env-token\nTG_BOT_TOKEN=123:abc\nTG_CHAT_ID=42\n")
        conn = self.writer(TOKEN, "old-env-token")
        before = list(conn.iterdump())
        code, out, err = self.bot("check", "--online")
        self.assertEqual(code, 0, out + err)
        self.assertTrue(self.requests("me")[0]["token_ok"])
        self.assertEqual(list(conn.iterdump()), before)
        code, _, err = self.bot("once")
        self.assertEqual(code, 0, err)
        self.assertEqual(store.kv_get(conn, "token"), TOKEN)
        self.assertEqual(store.kv_get(conn, "token_env_seen"), "old-env-token")
        self.assertIsNone(store.get_account_hold(conn))

    def test_no_database_env_fallback_does_not_create_database_before_once(self):
        self.world()
        db_path = os.path.join(self.dir, "core.db")
        code, out, err = self.bot("check", "--online")
        self.assertEqual(code, 0, out + err)
        self.assertTrue(self.requests("me")[0]["token_ok"])
        self.assertFalse(os.path.exists(db_path))
        code, _, err = self.bot("once")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.kv("token"), TOKEN)
        self.assertEqual(self.kv("token_env_seen"), TOKEN)

    def test_empty_env_keeps_valid_stored_token_without_writing_check_state(self):
        self.world(env_file=False)
        self.write(".env", "TG_BOT_TOKEN=123:abc\nTG_CHAT_ID=42\n")
        conn = self.writer(TOKEN, "old-env-token")
        before = list(conn.iterdump())
        code, out, err = self.bot("check", "--online")
        self.assertEqual(code, 0, out + err)
        self.assertTrue(self.requests("me")[0]["token_ok"])
        self.assertEqual(list(conn.iterdump()), before)
        code, _, err = self.bot("once")
        self.assertEqual(code, 0, err)
        self.assertEqual(store.kv_get(conn, "token"), TOKEN)
        self.assertEqual(store.kv_get(conn, "token_env_seen"), "old-env-token")
        self.assertIsNone(store.get_account_hold(conn))

    def test_no_token_exits_five_without_database_or_api_then_once_holds(self):
        self.world(env_file=False)
        self.write(".env", "TG_BOT_TOKEN=123:abc\nTG_CHAT_ID=42\n")
        code, out, err = self.bot("check", "--online")
        self.assertEqual(code, 5, out + err)
        self.assertEqual(self.requests(), [])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "core.db")))
        code, _, err = self.bot("once")
        self.assertEqual(code, 0, err)
        self.assertIsNone(self.kv("token"))
        self.assertEqual(self.requests("me"), [])
        self.assertEqual(store.get_account_hold(self.conn)["reason"], "no_token")

    def test_media_still_tests_stored_token_when_new_env_candidate_is_valid(self):
        self.world()
        conn = self.writer("old-invalid-token", "old-env-token")
        before = list(conn.iterdump())
        code, out, err = self.bot("media")
        self.assertEqual(code, 1, out + err)
        self.assertIn("global auth", err)
        self.assertFalse(self.requests("my_media")[0]["token_ok"])
        self.assertEqual(list(conn.iterdump()), before)
        code, out, err = self.bot("check", "--online")
        self.assertEqual(code, 0, out + err)
        self.assertTrue(self.requests("me")[0]["token_ok"])
        self.assertEqual(list(conn.iterdump()), before)


if __name__ == "__main__":
    unittest.main()
