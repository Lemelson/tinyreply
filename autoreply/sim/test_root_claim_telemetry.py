"""F08 exact committed-claim ordering and F06 malformed diagnostic controls."""
import unittest

from sim import invariants
from sim import test_i06_gates as fixtures


class ClaimTelemetry(unittest.TestCase):
    trace = fixtures.Gates.trace
    http = fixtures.Gates.http
    identity = fixtures.Gates.identity
    sweep = fixtures.Gates.sweep
    judge = fixtures.Gates.judge

    def fact(self, case, kind='dm', at=200.00008, pid=7, attempt=1):
        case.lines.append(f'INFO claim_committed pid={pid} monotonic={at:.9f} attempt={attempt} kind={kind}')

    def stamped_fact(self, case, stamp=None, **fields):
        self.fact(case, **fields)
        stamp = fixtures.iso(fixtures.T0+100) if stamp is None else stamp
        case.lines[-1] += ' started_at='+stamp

    def overlap(self):
        case = self.trace(cached=True)
        self.sweep(case, complete=100.00002)
        return case

    def test_legacy_floor_overlap_remains_inconclusive(self):
        violations = self.judge(self.overlap())
        self.assertTrue(any(v.invariant == 'INCONCLUSIVE' for v in violations), violations)

    def test_exact_committed_point_resolves_healthy_floor_overlap(self):
        for kind in ('dm', 'public'):
            with self.subTest(kind=kind):
                case = self.overlap(); self.fact(case, kind)
                self.assertEqual(self.judge(case, kind), [])

    def test_exact_point_after_new_cycle_rejects_missing_current_collect(self):
        for kind in ('dm', 'public'):
            with self.subTest(kind=kind):
                case = self.trace(cached=True); self.sweep(case)
                case.lines.append('INFO cycle_start pid=7 cycle=2 monotonic=200.00002')
                self.fact(case, kind)
                violations = self.judge(case, kind)
                self.assertTrue(any(v.invariant == 'I7' and 'this cycle' in v.evidence for v in violations), violations)
                self.assertFalse(any(v.invariant == 'INCONCLUSIVE' for v in violations), violations)

    def test_exact_point_rejects_account_hold(self):
        case = self.trace(cached=True); self.sweep(case)
        self.http(case, 'me', 90, status=400, body={'error': {'code': 190}}, complete=100.00002)
        self.fact(case)
        violations = self.judge(case)
        self.assertTrue(any(v.invariant == 'I7' and 'account' in v.evidence for v in violations), violations)

    def test_exact_point_cannot_substitute_for_a_full_sweep(self):
        case = self.trace(cached=True)
        self.http(case, 'comments', 90, fixtures.MEDIA, {'data': [{'id': 'c'}]}, complete=100.00002)
        self.fact(case)
        violations = self.judge(case)
        self.assertTrue(any(v.invariant == 'I7' and 'full sweep' in v.evidence for v in violations), violations)

    def test_exact_point_rejects_sending_false(self):
        case = self.overlap(); case.scenario['config']['sending'] = False; self.fact(case)
        violations = self.judge(case)
        self.assertTrue(any(v.invariant == 'I7' and 'sending=false' in v.evidence for v in violations), violations)
        self.assertFalse(any(v.invariant == 'INCONCLUSIVE' for v in violations), violations)

    def test_exact_point_before_future_completion_does_not_borrow_it(self):
        case = self.overlap(); self.fact(case, at=200.00001)
        violations = self.judge(case)
        self.assertTrue(any(v.invariant == 'I7' and 'collect' in v.evidence for v in violations), violations)

    def test_mismatched_or_out_of_process_facts_do_not_resolve_overlap(self):
        for change in ({'pid': 8}, {'attempt': 999}, {'kind': 'public'}, {'at': 89}, {'at': 203}):
            with self.subTest(change=change):
                case = self.overlap(); self.fact(case, **change)
                violations = self.judge(case)
                self.assertTrue(any(v.invariant == 'INCONCLUSIVE' for v in violations), violations)

    def test_fact_after_confirmed_process_stop_does_not_resolve_overlap(self):
        case = self.overlap(); self.fact(case)
        case.event('stop', at=100.00004, monotonic=200.00004, pid=7)
        violations = self.judge(case)
        self.assertTrue(any(v.invariant == 'INCONCLUSIVE' for v in violations), violations)

    def test_duplicate_conflicting_fact_is_not_chosen_arbitrarily(self):
        case = self.overlap(); self.fact(case); self.fact(case, at=200.00001)
        violations = self.judge(case)
        self.assertTrue(any(v.invariant == 'INCONCLUSIVE' for v in violations), violations)

    def test_claim_already_in_prelaunch_snapshot_is_not_a_new_fact(self):
        for kind in ('dm', 'public'):
            with self.subTest(kind=kind):
                case = self.overlap(); self.fact(case, kind)
                case.events[0]['snapshot']['max_dm_attempt_id'] = 1
                violations = self.judge(case, kind)
                self.assertTrue(any(v.invariant == 'INCONCLUSIVE' for v in violations), violations)

    def test_fact_after_its_receiver_arrival_cannot_resolve_overlap(self):
        case = self.overlap(); self.fact(case)
        case.dm('c', at=100.00005, monotonic=200.00005, sent=False)
        violations = self.judge(case)
        self.assertTrue(any(v.invariant == 'INCONCLUSIVE' for v in violations), violations)

    def test_exact_clock_collision_still_cannot_prove_event_order(self):
        case = self.overlap(); self.fact(case, at=200.00002)
        violations = self.judge(case)
        self.assertTrue(any(v.invariant == 'INCONCLUSIVE' for v in violations), violations)

    def test_claim_query_time_may_exceed_a_millisecond(self):
        case = self.overlap(); self.fact(case, at=201.0)
        self.assertEqual(self.judge(case), [])

    def test_malformed_call_or_http_never_raises_or_becomes_pass(self):
        for field, value in (('call', '70STATE_DIR=/tmp/fake'), ('call', 'nan'), ('call', '-1'),
                             ('http', '200STATE_DIR=/tmp/fake'), ('http', 'nan'), ('http', '999')):
            with self.subTest(field=field, value=value):
                case = self.overlap(); self.fact(case)
                case.lines[-2] = case.lines[-2].replace('call=2 ', f'call={value} ') if field == 'call' else \
                    case.lines[-2].replace('http=200', f'http={value}')
                violations = self.judge(case)
                self.assertTrue(any(v.invariant == 'INCONCLUSIVE' for v in violations), violations)
                self.assertFalse(any(v.invariant == 'I7' for v in violations), violations)

    def test_malformed_claim_monotonic_never_resolves_overlap(self):
        for value in ('nan', 'inf', '-inf', '-1', '200.00008STATE_DIR=/tmp/fake'):
            with self.subTest(value=value):
                case = self.overlap()
                case.lines.append(f'INFO claim_committed pid=7 monotonic={value} attempt=1 kind=dm')
                violations = self.judge(case)
                self.assertTrue(any(v.invariant == 'INCONCLUSIVE' for v in violations), violations)

    def test_healthy_complete_atomic_lines_stay_valid_amid_unrelated_text(self):
        case = self.overlap(); self.fact(case)
        case.lines.insert(0, 'STATE_DIR=/tmp/fake CONFIG=/tmp/fake/config.json')
        case.lines.append('armed: budget 9')
        self.assertEqual(self.judge(case), [])

    def test_journal_matched_stamp_survives_startup_clock_drift_both_directions(self):
        for kind in ('dm','public'):
            for drift in (-.003,-.000244,.000244,.003):
                with self.subTest(kind=kind,drift=drift):
                    case=self.overlap();case.events[0]['t']+=drift
                    self.stamped_fact(case,kind=kind)
                    self.assertEqual(self.judge(case,kind),[])

    def test_journal_matched_stamp_does_not_convert_finish_through_startup_clock(self):
        case=self.overlap();case.events[0]['t']+=.003;self.stamped_fact(case)
        case.db.execute('INSERT INTO attempts(comment_id,kind,started_at,finished_at,outcome) VALUES(?,?,?,?,?)',
                        ('c','dm',fixtures.iso(fixtures.T0+100),fixtures.iso(fixtures.T0+100.0001),'ok'))
        case.event('output',text='\n'.join(case.lines))
        self.assertEqual(invariants._g6(invariants.World(case.truth,case.db_path,case.scenario,case.events)),[])

    def test_legacy_unstamped_fact_keeps_conservative_drift_rejection(self):
        case=self.overlap();case.events[0]['t']-=.003;self.fact(case)
        self.assertTrue(any(v.invariant=='INCONCLUSIVE' for v in self.judge(case)))

    def test_stamped_claim_without_post_needs_no_inferred_wall_finish(self):
        for kind in ('dm','public'):
            for outcome in ('not_sent',None):
                with self.subTest(kind=kind,outcome=outcome):
                    case=self.overlap();case.events[0]['t']-=.003;self.stamped_fact(case,kind=kind)
                    finished=fixtures.iso(fixtures.T0+100.0001) if outcome else None
                    case.db.execute('INSERT INTO attempts(comment_id,kind,started_at,finished_at,outcome) VALUES(?,?,?,?,?)',
                                    ('c',kind,fixtures.iso(fixtures.T0+100),finished,outcome))
                    case.event('stop',at=100.0001,monotonic=200.0001,pid=7)
                    case.event('output',text='\n'.join(case.lines))
                    self.assertEqual(invariants._g6(invariants.World(case.truth,case.db_path,case.scenario,case.events)),[])

    def test_bad_provided_stamp_never_downgrades_to_legacy_acceptance(self):
        for stamp in (fixtures.iso(fixtures.T0+99), 'not-a-timestamp', '',
                      fixtures.iso(fixtures.T0+100).replace('Z','+00:00'),
                      '2026-99-99T00:00:00.000Z'):
            with self.subTest(stamp=stamp):
                case=self.overlap();self.stamped_fact(case,stamp=stamp)
                self.assertTrue(any(v.invariant=='INCONCLUSIVE' for v in self.judge(case)))

    def test_stamped_fact_preserves_journal_process_and_receipt_rejections(self):
        for change in ({'pid':8},{'attempt':999},{'kind':'public'},{'at':89},
                       {'at':200.00002},{'duplicate':True},{'stopped':True},{'received':True},
                       {'prelaunch':True},{'future_process':True}):
            with self.subTest(change=change):
                case=self.overlap()
                self.stamped_fact(case,**{k:v for k,v in change.items() if k in ('pid','attempt','kind','at')})
                if change.get('duplicate'):self.stamped_fact(case)
                if change.get('stopped'):case.event('stop',at=100.00004,monotonic=200.00004,pid=7)
                if change.get('received'):case.dm('c',at=100.00005,monotonic=200.00005,sent=False)
                if change.get('prelaunch'):case.events[0]['snapshot']['max_dm_attempt_id']=1
                if change.get('future_process'):
                    case.event('start',at=100.00004,monotonic=200.00004,pid=8,
                               snapshot={'kv':{},'known_comments':['c'],'max_dm_attempt_id':1})
                self.assertTrue(any(v.invariant=='INCONCLUSIVE' for v in self.judge(case)))

    def test_stamped_fact_preserves_independently_reconstructed_gates(self):
        for gate in ('current','full','account','sending'):
            with self.subTest(gate=gate):
                case=self.overlap()
                if gate=='current':case.lines.append('INFO cycle_start pid=7 cycle=2 monotonic=200.00004')
                if gate=='full':
                    case=self.trace(cached=True)
                    self.http(case,'comments',90,fixtures.MEDIA,{'data':[{'id':'c'}]},complete=100.00002)
                if gate=='account':self.http(case,'me',99,status=400,body={'error':{'code':190}},complete=100.00004)
                if gate=='sending':case.scenario['config']['sending']=False
                self.stamped_fact(case)
                violations=self.judge(case)
                self.assertTrue(any(v.invariant=='I7' for v in violations),violations)
                self.assertFalse(any(v.invariant=='INCONCLUSIVE' for v in violations),violations)


if __name__ == '__main__':
    unittest.main()
