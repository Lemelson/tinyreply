"""F34: accepted lost-person events carry positive counts, not mere status mentions."""
import unittest

from sim import invariants
from sim import test_invariants as fixtures


class LostAlertSemantics(unittest.TestCase):
    case = fixtures.RefereeTest.case
    healthy = fixtures.RefereeTest.healthy
    lost = fixtures.I12Alerts.lost

    def judge(self, case):
        world = invariants.World(case.truth, case.db_path, case.scenario, case.events)
        return [v for v in invariants.i12(world) if 'heard nothing' in v.evidence]

    def event_text(self, status, count=1, total=1):
        payload = f'{count} people lost: their comments are past the 7-day window' if status=='expired' else \
                  f'{count} DM(s) may or may not have been delivered: they are never sent again'
        return f'ℹ {status}: {payload} ({total} in all)'

    def test_positive_event_and_missing_controls(self):
        for status in ('expired', 'uncertain'):
            with self.subTest(status=status):
                self.assertEqual(len(self.judge(self.lost(status))), 1)
                self.assertEqual(self.judge(self.lost(status, self.event_text(status))), [])

    def test_clear_digest_zero_and_other_event_mentions_cannot_discharge_loss(self):
        for status in ('expired', 'uncertain'):
            for text in (f'✓ cleared: {status}', f'{status} cleared', f'ℹ digest: {status} 0',
                         f'⚠ hold:send: {status}', f'ℹ digest: {status} 1', f'⚠ {status}_manual: 1 person',
                         self.event_text(status, count=0, total=0)):
                with self.subTest(status=status, text=text):
                    self.assertEqual(len(self.judge(self.lost(status, text))), 1)

    def test_queued_unsent_event_does_not_make_a_clear_message_sufficient(self):
        for status in ('expired', 'uncertain'):
            with self.subTest(status=status):
                case = self.lost(status, f'✓ cleared: {status}')
                case.db.execute('INSERT INTO outbox(key,text,sent_at) VALUES(?,?,NULL)',
                                (status, self.event_text(status)))
                self.assertEqual(len(self.judge(case)), 1)

    def test_accepted_batch_counts_even_when_db_confirmation_is_missing(self):
        for status in ('expired', 'uncertain'):
            with self.subTest(status=status):
                text = self.event_text(status)
                case = self.lost(status, 'ℹ started: ready\n'+text+'\n✓ cleared: waiting')
                case.db.execute('INSERT INTO outbox(key,text,sent_at) VALUES(?,?,NULL)', (status,text))
                self.assertEqual(self.judge(case), [])

    def test_exact_typed_localized_outbox_payload_is_accepted_without_sent_at(self):
        for status, text in (('expired','ℹ expired: 1 человек потерян (1 in all)'),
                             ('uncertain','ℹ uncertain: 1 доставка неизвестна (1 in all)')):
            with self.subTest(status=status):
                case = self.lost(status, text)
                case.db.execute('INSERT INTO outbox(key,text,sent_at) VALUES(?,?,NULL)', (status,text))
                self.assertEqual(self.judge(case), [])

    def test_payload_substring_is_not_an_accepted_complete_event_line(self):
        for status in ('expired','uncertain'):
            with self.subTest(status=status):
                text = self.event_text(status)
                case = self.lost(status, 'ℹ digest: previous event was '+text)
                case.db.execute('INSERT INTO outbox(key,text,sent_at) VALUES(?,?,NULL)', (status,text))
                self.assertEqual(len(self.judge(case)), 1)

    def test_count_one_cannot_cover_two_people_even_if_the_event_is_resent(self):
        for status in ('expired','uncertain'):
            with self.subTest(status=status):
                case = self.lost(status)
                case.comment('second-lost', user='second-user', age=8*fixtures.DAY)
                case.row('second-lost',status,at=1000)
                text = self.event_text(status)
                case.telegram(text,at=1060);case.telegram(text,at=1061)
                self.assertEqual(len(self.judge(case)),1)

    def test_count_two_covers_two_people_and_distinct_events_preserve_coverage(self):
        for status in ('expired','uncertain'):
            for texts in ((self.event_text(status,2,2),),
                          (self.event_text(status,1,1),self.event_text(status,1,2))):
                with self.subTest(status=status,texts=texts):
                    case=self.lost(status)
                    case.comment('second-lost',user='second-user',age=8*fixtures.DAY)
                    case.row('second-lost',status,at=1000)
                    for text in texts:case.telegram(text,at=1060)
                    self.assertEqual(self.judge(case),[])

    def test_positive_event_after_deadline_still_fails(self):
        for status in ('expired','uncertain'):
            with self.subTest(status=status):
                case=self.lost(status)
                # `expired` may wait for the pace of alerts.repeat_min on top of the deadline (engine._lost_people)
                paced=60*case.scenario['config']['alerts']['repeat_min'] if status=='expired' else 0
                case.telegram(self.event_text(status),at=1000+invariants.ALERT_WITHIN+paced+2)
                self.assertEqual(len(self.judge(case)),1)
                if paced:
                    on_time=self.lost(status)
                    on_time.telegram(self.event_text(status),at=1000+invariants.ALERT_WITHIN+paced-2)
                    self.assertEqual(self.judge(on_time),[])


if __name__ == '__main__':
    unittest.main()
