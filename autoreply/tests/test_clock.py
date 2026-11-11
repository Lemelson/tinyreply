"""SPEC section 3: timestamps, the scaled test clock, sleep slices, the durations table, the safe-env check."""
import os
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import clock

CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UTC = timezone.utc
ANCHOR = 1_800_000_000.0
LOCAL_IG = "http://127.0.0.1:8080"
LOCAL_TG = "http://127.0.0.1:8081"
SCALED = {"CLOCK_SCALE": "60", "CLOCK_ANCHOR": "1800000000", "IG_API_BASE": LOCAL_IG, "TG_API_BASE": LOCAL_TG}

MIN, HOUR, DAY = 60, 3600, 86400


class FakeTime:
    """Real time that only moves when somebody sleeps."""

    def __init__(self):
        self.t = 1000.0
        self.slices = []

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.slices.append(seconds)
        self.t += seconds


class Timestamps(unittest.TestCase):
    def test_iso_format(self):
        for dt, text in [
            (datetime(2026, 10, 3, 12, 34, 56, 789000, tzinfo=UTC), "2026-10-03T12:34:56.789Z"),
            (datetime(2026, 10, 3, 12, 34, 56, 789999, tzinfo=UTC), "2026-10-03T12:34:56.789Z"),
            (datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC), "2026-01-02T03:04:05.000Z"),
            (datetime(2026, 1, 2, 3, 4, 5, 7000, tzinfo=UTC), "2026-01-02T03:04:05.007Z"),
            (datetime(2026, 10, 3, 15, 34, 56, tzinfo=timezone(timedelta(hours=3))), "2026-10-03T12:34:56.000Z"),
        ]:
            with self.subTest(dt=dt):
                self.assertEqual(clock.iso(dt), text)

    def test_iso_without_argument_is_now(self):
        with mock.patch.multiple(clock, SCALE=1.0, ANCHOR=0.0), \
                mock.patch.object(clock.time, "time", return_value=ANCHOR + 0.25):
            self.assertEqual(clock.iso(), "2027-01-15T08:00:00.250Z")

    def test_iso_is_fixed_width_so_text_order_is_time_order(self):
        self.assertRegex(clock.iso(), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        moments = [datetime(2026, 10, 3, 9, 59, 59, 999000, tzinfo=UTC) + timedelta(milliseconds=step)
                   for step in (0, 1, 2, 1001, 60_001, 3_600_001, 86_400_001)]
        texts = [clock.iso(moment) for moment in moments]
        self.assertEqual(sorted(texts), texts)
        self.assertEqual(len(set(texts)), len(texts))

    def test_iso_refuses_a_naive_datetime(self):
        with self.assertRaises(ValueError):
            clock.iso(datetime(2026, 10, 3, 12, 0, 0))

    def test_parse(self):
        expected = datetime(2026, 10, 3, 12, 34, 56, tzinfo=UTC)
        for text, dt in [
            ("2026-10-03T12:34:56.000Z", expected),
            ("2026-10-03T12:34:56.789Z", expected + timedelta(milliseconds=789)),
            ("2026-10-03T12:34:56+0000", expected),          # Meta's comment timestamp
            ("2026-10-03T12:34:56+00:00", expected),         # v1 database
            ("2026-10-03T15:34:56+03:00", expected),
        ]:
            with self.subTest(text=text):
                parsed = clock.parse(text)
                self.assertEqual(parsed, dt)
                self.assertEqual(parsed.utcoffset(), timedelta(0))

    def test_parse_refuses_everything_else_with_valueerror(self):
        for text in ["2026-10-03T12:34:56", "2026-10-03", "", "x", "1800000000", None, 1800000000, b"2026"]:
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    clock.parse(text)

    def test_round_trip_is_exact(self):
        moment = clock.now()
        self.assertEqual(moment.microsecond % 1000, 0)
        self.assertEqual(clock.parse(clock.iso(moment)), moment)
        self.assertEqual(clock.iso(clock.parse("2026-10-03T12:34:56.789Z")), "2026-10-03T12:34:56.789Z")


class ScaledClock(unittest.TestCase):
    def now_at(self, real, scale, anchor):
        with mock.patch.multiple(clock, SCALE=scale, ANCHOR=anchor), \
                mock.patch.object(clock.time, "time", return_value=real):
            return clock.now()

    def test_real_clock(self):
        self.assertEqual(self.now_at(ANCHOR + 0.1239, 1.0, 0.0),
                         datetime(2027, 1, 15, 8, 0, 0, 123000, tzinfo=UTC))

    def test_now_is_utc_and_close_to_the_wall_clock_by_default(self):
        self.assertEqual(clock.SCALE, 1.0, "the test suite itself must run on real time")
        self.assertEqual(clock.now().utcoffset(), timedelta(0))
        self.assertLess(abs((clock.now() - datetime.now(UTC)).total_seconds()), 1)

    def test_scaled_clock_runs_from_the_anchor(self):
        start = datetime(2027, 1, 15, 8, 0, 0, tzinfo=UTC)
        for real_seconds, scale, bot_seconds in [(0, 60.0, 0), (10, 60.0, 600), (1.5, 600.0, 900), (90, 0.5, 45)]:
            with self.subTest(real_seconds=real_seconds, scale=scale):
                self.assertEqual(self.now_at(ANCHOR + real_seconds, scale, ANCHOR),
                                 start + timedelta(seconds=bot_seconds))

    def test_monotonic_is_scaled_like_now(self):
        with mock.patch.object(clock.time, "monotonic", return_value=100.0):
            with mock.patch.object(clock, "SCALE", 1.0):
                self.assertEqual(clock.monotonic(), 100.0)
            with mock.patch.object(clock, "SCALE", 60.0):
                self.assertEqual(clock.monotonic(), 6000.0)


class Sleep(unittest.TestCase):
    def sleep(self, seconds, scale=1.0, **kwargs):
        fake = FakeTime()
        with mock.patch.object(clock, "SCALE", scale), \
                mock.patch.object(clock.time, "monotonic", fake.monotonic), \
                mock.patch.object(clock.time, "sleep", fake.sleep):
            clock.sleep(seconds, **kwargs)
        return fake.slices

    def test_slices_of_at_most_one_real_second(self):
        self.assertEqual(self.sleep(3.5), [1, 1, 1, 0.5])
        self.assertEqual(self.sleep(0.25), [0.25])
        self.assertEqual(self.sleep(1), [1])

    def test_scale_divides_the_real_sleep(self):
        self.assertEqual(self.sleep(90, scale=60.0), [1, 0.5])
        self.assertEqual(self.sleep(15, scale=60.0), [0.25])
        self.assertEqual(self.sleep(2, scale=0.5), [1, 1, 1, 1])

    def test_nothing_to_sleep(self):
        pings = []
        self.assertEqual(self.sleep(0, ping=lambda: pings.append(1)), [])
        self.assertEqual(self.sleep(-5, ping=lambda: pings.append(1)), [])
        self.assertEqual(pings, [])

    def test_ping_in_every_slice(self):
        pings = []
        slices = self.sleep(3.5, ping=lambda: pings.append(1))
        self.assertEqual(len(pings), len(slices))
        self.assertEqual(len(pings), 4)

    def test_stop_before_the_first_slice(self):
        pings = []
        self.assertEqual(self.sleep(90, should_stop=lambda: True, ping=lambda: pings.append(1)), [])
        self.assertEqual(pings, [])

    def test_stop_is_checked_between_slices(self):
        checks = []

        def stop_after_two_slices():
            checks.append(1)
            return len(checks) > 2

        self.assertEqual(self.sleep(90, should_stop=stop_after_two_slices), [1, 1])

    def test_a_stop_check_that_stays_false_does_not_shorten_the_sleep(self):
        self.assertEqual(sum(self.sleep(5, should_stop=lambda: False)), 5)


class SafeEnv(unittest.TestCase):
    def check(self, env):
        with mock.patch.dict(os.environ, env, clear=True):
            return clock.check_safe_env()

    def test_safe(self):
        for env in [
            {},
            {"CLOCK_SCALE": ""},
            {"CLOCK_SCALE": "1"},
            {"CLOCK_SCALE": "1.0", "IG_API_BASE": "https://graph.instagram.com"},
            {"IG_API_BASE": "https://graph.instagram.com", "TG_API_BASE": "https://api.telegram.org"},
            SCALED,
            {**SCALED, "CLOCK_SCALE": "0.5"},
            {**SCALED, "IG_API_BASE": "http://127.0.0.1:1", "TG_API_BASE": "http://127.0.0.1:65535"},
        ]:
            with self.subTest(env=env):
                self.assertIsNone(self.check(env))

    def test_scaled_clock_needs_both_bases_on_loopback(self):
        for name in ("IG_API_BASE", "TG_API_BASE"):
            for base in [
                None,
                "",
                "https://graph.instagram.com",
                "https://api.telegram.org",
                "http://localhost:8080",
                "https://127.0.0.1:8080",
                "http://127.0.0.1",
                "http://127.0.0.1:",
                "http://127.0.0.1:0",
                "http://127.0.0.1:99999",
                "http://127.0.0.1:8080/",
                "http://127.0.0.1:8080/v26.0",
                "http://127.0.0.1:8080@graph.instagram.com",
                "http://127.0.0.1:8080.example.com",
                "http://127.0.0.1.example.com:8080",
                "http://127.0.0.2:8080",
                "http://0.0.0.0:8080",
                "http://[::1]:8080",
                " http://127.0.0.1:8080",
                "http://127.0.0.1:8080\n",
                "HTTP://127.0.0.1:8080",
            ]:
                env = dict(SCALED)
                if base is None:
                    del env[name]
                else:
                    env[name] = base
                with self.subTest(name=name, base=base):
                    self.assertIn(name, self.check(env))

    def test_any_scale_other_than_one_counts(self):
        real = {"CLOCK_ANCHOR": "1800000000", "IG_API_BASE": "https://graph.instagram.com",
                "TG_API_BASE": "https://api.telegram.org"}
        for scale in ["60", "0.999", "1.0001", "2", "1e3"]:
            with self.subTest(scale=scale):
                self.assertIn("IG_API_BASE", self.check({**real, "CLOCK_SCALE": scale}))

    def test_a_scale_that_is_not_a_positive_number_is_refused(self):
        for scale in ["abc", "0", "-1", "nan", "inf", "60x", "1,5"]:
            with self.subTest(scale=scale):
                self.assertIn("CLOCK_SCALE", self.check({**SCALED, "CLOCK_SCALE": scale}))

    def test_scaled_clock_needs_an_anchor(self):
        for anchor in [None, "", "abc", "inf"]:
            env = {key: value for key, value in SCALED.items() if key != "CLOCK_ANCHOR"}
            if anchor is not None:
                env["CLOCK_ANCHOR"] = anchor
            with self.subTest(anchor=anchor):
                self.assertIn("CLOCK_ANCHOR", self.check(env))

    def in_a_fresh_process(self, env):
        code = "import clock; print(clock.SCALE, clock.ANCHOR, clock.check_safe_env() is None)"
        done = subprocess.run([sys.executable, "-B", "-c", code], cwd=CODE, env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def test_the_environment_scales_the_clock_of_a_new_process(self):
        self.assertEqual(self.in_a_fresh_process(SCALED), "60.0 1800000000.0 True")

    def test_an_unsafe_environment_leaves_the_clock_real(self):
        # even a caller that forgets check_safe_env() cannot run scaled time against a real service
        unsafe = {**SCALED, "IG_API_BASE": "https://graph.instagram.com"}
        self.assertEqual(self.in_a_fresh_process(unsafe), "1.0 0.0 False")
        self.assertEqual(self.in_a_fresh_process({**SCALED, "CLOCK_SCALE": "abc"}), "1.0 0.0 False")


class Durations(unittest.TestCase):
    def test_every_duration_of_the_spec(self):
        self.assertEqual(clock.T, {
            "reply_window": 7 * DAY,                                       # 7.2 step 1
            "window_margin": 10 * MIN,                                     # 7.2 step 1
            "rate_window": HOUR,                                           # G2, 7.2 step 4
            "clock_offset_margin": 600.001,                                # offsets and millisecond floor
            "cap_margin": 660.001,                                         # 7.2 + bounded unknown-flight guard
            "public_delay": (2, 4),                                        # 7.4, 8.4
            "full_sweep_every": 30 * MIN,                                  # 8.3
            "clock_back": 5 * MIN,                                         # 8.1
            "clock_jump": 10 * MIN,                                        # 8.1
            "token_min_age": 24 * HOUR,                                    # 8.2
            "token_refresh_age": 7 * DAY,                                  # 8.2
            "token_retry_after": 6 * HOUR,                                 # 8.2
            "token_warn_left": 14 * DAY,                                   # 8.2
            "token_lifetime": 60 * DAY,                                    # 8.2
            "probe_schedule": (5 * MIN, 10 * MIN, 20 * MIN, 30 * MIN),     # 8.5
            "probe_first_rate": 15 * MIN,                                  # 8.5
            "hold_uncertain_base": 15 * MIN,                               # 8.5
            "hold_uncertain_max": 6 * HOUR,                                # 8.5
            "hold_blocked_base": 30 * MIN,                                 # 8.5
            "hold_blocked_max": 12 * HOUR,                                 # 8.5
            "hold_spam": 24 * HOUR,                                        # 8.5
            "hold_streak_base": HOUR,                                      # 8.5
            "hold_streak_max": 12 * HOUR,                                  # 8.5
            "metric_every": HOUR,                                          # 8.7
            "waiting_after": 30 * MIN,                                     # 9
            "expiring_within": 24 * HOUR,                                  # 9
            "reel_stale_after": 2 * HOUR,                                  # 9
            "not_running_after": 10 * MIN,                                 # 9
            "open_claim_after": 3 * MIN,                                   # 9
            "public_owed_after": 10 * MIN,                                 # 9
            "report_dm_history": 24 * HOUR,                                # 9
            "refusals_within": 24 * HOUR,                                  # 9
            "followers_ago": (24 * HOUR, 7 * DAY),                         # 9
            "step_cap": 200,                                               # 14.4
            "inbound_stale": 10 * MIN,                                     # 14.5
            "inbound_max_age": 24 * HOUR,                                  # 14.5
            "followup_window": 23 * HOUR,                                  # 14.4
            "recheck_after": (DAY, 7 * DAY),                               # 14.5
            "max_prompts": 3,                                              # 14.4
            "poll_interval_min": 30,                                       # 4
            "poll_interval_max": 5 * MIN,                                  # 4
            "lock_wait_run": 60,                                           # 3
            "lock_retry": 1,                                               # 3
            "sleep_slice": 1,                                              # 3
            "http_socket_timeout": 30,                                     # 6
            "http_deadline": 60,                                           # 6
            "tg_timeout": 10,                                              # 9
            "db_busy_timeout": 30,                                         # 3
        })

    def test_every_duration_is_positive_and_only_rate_margins_use_milliseconds(self):
        for key, value in clock.T.items():
            with self.subTest(key=key):
                for seconds in value if isinstance(value, tuple) else (value,):
                    self.assertIsInstance(seconds, float if key in ("clock_offset_margin", "cap_margin") else int)
                    self.assertGreater(seconds, 0)


if __name__ == "__main__":
    unittest.main()
