"""Managed agents see and dispatch rent rescue / optional pillow actions."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'python'))
from gym_env import RustGymEnv, TOGGLE_BASE, USE_BASE
from rust_env import RustEnv


class RentDecisionTests(unittest.TestCase):
    def test_managed_mode_is_enabled_and_survives_both_reset_methods(self):
        env = RustGymEnv(41, enable_management=True)
        self.assertTrue(env.observation['manual_decisions'])
        self.assertTrue(env.reset(42)[0]['manual_decisions'])
        self.assertTrue(env.env.reset_text('RENTDECISION')['manual_decisions'])
        self.assertFalse(RustEnv(41).observation['manual_decisions'])

    def test_each_emergency_item_and_pillow_has_a_legal_use_action(self):
        env = RustGymEnv(41, enable_management=True, allow_item_toggle=False)
        for kind, name in [('item', 'piggy_bank'), ('item', 'swear_jar'),
                           ('item', 'coffee'), ('item', 'devils_deal'),
                           ('item', 'comfy_pillow'), ('essence', 'comfy_pillow_essence')]:
            with self.subTest(name=name):
                obs = copy.deepcopy(env.observation)
                obs['managed_items'] = [dict(kind=kind, name=name, type_id=7, count=1,
                                            disabled=False, can_toggle=True, can_use=True)]
                obs['rent_pending'] = name in ('piggy_bank', 'swear_jar', 'coffee', 'devils_deal')
                obs['spins_left'] = 0 if obs['rent_pending'] else (2 if kind == 'essence' else 1)
                fake = Mock(observation=obs, seed=41)
                env.env = fake
                mask = env.action_mask()
                self.assertTrue(mask[USE_BASE])
                self.assertFalse(mask[TOGGLE_BASE])
                _, _, _, _, info = env.step(USE_BASE)
                fake.use_managed.assert_called_once_with(kind == 'essence', 7)
                self.assertEqual(info['step_record']['managed_target']['name'], name)

    def test_rent_confirmation_and_pillow_decline_are_distinguishable_in_logs(self):
        for pending in [False, True]:
            env = RustGymEnv(41, enable_management=True)
            obs = copy.deepcopy(env.observation)
            obs['rent_pending'] = pending
            obs['managed_items'] = [dict(kind='item', name='comfy_pillow', type_id=7,
                                        can_use=not pending, can_toggle=True, count=1)]
            fake = Mock(observation=obs, seed=41)
            fake.step.return_value = obs
            env.env = fake
            record = env.step(0)[-1]['step_record']
            self.assertEqual(record['action_kind'], 'pay_rent' if pending else 'spin')
            self.assertEqual(record['pillow_declined'], not pending)


if __name__ == '__main__':
    unittest.main()
