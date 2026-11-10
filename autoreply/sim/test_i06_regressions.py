"""I06 negative controls: synthetic traces and native SQLite, no external service."""
import json
import tempfile
import unittest
from sim import invariants, scenarios, soak
from sim.test_invariants import Case, T0, iso, error

class HarnessObligations(unittest.TestCase):
    def run_case(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        run = soak.Run(scenarios.Plan('obligations').scenario(), 'unused', temp.name)
        run.anchor = T0
        run.seq = lambda: 0
        run.wait_for = lambda *args, **kwargs: False
        return run

    def test_required_until_timeout_is_failure(self):
        run = self.run_case()
        run.until({'what': 'rows', 'timeout': 0})
        self.assertTrue(run.problems)

    def test_optional_until_requires_reason_and_records_coverage(self):
        run = self.run_case()
        run.until({'what': 'rows', 'timeout': 0, 'required': False, 'reason': 'background observation'})
        self.assertFalse(run.problems)
        self.assertEqual(run.coverage[-1]['outcome'], 'missed')
        self.assertFalse(run.coverage[-1]['required'])

    def test_required_fault_without_until_cannot_disappear(self):
        run = self.run_case()
        run.ctl = lambda *args, **kwargs: {}
        run.requests_since = lambda seq: []
        run.faults({'add': [{'id': 'unfired', 'kind': 'dm', 'nth': 999}]})
        run.check_faults()
        self.assertTrue(run.problems)

    def test_stop_timeout_matches_deployment(self):
        self.assertEqual(soak.STOP_TIMEOUT, 120)

class OracleRegressions(unittest.TestCase):
    def case(self):
        return Case(self, automatic_journal=False)

    def world(self, case):
        return invariants.World(case.truth, case.db_path, case.scenario, case.events)

    def attempt(self, case, cid, start, finish, outcome='ok'):
        case.db.execute('INSERT INTO attempts(comment_id,kind,started_at,finished_at,outcome) VALUES (?, ?, ?, ?, ?)',
                        (cid, 'dm', iso(T0 + start), iso(T0 + finish) if finish is not None else None, outcome))

    def test_arm_is_replacement_even_after_unlimited(self):
        case = self.case()
        case.event('arm', n='all', boundary_id=0)
        case.event('arm', n=1, boundary_id=0)
        for num in range(2):
            cid = str(num)
            case.comment(cid, user=cid)
            self.attempt(case, cid, num * 60, num * 60 + 1)
            case.dm(cid, num * 60 + .5)
        self.assertTrue(invariants._budget(self.world(case)))

    def test_not_sent_consumes_budget_without_http_post(self):
        case = self.case()
        case.event('arm', n=1, boundary_id=0)
        case.comment('c')
        self.attempt(case, 'c', 0, 1, 'not_sent')
        self.attempt(case, 'c', 60, 61)
        case.dm('c', 60.5)
        self.assertTrue(invariants._budget(self.world(case)))

    def test_disarm_only_preserves_specific_prior_claim(self):
        case = self.case()
        case.event('arm', n=1, boundary_id=0)
        case.event('disarm', boundary_id=0, seq_after=0)
        case.comment('c')
        self.attempt(case, 'c', 2, 4)
        case.dm('c', 3)
        self.assertTrue(invariants._budget(self.world(case)))

    def test_old_claim_after_rearm_is_not_charged_twice(self):
        case = self.case()
        case.event('arm', n=1, boundary_id=0)
        case.event('disarm', boundary_id=1, seq_after=0)
        case.comment('c')
        self.attempt(case, 'c', 0, 4)
        case.dm('c', 3)
        self.assertEqual(invariants._budget(self.world(case)), [])

    def test_refusal_does_not_justify_dup(self):
        case = self.case()
        case.comment('old')
        case.comment('dup')
        case.dm('old', 1, status=400, response=error(551))
        self.attempt(case, 'old', 0, 2, 'rejected')
        case.row('old', 'refused', at=2)
        case.row('dup', 'dup_user', reason='dup:old', at=3)
        self.assertTrue(invariants.i9(self.world(case)))

    def test_future_handled_state_does_not_justify_dup(self):
        case = self.case()
        case.comment('old')
        case.comment('dup')
        case.dm('old', 10)
        self.attempt(case, 'old', 9, 11)
        case.row('old', 'done', reason='public_off', at=11)
        case.row('dup', 'dup_user', reason='dup:old', at=3)
        self.assertTrue(invariants.i9(self.world(case)))

    def test_inflight_claim_at_dup_is_valid_even_if_later_refused(self):
        case = self.case()
        case.comment('old')
        case.comment('dup')
        case.dm('old', 1, status=400, response=error(551))
        self.attempt(case, 'old', 0, 5, 'rejected')
        case.row('old', 'refused', at=5)
        case.row('dup', 'dup_user', reason='dup:old', at=3)
        self.assertEqual(invariants.i9(self.world(case)), [])

    def test_report_requires_exact_nonnegative_integer(self):
        for value in ({}, [], {'unaccounted': False}, {'unaccounted': '0'}, {'unaccounted': -1},
                      {'unaccounted': 0.0}, {'unaccounted': {}}, {'nested': {'unaccounted': 0}}):
            with self.subTest(value=value):
                case = self.case()
                case.event('report', rc=0, text=json.dumps(value))
                self.assertTrue(invariants.i12(self.world(case)))

    def test_report_zero_valid_only_with_independent_reconciliation(self):
        case = self.case()
        case.event('report', rc=0, text='{"unaccounted": 0}')
        self.assertEqual(invariants.i12(self.world(case)), [])
        case.comment('unfinished')
        case.row('unfinished', 'sending')
        self.assertTrue(invariants.i12(self.world(case)))

    def test_legacy_missing_gate_evidence_is_inconclusive(self):
        case = self.case()
        case.served('c', 'u', 60)
        self.assertTrue(any(v.invariant == 'INCONCLUSIVE' for v in invariants.check(
            case.truth, case.db_path, case.scenario, case.events)))

class RuntimeEvidence(unittest.TestCase):
    def make_run(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        from pathlib import Path
        run = soak.Run(scenarios.Plan('runtime').scenario(), 'unused', temp.name)
        run.anchor = T0
        run.seq = lambda: 0
        run.requests_since = lambda seq: []
        run.wait_for = lambda *args, **kwargs: False
        self.path = Path(temp.name)
        return run

    def test_settle_timeout_is_required_failure(self):
        run = self.make_run()
        run.ctl = lambda *args, **kwargs: {}
        run.start = lambda: None
        run.settle()
        self.assertEqual(run.coverage[-1]['kind'], 'settle')
        self.assertTrue(run.problems)

    def test_optional_without_reason_is_error(self):
        run = self.make_run()
        run.until({'what': 'rows', 'timeout': 0, 'required': False})
        self.assertTrue(run.problems)

    def test_fault_repeat_count_is_required_and_metadata_stays_out_of_fake(self):
        run = self.make_run()
        calls = []
        run.ctl = lambda path, body: calls.append(body)
        run.faults({'required': True, 'min_fires': 2, 'add': [{'id':'twice','kind':'dm','delay_real_sec':.01}]})
        self.assertEqual(calls, [{'add':[{'id':'twice','kind':'dm','delay_real_sec':.01}]}])
        run.requests_since = lambda seq: [{'fault_id':'twice'}]
        run.check_faults()
        self.assertEqual(run.coverage[-1]['hits'], 1)
        self.assertTrue(run.problems)

    def test_snapshot_and_start_event_precede_child_creation(self):
        from types import SimpleNamespace
        from unittest import mock
        run = self.make_run()
        run.api_port = 1234
        run.snapshot = lambda: {'kv': {'me_id':'cached'}, 'known_comments':['existing']}
        def launch(*args, **kwargs):
            self.assertTrue(run.events, 'child started before the process boundary was recorded')
            self.assertEqual(run.events[-1]['op'], 'start')
            self.assertEqual(run.events[-1]['snapshot']['known_comments'], ['existing'])
            return SimpleNamespace(pid=123)
        with mock.patch.object(soak.subprocess, 'Popen', side_effect=launch):
            run.start()
        self.assertEqual(run.events[-1]['pid'], 123)

    def materialize(self, run):
        truth = {'requests': [], 'comments': [], 'dms': [], 'replies': [], 'telegram': [], 'events': []}
        for name,value in (('truth',truth),('scenario',run.scenario),('events',run.events)):
            (self.path/(name+'.json')).write_text(json.dumps(value))

    def test_run_only_failure_survives_replay(self):
        run = self.make_run()
        run.problem('arm', 'unexpected exit')
        run.save_result(completed=True)
        self.materialize(run)
        self.assertEqual(invariants.replay(self.path), run.problems)

    def test_legacy_replay_cannot_pass_without_runtime_evidence(self):
        run = self.make_run()
        self.materialize(run)
        self.assertTrue(any(v.invariant == 'INCONCLUSIVE' for v in invariants.replay(self.path)))

    def test_completed_clean_replay_passes(self):
        run = self.make_run()
        run.save_result(completed=True)
        self.materialize(run)
        self.assertEqual(invariants.replay(self.path), [])

    def test_future_journal_entry_cannot_explain_prior_http(self):
        case = Case(self, automatic_journal=False)
        case.comment('c')
        case.event('arm', n=1, boundary_id=0)
        case.dm('c', 1)
        case.db.execute('INSERT INTO attempts(comment_id,kind,started_at,finished_at,outcome) VALUES (?,?,?,?,?)',
                        ('c','dm',iso(T0+10),iso(T0+11),'ok'))
        w = invariants.World(case.truth, case.db_path, case.scenario, case.events)
        self.assertTrue(invariants._budget(w))
