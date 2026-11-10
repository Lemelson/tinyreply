"""R12: G6 is a claim contract, and rounded clocks cannot prove boundary ordering."""
import copy
import unittest
from sim import invariants
from sim import test_i06_gates as fixtures


class ClaimEdges(unittest.TestCase):
    trace = fixtures.Gates.trace
    http = fixtures.Gates.http
    identity = fixtures.Gates.identity
    sweep = fixtures.Gates.sweep

    def ready(self, sending=True):
        case = self.trace(cached=True)
        case.scenario['config']['sending'] = sending
        self.sweep(case)
        return case

    def judge(self, case, kind='dm', at=100):
        # A committed claim consumed an admission even when no POST reached the receiver.
        case.db.execute('INSERT INTO attempts(comment_id,kind,started_at,finished_at,outcome) VALUES (?,?,?,?,?)',
                        ('c', kind, fixtures.iso(fixtures.T0 + at), fixtures.iso(fixtures.T0 + at + 2), 'not_sent'))
        case.event('output', text='\n'.join(case.lines))
        return invariants._g6(invariants.World(case.truth, case.db_path, case.scenario, case.events))

    def test_sending_false_blocks_dm_and_public_claims_without_post(self):
        for kind in ('dm', 'public'):
            with self.subTest(kind=kind):
                result = self.judge(self.ready(sending=False), kind)
                self.assertTrue(any(v.invariant == 'I7' and 'sending=false' in v.evidence for v in result), result)

    def test_sending_true_not_sent_claim_is_valid(self):
        self.assertEqual(self.judge(self.ready()), [])

    def test_public_claim_exempts_budget_only(self):
        case = self.ready()
        case.event('disarm', at=90, boundary_id=0, rc=0)
        self.assertEqual(self.judge(case, kind='public'), [])
        world = invariants.World(case.truth, case.db_path, case.scenario, case.events)
        self.assertEqual(invariants._budget(world), [])

    def test_claim_before_later_cycle_is_valid_when_separated(self):
        case = self.ready()
        case.lines.append('INFO cycle_start pid=7 cycle=2 monotonic=200.01')
        self.assertEqual(self.judge(case), [])

    def test_claim_after_new_cycle_without_collect_is_invalid_when_separated(self):
        case = self.ready()
        case.lines.append('INFO cycle_start pid=7 cycle=2 monotonic=199.99')
        result = self.judge(case)
        self.assertTrue(any(v.invariant == 'I7' and 'this cycle' in v.evidence for v in result), result)

    def test_claim_rounding_overlaps_new_cycle_is_inconclusive(self):
        for kind in ('dm', 'public'):
            for boundary in (199.99998, 200., 200.0003, 200.001):
                with self.subTest(kind=kind, boundary=boundary):
                    case = self.ready()
                    case.lines.append(f'INFO cycle_start pid=7 cycle=2 monotonic={boundary}')
                    result = self.judge(case, kind)
                    self.assertTrue(any(v.invariant == 'INCONCLUSIVE' for v in result), result)

    def test_claim_rounding_overlaps_http_completion_is_inconclusive(self):
        case = self.trace(cached=True)
        self.sweep(case, complete=100.0003)
        self.assertTrue(any(v.invariant == 'INCONCLUSIVE' for v in self.judge(case)))

    def test_loaded_config_stays_in_force_until_restart(self):
        for original in (False, True):
            with self.subTest(original=original):
                case = self.ready(sending=original)
                candidate = copy.deepcopy(case.scenario['config'])
                candidate['sending'] = not original
                case.event('config', at=50, config=candidate)
                result = self.judge(case)
                self.assertEqual(any('sending=false' in v.evidence for v in result), not original)

    def test_candidate_with_same_request_seq_as_start_does_not_replace_loaded_config(self):
        for original in (False, True):
            with self.subTest(original=original):
                case = self.trace(cached=True)
                case.scenario['config']['sending'] = original
                candidate = copy.deepcopy(case.scenario['config'])
                candidate['sending'] = not original
                case.event('config', at=0, config=candidate)
                self.sweep(case)
                result = self.judge(case)
                self.assertEqual(any('sending=false' in v.evidence for v in result), not original)

    def test_restarted_process_uses_its_new_config(self):
        for original in (False, True):
            with self.subTest(original=original):
                case = self.ready(sending=original)
                candidate = copy.deepcopy(case.scenario['config'])
                candidate['sending'] = not original
                case.event('config', at=40, config=candidate)
                case.event('start', at=50, pid=8, monotonic=150., snapshot={
                    'kv': {'me_id': '1', 'me_user_id': '2', 'me_username': 'owner'}, 'known_comments': ['c']})
                case.lines.append('INFO cycle_start pid=8 cycle=1 monotonic=151.')
                old = len(case.lines)
                self.http(case, 'media', 60, fixtures.MEDIA, {'comments_count': 1})
                self.http(case, 'comments', 61, fixtures.MEDIA, {'data': [{'id': 'c'}]})
                for index in range(old, len(case.lines)):
                    case.lines[index] = case.lines[index].replace('pid=7', 'pid=8').replace(
                        f'call={index-old+3}', f'call={index-old+1}')
                result = self.judge(case)
                self.assertEqual(any('sending=false' in v.evidence for v in result), original)


if __name__ == '__main__':
    unittest.main()
