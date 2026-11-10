"""Initial fake-fault obligations against the real bot and isolated loopback fake."""
import json
import tempfile
import unittest
from pathlib import Path
from sim import invariants, scenarios, soak

BOT = Path(__file__).resolve().parents[1] / 'bot.py'


class InitialFaults(unittest.TestCase):
    def run_faults(self, faults):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        plan = scenarios.Plan('initial-fault-coverage', scale=1000)
        plan.config['public_reply'] = False
        plan.load(plan.comment('спасибо!', should_match=False))
        plan.fake['faults'] = faults
        plan.step('start')
        plan.until('rows')
        plan.until('ok', kind='comments')
        directory = Path(temporary.name)
        run = soak.Run(plan.scenario(), BOT, directory)
        violations, _ = run.run()
        self.assertEqual(violations, invariants.replay(directory))
        return run, violations, directory

    def test_unfired_initial_fault_is_required_and_survives_replay(self):
        run, violations, directory = self.run_faults([
            {'id': 'initial-miss', 'kind': 'dm', 'nth': 1, 'delay_real_sec': .01}])
        self.assertTrue(any(v.invariant == 'RUN' for v in violations))
        coverage = next(c for c in run.coverage if c['kind'] == 'fault')
        self.assertEqual((coverage['id'], coverage['hits'], coverage['outcome'], coverage['required']),
                         ('initial-miss', 0, 'missed', True))
        self.assertTrue(json.loads((directory / 'run-result.json').read_text())['completed'])

    def test_fired_initial_fault_has_healthy_required_coverage(self):
        run, violations, _ = self.run_faults([
            {'id': 'initial-hit', 'kind': 'comments', 'nth': 1, 'delay_real_sec': .01}])
        self.assertEqual(violations, [])
        coverage = next(c for c in run.coverage if c['kind'] == 'fault')
        self.assertEqual((coverage['id'], coverage['hits'], coverage['outcome'], coverage['required']),
                         ('initial-hit', 1, 'met', True))
        self.assertEqual(coverage['installed_step'], -1)

    def test_initial_optional_miss_keeps_reason_and_strips_fake_metadata(self):
        fault = {'id': 'optional-miss', 'kind': 'dm', 'nth': 1, 'delay_real_sec': .01,
                 'required': False, 'reason': 'intentional background observation', 'min_fires': 2}
        run, violations, directory = self.run_faults([fault])
        self.assertEqual(violations, [])
        coverage = next(c for c in run.coverage if c['kind'] == 'fault')
        self.assertEqual((coverage['outcome'], coverage['required'], coverage['reason'], coverage['min_fires']),
                         ('missed', False, 'intentional background observation', 2))
        fake_fault = json.loads((directory / 'fake.json').read_text())['faults'][0]
        self.assertFalse({'required', 'reason', 'min_fires'} & set(fake_fault))
        self.assertEqual(run.scenario['fake']['faults'][0], fault)

    def test_initial_optional_without_reason_is_runtime_failure(self):
        run, violations, _ = self.run_faults([
            {'id': 'optional-no-reason', 'kind': 'dm', 'nth': 1,
             'delay_real_sec': .01, 'required': False}])
        self.assertTrue(any(v.invariant == 'RUN' and 'requires a reason' in v.evidence for v in violations))
        coverage = next(c for c in run.coverage if c['kind'] == 'fault')
        self.assertFalse(coverage['required'])
        self.assertEqual(coverage['outcome'], 'missed')

    def test_initial_faults_use_fake_generated_ids_and_minimum_hits(self):
        run, violations, _ = self.run_faults([
            {'kind': 'comments', 'nth': [1, 2], 'delay_real_sec': .01, 'min_fires': 2},
            {'kind': 'dm', 'nth': 1, 'delay_real_sec': .01}])
        coverage = [c for c in run.coverage if c['kind'] == 'fault']
        self.assertEqual([(c['id'], c['hits'], c['min_fires'], c['outcome']) for c in coverage],
                         [('f1', 2, 2, 'met'), ('f2', 0, 1, 'missed')])
        self.assertTrue(any(v.invariant == 'RUN' and 'f2' in v.evidence for v in violations))

    def test_initial_minimum_unmet_is_runtime_failure_even_after_one_hit(self):
        run, violations, _ = self.run_faults([
            {'id': 'insufficient', 'kind': 'comments', 'nth': 1,
             'delay_real_sec': .01, 'min_fires': 2}])
        coverage = next(c for c in run.coverage if c['kind'] == 'fault')
        self.assertEqual((coverage['hits'], coverage['min_fires'], coverage['outcome']), (1, 2, 'missed'))
        self.assertTrue(any(v.invariant == 'RUN' for v in violations))
