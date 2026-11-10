"""Raw probe responses must establish/lift G6 account gates independently of core flags."""
import re
import unittest
from sim import test_i06_gates as fixtures


class ReadHolds(unittest.TestCase):
    trace = fixtures.Gates.trace
    http = fixtures.Gates.http
    identity = fixtures.Gates.identity
    sweep = fixtures.Gates.sweep
    judge = fixtures.Gates.judge

    def ready(self):
        case = self.trace(cached=True)
        self.sweep(case)
        return case

    def probe(self, case, at=60, status=200, body=None):
        self.http(case, 'me', at, status=status,
                  body={'id': '1', 'user_id': '2', 'username': 'owner'} if body is None else body)

    def require_account_rejection(self, case, kind='dm'):
        violations = self.judge(case, kind)
        self.assertTrue(any(v.invariant == 'I7' and 'account' in v.evidence for v in violations), violations)

    def test_changed_identity_blocks_dm_and_public(self):
        for kind in ('dm', 'public'):
            with self.subTest(kind=kind):
                case = self.ready()
                self.probe(case, body={'user_id': 'other', 'id': '9', 'username': 'alien'})
                self.require_account_rejection(case, kind)

    def test_repeated_changed_identity_does_not_lift_hold(self):
        case = self.ready()
        self.probe(case, body={'user_id': 'other', 'id': '9', 'username': 'alien'})
        self.probe(case, at=65, body={'user_id': 'other', 'id': '9', 'username': 'alien'})
        self.require_account_rejection(case)

    def test_original_identity_lifts_mismatch_hold(self):
        case = self.ready()
        self.probe(case, body={'user_id': 'other', 'id': '9', 'username': 'alien'})
        self.probe(case, at=65)
        self.assertEqual(self.judge(case), [])

    def test_global_probe_codes_and_http_429_block(self):
        for status, body in ((400, {'error': {'code': 190}}), (400, {'error': {'code': 4}}),
                             (400, {'error': {'code': 10}}), (400, {'error': {'code': 368}}),
                             (429, 'Too many requests'), (400, {'error': {'code': '190'}})):
            with self.subTest(status=status, body=body):
                case = self.ready(); self.probe(case, status=status, body=body)
                self.require_account_rejection(case)

    def test_global_probe_requires_success_to_recover(self):
        case = self.ready()
        self.probe(case, status=400, body={'error': {'code': 190}})
        self.probe(case, at=65, status=500, body={'error': {'code': 190}})
        self.require_account_rejection(case)

    def test_compatible_partial_probe_lifts_hold_using_cached_fields(self):
        case = self.ready()
        self.probe(case, status=400, body={'error': {'code': 190}})
        self.probe(case, at=65, body={'user_id': '2'})
        self.assertEqual(self.judge(case), [])

    def test_cached_identity_allows_two_ordinary_transient_probes(self):
        for status, body in ((500, {'error': {'code': 190}}), (400, {'error': {'code': 1}}),
                             (200, {'error': {'code': 2}})):
            with self.subTest(status=status, body=body):
                case = self.ready()
                self.probe(case, status=status, body=body)
                self.probe(case, at=65, status=status, body=body)
                self.assertEqual(self.judge(case), [])

    def test_third_unanswered_probe_blocks(self):
        case = self.ready()
        for at in (60, 65, 70):
            self.probe(case, at=at, status=500, body={'error': {'code': 190}})
        self.require_account_rejection(case)

    def test_no_response_completions_count_as_failed_probes(self):
        case = self.ready()
        for at in (60, 65, 70):
            self.probe(case, at=at, status=0, body={})
            case.truth['requests'][-1]['sent'] = False
        self.require_account_rejection(case)

    def test_success_resets_probe_failure_streak(self):
        case = self.ready()
        for at in (60, 65):
            self.probe(case, at=at, status=500, body={'error': {'code': 190}})
        self.probe(case, at=70)
        self.probe(case, at=75, status=500, body={'error': {'code': 190}})
        self.assertEqual(self.judge(case), [])

    def test_success_body_without_user_id_cannot_lift_hold(self):
        case = self.ready()
        self.probe(case, status=400, body={'error': {'code': 190}})
        self.probe(case, at=65, body={'id': '1', 'username': 'owner'})
        self.require_account_rejection(case)

    def read_failure_then_collect(self, status=400, body=None, probe=None):
        case = self.ready()
        self.http(case, 'comments', 60, fixtures.MEDIA, status=status,
                  body={'error': {'code': 190}} if body is None else body)
        if probe is not None:
            self.probe(case, at=65, **probe)
        self.http(case, 'media', 70, fixtures.MEDIA, {'comments_count': 1})
        self.http(case, 'comments', 75, fixtures.MEDIA, {'data': [{'id': 'c'}]})
        return case

    def test_global_read_without_probe_cannot_be_erased_by_collect(self):
        self.require_account_rejection(self.read_failure_then_collect())

    def test_failed_probe_after_global_read_blocks_at_first_failure(self):
        case = self.read_failure_then_collect(probe={'status': 500, 'body': {'error': {'code': 2}}})
        self.require_account_rejection(case)

    def test_good_probe_after_global_read_is_a_reel_only_failure(self):
        self.assertEqual(self.judge(self.read_failure_then_collect(probe={})), [])

    def test_transient_read_does_not_require_probe_after_clean_retry(self):
        case = self.read_failure_then_collect(status=500, body={'error': {'code': 190}})
        self.assertEqual(self.judge(case), [])

    def test_refresh_refusal_never_sets_account_hold(self):
        case = self.ready()
        self.http(case, 'refresh', 60, status=400, body={'error': {'code': 190}})
        self.assertEqual(self.judge(case), [])

    def test_claimless_previous_process_can_establish_account_hold(self):
        case = self.ready()
        self.probe(case, at=10, status=400, body={'error': {'code': 190}})
        case.event('start', at=40, pid=8, monotonic=140., snapshot={
            'kv': {'me_id': '1', 'me_user_id': '2', 'me_username': 'owner'}, 'known_comments': ['c']})
        old_count = len(case.lines)
        case.lines.append('INFO cycle_start pid=8 cycle=1 monotonic=145.0')
        self.http(case, 'media', 50, fixtures.MEDIA, {'comments_count': 1})
        self.http(case, 'comments', 55, fixtures.MEDIA, {'data': [{'id': 'c'}]})
        for index in range(old_count + 1, len(case.lines)):
            number = index - old_count
            line = case.lines[index].replace('pid=7', 'pid=8')
            case.lines[index] = re.sub(r'call=\d+', 'call='+str(number), line)
        self.require_account_rejection(case)

    def test_an_unidentifiable_neighbor_does_not_stop_the_reel(self):
        # an element nobody can identify is counted and told by the bot (kv unreadable:<media>); it costs only itself
        for item in ({}, None, [], {'id': ''}, {'id': False}, {'id': []}, {'id': 1.5}):
            with self.subTest(item=item):
                case = self.trace(cached=True)
                self.sweep(case, items=[{'id': 'c'}, item])
                self.assertEqual(self.judge(case), [])

    def test_an_unidentifiable_item_in_a_head_does_not_revoke_the_full_sweep(self):
        for full_retry in (False, True):
            with self.subTest(full_retry=full_retry):
                case = self.ready()
                case.lines.append('INFO cycle_start pid=7 cycle=2 monotonic=150.0')
                self.http(case, 'comments', 60, fixtures.MEDIA, {'data': [{'id': 'c'}, {}]})
                case.lines.append('INFO cycle_start pid=7 cycle=3 monotonic=170.0')
                if full_retry:
                    self.http(case, 'media', 75, fixtures.MEDIA, {'comments_count': 1})
                self.http(case, 'comments', 80, fixtures.MEDIA, {'data': [{'id': 'c'}]})
                self.assertEqual(self.judge(case), [])

    def test_integer_zero_and_duplicate_usable_ids_are_not_missing(self):
        for items in ([{'id': 'c'}, {'id': 0}], [{'id': 'c'}, {'id': 'c'}]):
            with self.subTest(items=items):
                case = self.trace(cached=True)
                self.sweep(case, items=items)
                self.assertEqual(self.judge(case), [])


if __name__ == '__main__':
    unittest.main()
