"""G6 HTTP-semantic controls, independent of engine truth flags."""
import importlib.util
import os
import unittest
from sim import invariants
from sim.test_invariants import Case, MEDIA, OTHER_MEDIA, T0, iso

if os.environ.get('I06_BASELINE'):
    spec = importlib.util.spec_from_file_location('i06_baseline', os.environ['I06_BASELINE'])
    invariants = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(invariants)


class Gates(unittest.TestCase):
    def trace(self, cached=False):
        c = Case(self, automatic_journal=False)
        c.comment('c')
        c.row('c', 'done', reason='public_off', at=102)
        c.scenario['scale'] = 1
        c.events = []
        kv = {'me_id': '1', 'me_user_id': '2', 'me_username': 'owner'} if cached else {}
        c.event('start', at=-10, pid=7, monotonic=90., snapshot={'kv': kv, 'known_comments': ['c']})
        c.event('arm', at=-9, n='all', boundary_id=0)
        c.lines = ['INFO cycle_start pid=7 cycle=1 monotonic=100.0']
        return c

    def http(self, c, kind, at, target=None, body=None, status=200, complete=None, params=None):
        route = '/v26.0/' + ('me' if kind == 'me' else str(target)) + ('/comments' if kind == 'comments' else '')
        r = c.request(kind, at, target=target, response=body, status=status, path=route,
                      monotonic=100. + at, params=params or {})
        c.lines.append(f'INFO http_complete pid=7 monotonic={100. + (complete if complete is not None else at + .1)} '
                       f'call={r["seq"]} method=GET kind=read_{kind} route={route} http={status}')
        return r

    def identity(self, c, body=None):
        self.http(c, 'me', 1, body=body if body is not None else {'id': '1', 'user_id': '2', 'username': 'owner'})

    def sweep(self, c, target=MEDIA, next_page=None, count=1, items=None, complete=None, shortcode='x'):
        self.http(c, 'media', 2, target, {'id': target, 'comments_count': count, 'shortcode': shortcode})
        body = {'data': [{'id': 'c'}] if items is None else items}
        if next_page:
            body['paging'] = {'next': next_page}
        self.http(c, 'comments', 3, target, body, complete=complete)

    def judge(self, c, kind='dm', at=100):
        c.db.execute('INSERT INTO attempts(comment_id,kind,started_at,finished_at,outcome) VALUES (?,?,?,?,?)',
                     ('c', kind, iso(T0+at), iso(T0+at+2), 'ok'))
        c.event('output', text='\n'.join(c.lines))
        w = invariants.World(c.truth, c.db_path, c.scenario, c.events)
        # The frozen i7 was the entry point claiming G6 coverage; new code exposes its independent model.
        return getattr(invariants, '_g6', invariants.i7)(w)

    def test_identity_absent(self):
        c = self.trace(); self.sweep(c)
        self.assertTrue(self.judge(c))

    def test_identity_partial(self):
        c = self.trace(); self.identity(c, {'user_id': '2'}); self.sweep(c)
        self.assertTrue(self.judge(c))

    def test_sweep_absent(self):
        c = self.trace(); self.identity(c)
        self.assertTrue(self.judge(c))

    def test_pagination_unfinished(self):
        c = self.trace(); self.identity(c)
        self.sweep(c, next_page=f'http://127.0.0.1/v26.0/{MEDIA}/comments?after=cursor')
        self.assertTrue(self.judge(c))

    def test_wrong_reel_sweep(self):
        c = self.trace(); self.identity(c); self.sweep(c, target=OTHER_MEDIA)
        self.assertTrue(self.judge(c))

    def test_current_cycle_without_collect(self):
        c = self.trace(); self.identity(c); self.sweep(c)
        c.lines.append('INFO cycle_start pid=7 cycle=2 monotonic=150.0')
        self.assertTrue(self.judge(c))

    def test_current_collect_failure_blocks_dm_and_public(self):
        for kind in ('dm', 'public'):
            with self.subTest(kind=kind):
                c = self.trace(); self.identity(c); self.sweep(c)
                c.lines.append('INFO cycle_start pid=7 cycle=2 monotonic=150.0')
                self.http(c, 'comments', 60, MEDIA, {'error': {'code': 2}}, status=500)
                self.assertTrue(self.judge(c, kind))

    def test_later_completion_does_not_justify_earlier_claim(self):
        c = self.trace(); self.identity(c); self.sweep(c, complete=101)
        self.assertTrue(self.judge(c))

    def test_empty_full_with_positive_count(self):
        c = self.trace(); self.identity(c); self.sweep(c, count=2, items=[])
        self.assertTrue(self.judge(c))

    def test_permalink_mismatch(self):
        c = self.trace(); self.identity(c)
        c.scenario['config']['reels'][0]['permalink'] = 'https://www.instagram.com/reel/expected/'
        self.sweep(c, shortcode='wrong')
        self.assertTrue(self.judge(c))

    def test_incorrect_pagination_cursor(self):
        c = self.trace(); self.identity(c)
        self.sweep(c, next_page=f'http://127.0.0.1/v26.0/{MEDIA}/comments?after=right')
        self.http(c, 'comments', 4, MEDIA, {'data': []}, params={'after': 'wrong'})
        self.assertTrue(self.judge(c))

    def test_full_chain_and_fresh_head_are_valid(self):
        c = self.trace(); self.identity(c)
        self.sweep(c, next_page=f'http://127.0.0.1/v26.0/{MEDIA}/comments?after=right')
        self.http(c, 'comments', 4, MEDIA, {'data': []}, params={'after': 'right'})
        c.lines.append('INFO cycle_start pid=7 cycle=2 monotonic=150.0')
        # A head page containing only known ids legitimately stops despite paging.next.
        self.http(c, 'comments', 60, MEDIA, {'data': [{'id': 'c'}], 'paging': {'next': 'http://unused/?after=x'}})
        self.assertEqual(self.judge(c), [])

    def test_cached_identity_still_requires_new_process_full(self):
        c = self.trace(cached=True)
        self.sweep(c)
        self.assertEqual(self.judge(c), [])
        c = self.trace(cached=True)
        self.http(c, 'comments', 3, MEDIA, {'data': [{'id': 'c'}]})
        self.assertTrue(self.judge(c))

    def test_400_pages_with_more_data_not_full(self):
        c = self.trace(); self.identity(c)
        self.sweep(c, next_page=f'http://127.0.0.1/v26.0/{MEDIA}/comments?after=1')
        for number in range(1,400):
            self.http(c, 'comments', 3+number/100, MEDIA,
                      {'data': [], 'paging': {'next': f'http://127.0.0.1/v26.0/{MEDIA}/comments?after={number+1}'}},
                      complete=3+number/100+.001, params={'after': str(number)})
        self.assertTrue(self.judge(c))

    def test_fake_afterwrite_timestamp_is_not_a_client_boundary(self):
        c = self.trace(); self.identity(c); self.sweep(c)
        for r in c.truth['requests']:
            r['completed_monotonic'] = 999999.
        self.assertEqual(self.judge(c), [])

    def test_deleted_client_completion_diagnostic_is_inconclusive(self):
        c = self.trace(); self.identity(c); self.sweep(c)
        c.lines = [line for line in c.lines if 'call=2 ' not in line]
        self.assertTrue(any(v.invariant == 'INCONCLUSIVE' for v in self.judge(c)))
