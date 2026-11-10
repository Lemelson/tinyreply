"""I04 regressions: validated local input, startup and operator-only acknowledgement."""
import copy
import fcntl
import json
import os
import sqlite3
import subprocess
import sys
import unittest
from unittest import mock

import clock
import config
import store
from tests.test_cli import CODE, EXAMPLE, MEDIA, CliCase, comment
from tests.test_config import valid


class ConfigI04(unittest.TestCase):
    def loaded(self, raw):
        return config.loads(json.dumps(raw, ensure_ascii=True))

    def test_https_rejects_invalid_authority_port_and_raw_controls_by_field(self):
        bad = ['https://#section', 'https://?query', 'https://:', 'https://[not-an-ip]',
               'https://example.test:bad/p', 'https://example.test:65536/p', 'https://user@/p',
               'https://example.test/\x00', 'https://\x01example.test/path', 'https://[::1',
               'https://example.test/\x7f', 'https://example.test/\tpath']
        for field in ('link', 'profile_url', 'permalink'):
            for url in bad:
                with self.subTest(field=field, url=url):
                    raw = valid()
                    target = raw if field == 'profile_url' else raw['reels'][0]
                    target[field] = url
                    with self.assertRaises(config.ConfigError) as caught:
                        self.loaded(raw)
                    expected = field if field == 'profile_url' else 'reels[0].' + field
                    self.assertEqual(len(caught.exception.problems), 1)
                    self.assertTrue(caught.exception.problems[0].startswith(expected + ': '))

    def test_https_accepts_syntax_without_dns_allowlist_or_probe(self):
        good = ['https://example.test', 'https://example.test?q=one&b=two',
                'https://example.test#fragment', 'https://пример.рф/путь?ключ=значение#раздел',
                'https://xn--e1afmkfd.xn--p1ai/p', 'https://[2001:db8::1]/path',
                'https://[::1]:8443/p?q=x#f', 'https://example.test:8443/a%20b?x=%23#f',
                'https://localhost/path', 'https://192.0.2.1/path',
                'https://user:pass@example.test:8443/path', 'https://service_name.internal/path',
                'https://[fe80::1%25en0]/path']
        with mock.patch('socket.getaddrinfo', side_effect=AssertionError('config must be offline')):
            for field in ('link', 'profile_url', 'permalink'):
                for url in good:
                    with self.subTest(field=field, url=url):
                        raw = valid()
                        target = raw if field == 'profile_url' else raw['reels'][0]
                        target[field] = url
                        cfg = self.loaded(raw)
                        self.assertEqual(config.loads(config.dump(cfg)), cfg)
                        config.fingerprint(cfg)
            self.assertEqual(self.loaded(valid()).reels[0].permalink, '')

    def test_raw_keyword_surrogates_are_config_errors(self):
        for field in ('keywords', 'also_match'):
            for surrogate in ('\ud800', '\udfff'):
                for value in (surrogate + 'арена', 'арена' + surrogate, 'аре' + surrogate + 'на'):
                    with self.subTest(field=field, value=repr(value)):
                        raw = valid()
                        raw['reels'][0][field] = [value]
                        with self.assertRaises(config.ConfigError) as caught:
                            self.loaded(raw)
                        self.assertEqual(len(caught.exception.problems), 1)
                        self.assertTrue(caught.exception.problems[0].startswith('reels[0].' + field + ': '))

    def test_healthy_unicode_keywords_roundtrip_and_fingerprint(self):
        for field in ('keywords', 'also_match'):
            for value in ('арена', 'café', 'cafe\u0301', '\U00010400\U00010401', 'арена🔥'):
                with self.subTest(field=field, value=value):
                    raw = valid()
                    raw['reels'][0][field] = [value]
                    cfg = self.loaded(raw)
                    self.assertEqual(config.loads(config.dump(cfg)), cfg)
                    self.assertEqual(len(config.fingerprint(cfg)), 64)

    def test_unknown_surrogate_key_has_safe_utf8_diagnostic(self):
        raw = valid()
        raw['\udfff'] = True
        with self.assertRaises(config.ConfigError) as caught:
            self.loaded(raw)
        error = caught.exception
        self.assertEqual(error.problems, ['\\udfff: unknown key'])
        self.assertEqual(str(error), '\\udfff: unknown key')
        self.assertEqual(str(error).encode('utf-8').decode('utf-8'), str(error))


class CliI04(CliCase):
    def writer(self):
        conn = store.connect(os.path.join(self.dir, 'core.db'))
        store.init(conn)
        self.addCleanup(conn.close)
        return conn

    def test_report_recovers_valid_snapshot_from_native_broken_file_readonly(self):
        self.world([comment('c1')])
        self.assertEqual(self.bot('once')[0], 0)
        self.assertEqual(self.bot('arm', '7')[0], 0)
        before = '\n'.join(self.conn.iterdump())
        for content in ('{', json.dumps({'extra': True}), json.dumps({'\udfff': True}), None):
            with self.subTest(content=content):
                if content is None:
                    os.unlink(os.path.join(self.dir, 'config.json'))
                else:
                    self.write('config.json', content)
                code, out, err = self.bot('report', '--json')
                self.assertEqual(code, 0, err)
                data = json.loads(out)
                self.assertEqual(data['gates']['budget'], 7)
                self.assertTrue(data['gates']['sending'])
                self.assertEqual(data['reels'][0]['people_waiting'], 1)
                self.assertIn('config_file_invalid', dict(data['problems']))
                self.assertEqual('\n'.join(self.conn.iterdump()), before)
                for args in (('check',), ('arm', '5', '--deaf'), ('once',)):
                    self.assertEqual(self.bot(*args)[0], 4)
                self.assertEqual(store.budget(self.conn), 7)

    def test_report_invalid_file_without_valid_snapshot_remains_strict(self):
        self.world()
        conn = self.writer()
        self.write('config.json', '{')
        for snapshot in (None, '{', json.dumps({'extra': True})):
            with self.subTest(snapshot=snapshot):
                store.kv_set(conn, 'config_running', snapshot)
                code, out, err = self.bot('report', '--json')
                self.assertEqual((code, out), (4, ''), err)

    def test_invalid_surrogate_key_check_and_start_keep_exit4_and_fatal_diagnostic(self):
        self.world()
        raw = copy.deepcopy(EXAMPLE)
        raw['\udfff'] = True
        self.write('config.json', json.dumps(raw, ensure_ascii=True))
        for args in (('check',), ('once',), ('send-one', 'c1'), ('run',)):
            with self.subTest(args=args):
                code, _, err = self.bot(*args)
                self.assertEqual(code, 4, err)
                self.assertIn('\\udfff: unknown key', err)
        self.assertIn('fatal_start', self.telegram())
        self.assertIn('\\udfff: unknown key', self.telegram())
        self.assertEqual([r['kind'] for r in self.requests()], ['telegram'])

    def test_invalid_urls_and_keywords_refuse_real_cli_before_claim(self):
        self.world([comment('c1')])
        self.assertEqual(self.bot('once')[0], 0)
        self.assertEqual(self.bot('arm', '1')[0], 0)
        for key, value in (('link', 'https://#bad'), ('keywords', ['арена\udfff']),
                           ('also_match', ['\ud800арена'])):
            raw = copy.deepcopy(EXAMPLE)
            raw['sending'] = True
            raw['reels'][0][key] = value
            self.write('config.json', json.dumps(raw, ensure_ascii=True))
            for args in (('check',), ('once',), ('send-one', 'c1')):
                with self.subTest(key=key, args=args):
                    code, _, err = self.bot(*args)
                    self.assertEqual(code, 4, err)
            self.assertEqual(store.budget(self.conn), 1)
            self.assertEqual(self.status('c1'), ('pending', ''))
            self.assertEqual(self.dm_targets(), [])

    def test_startup_invalid_config_is_said_directly_past_a_large_old_backlog(self):
        self.world()
        conn = self.writer()
        for key in ('digest', 'fatal_start', 'fatal_start'):
            conn.execute('INSERT INTO outbox(key,text,created_at) VALUES(?,?,?)',
                         (key, 'OLD_' + key + ' ' + 'x' * 4000, clock.iso()))
        self.write('config.json', '{')
        code, _, err = self.bot('run')
        self.assertEqual(code, 4, err)
        delivered = self.telegram()
        self.assertIn('the config is invalid', delivered)
        self.assertNotIn('OLD_', delivered)
        rows = conn.execute('SELECT id,sent_at FROM outbox ORDER BY id').fetchall()
        self.assertEqual([row['sent_at'] is not None for row in rows], [False, False, False])   # no new row either

    def test_native_sqlite_failure_during_run_preparation_has_no_ready_and_fatal(self):
        self.startup_failure('authorizer')

    def test_native_corrupt_header_during_run_preparation_has_no_ready_and_fatal(self):
        self.startup_failure('header')

    def startup_failure(self, fault):
        self.world()
        script = '''import json, os, sqlite3, sys
sys.path.insert(0, sys.argv[1])
import bot, engine
original = engine.Engine.__init__
def native_failure(self, *args, **kwargs):
    original(self, *args, **kwargs)
    if sys.argv[2] == 'authorizer':
        def authorizer(action, table, *unused):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table == 'kv' else sqlite3.SQLITE_OK
        self.conn.set_authorizer(authorizer)
    else:
        self.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall()
        path = self.conn.execute('PRAGMA database_list').fetchone()[2]
        with open(path, 'r+b', buffering=0) as file:
            file.write(b'X' * 100)
            os.fsync(file.fileno())
        self.conn.execute('PRAGMA shrink_memory')
engine.Engine.__init__ = native_failure
states = []
bot.sd_notify = states.append
bot.stop_flag = lambda: lambda: True
code = bot.main(['run'])
print(json.dumps({'code': code, 'states': states}))
sys.exit(code)
'''
        done = subprocess.run([sys.executable, '-B', '-c', script, CODE, fault], env=self.env,
                              capture_output=True, text=True, timeout=20)
        data = json.loads(done.stdout)
        self.assertEqual(done.returncode, 6, done.stderr)
        self.assertNotIn('READY=1', data['states'])
        self.assertIn('fatal_start', self.telegram())
        self.assertEqual([r['kind'] for r in self.requests()], ['telegram'])

    def test_failed_fatal_queue_uses_direct_startup_alert(self):
        self.world()
        conn = self.writer()
        conn.execute("CREATE TRIGGER reject_alert BEFORE INSERT ON outbox BEGIN "
                     "SELECT RAISE(ABORT,'native alert write failure'); END")
        self.write('config.json', '{')
        code, _, err = self.bot('run')
        self.assertEqual(code, 4, err)
        self.assertIn('fatal_start', self.telegram())
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 0)

    def test_ack_uncertain_is_idempotent_preserves_every_delivery_row_and_budget(self):
        self.world()
        conn = self.writer()
        now = clock.iso()
        for comment_id, status in (('c1', 'uncertain'), ('c2', 'uncertain'), ('pending', 'pending'), ('done', 'done')):
            conn.execute('INSERT INTO comments(comment_id,media_id,user_id,ts,status,first_seen_at,updated_at) '
                         'VALUES(?,?,?,?,?,?,?)', (comment_id, MEDIA, 'u_' + comment_id, now, status, now, now))
        conn.execute('INSERT INTO attempts(comment_id,kind,started_at,finished_at,outcome,reason) '
                     'VALUES(?,?,?,?,?,?)', ('c1', 'dm', now, now, 'uncertain', 'http_5xx'))
        store.arm(conn, 9)
        before = {table: [tuple(row) for row in conn.execute('SELECT * FROM ' + table)]
                  for table in ('comments', 'attempts', 'outbox')}
        before_kv = dict(conn.execute('SELECT key,value FROM kv'))
        with open(os.path.join(self.dir, 'core.lock'), 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, out, err = self.bot('ack-uncertain', 'c1')
        self.assertEqual(code, 0, err)
        self.assertIn('c1', out)
        acknowledged_at = store.kv_get(conn, 'uncertain_ack:c1')
        self.assertIsNotNone(acknowledged_at)
        self.assertEqual(self.bot('ack-uncertain', 'c1')[0], 0)
        self.assertEqual(store.kv_get(conn, 'uncertain_ack:c1'), acknowledged_at)
        for comment_id in ('pending', 'done', 'missing'):
            self.assertEqual(self.bot('ack-uncertain', comment_id)[0], 1)
            self.assertIsNone(store.kv_get(conn, 'uncertain_ack:' + comment_id))
        self.assertIsNone(store.kv_get(conn, 'uncertain_ack:c2'))
        self.assertEqual(store.budget(conn), 9)
        self.assertEqual({table: [tuple(row) for row in conn.execute('SELECT * FROM ' + table)]
                          for table in before}, before)
        self.assertEqual(dict(conn.execute('SELECT key,value FROM kv')),
                         {**before_kv, 'uncertain_ack:c1': acknowledged_at})
        self.assertEqual(self.requests(), [])

    def test_native_ack_write_failure_leaves_manual_case_and_delivery_state_unchanged(self):
        self.world()
        conn = self.writer()
        now = clock.iso()
        conn.execute('INSERT INTO comments(comment_id,media_id,user_id,ts,status,first_seen_at,updated_at) '
                     'VALUES(?,?,?,?,?,?,?)', ('c1', MEDIA, 'u_c1', now, 'uncertain', now, now))
        store.arm(conn, 5)
        conn.execute("CREATE TRIGGER reject_ack BEFORE INSERT ON kv "
                     "WHEN NEW.key LIKE 'uncertain_ack:%' BEGIN SELECT RAISE(ABORT,'native ack write failure'); END")
        before = '\n'.join(conn.iterdump())
        code, _, err = self.bot('ack-uncertain', 'c1')
        self.assertEqual(code, 1, err)
        self.assertIn('native ack write failure', err)
        self.assertEqual('\n'.join(conn.iterdump()), before)
        self.assertIsNone(store.kv_get(conn, 'uncertain_ack:c1'))
        conn.execute('DROP TRIGGER reject_ack')
        self.assertEqual(self.bot('ack-uncertain', 'c1')[0], 0)
        self.assertEqual(store.comment(conn, 'c1')['status'], 'uncertain')
        self.assertEqual(store.budget(conn), 5)
        self.assertEqual(self.requests(), [])


if __name__ == '__main__':
    unittest.main()
