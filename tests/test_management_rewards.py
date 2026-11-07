from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))
from gym_env import RustGymEnv, TOGGLE_BASE, USE_BASE, outcome_reward_breakdown

class ManagementRewardTests(unittest.TestCase):
    def test_fixed_rewards_are_capped_and_win_once(self):
        old = {'rents_paid': 11, 'done': False, 'won': False}
        new = {'rents_paid': 12, 'done': False, 'won': False}
        self.assertEqual(outcome_reward_breakdown(old, new, 'fixed', 1), {'rent': 1.0, 'terminal': 0.0})
        self.assertEqual(outcome_reward_breakdown(new, dict(new, rents_paid=13), 'fixed', 1)['rent'], 0)
        self.assertEqual(outcome_reward_breakdown(new, dict(new, done=True, won=True), 'fixed', 1)['terminal'], 20)

    def test_toggle_penalty_and_same_spin_lock(self):
        env = RustGymEnv(41, reward_mode='fixed', enable_management=True)
        obs, _ = env.reset()
        while not obs['done']:
            if obs['phase'] == 'ready' and obs['managed_items']:
                break
            action = 0 if obs['phase'] == 'ready' else 3
            obs, _, done, _, _ = env.step(action)
            if done: self.fail('game ended before receiving an item')
        for i, entry in enumerate(obs['managed_items']):
            if entry['can_toggle']:
                obs, reward, done, _, info = env.step(TOGGLE_BASE + i)
                self.assertEqual(reward, -0.05)
                self.assertEqual(info['step_record']['managed_target']['name'], entry['name'])
                updated = next(x for x in obs['managed_items'] if x['name'] == entry['name'])
                self.assertNotEqual(updated['disabled'], entry['disabled'])
                self.assertFalse(updated['can_toggle'])
                return
        self.fail('no toggleable item')

if __name__ == '__main__':
    unittest.main()
