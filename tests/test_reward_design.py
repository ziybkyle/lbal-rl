"""Tests for progressive rent, terminal rewards and time-limit handling."""
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from gym_env import RustGymEnv, outcome_reward_breakdown  # noqa: E402
from train_ppo import LbalGym  # noqa: E402
from reward_schemes import (DEFAULT_PROGRESSIVE_RENTS, parse_progressive_rents, validate_progressive_rents,
                            validate_progressive_outcomes)  # noqa: E402
from stable_baselines3.common.vec_env import DummyVecEnv  # noqa: E402


class RewardDesignTests(unittest.TestCase):
    def test_custom_progressive_rents_are_scaled_and_only_change_selected_stages(self):
        rents = list(DEFAULT_PROGRESSIVE_RENTS)
        rents[5] = 1.5
        before = {'rents_paid': 5, 'done': False, 'won': False}
        after = {'rents_paid': 6, 'done': False, 'won': False}
        result = outcome_reward_breakdown(before, after, 'progressive', 1, rents)
        self.assertAlmostEqual(result['rent'], 1.5 / 20)
        unchanged = outcome_reward_breakdown({'rents_paid': 4}, before, 'progressive', 1, rents)
        self.assertAlmostEqual(unchanged['rent'], DEFAULT_PROGRESSIVE_RENTS[4] / 20)
        final = outcome_reward_breakdown(after, dict(after, done=True), 'progressive', 1, rents)
        self.assertEqual(final['terminal'], -3 / 20)
        spanning = outcome_reward_breakdown({'rents_paid': 4}, after, 'progressive', 1, rents)
        self.assertAlmostEqual(spanning['rent'], (rents[4] + rents[5]) / 20)
        adapter = LbalGym(seed=0, reward_mode='progressive', reward_gamma=1, progressive_rents=rents)
        self.assertEqual(adapter._env.progressive_rents, tuple(rents))
        rents[5] = 4
        self.assertEqual(adapter._env.progressive_rents[5], 1.5)

    def test_progressive_rent_settings_validate_shape_range_and_numbers(self):
        for invalid in ([1] * 11, [1] * 13, [1] * 11 + [-1], [1] * 11 + [5.1],
                        [1] * 11 + [float('nan')], [1] * 11 + [True], [1] * 11 + ['1']):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_progressive_rents(invalid)
        with self.assertRaises(ValueError):
            parse_progressive_rents('invalid json')

    def test_custom_progressive_terminal_rewards_and_time_limits(self):
        before = {'rents_paid': 5, 'done': False, 'won': False}
        for won, expected in ((False, -7.0 / 20), (True, 35.0 / 20)):
            after = dict(before, done=True, won=won)
            result = outcome_reward_breakdown(before, after, 'progressive', 1,
                                              progressive_loss=-7, progressive_win=35)
            self.assertAlmostEqual(result['terminal'], expected)
        ongoing = outcome_reward_breakdown(before, dict(before), 'progressive', 1,
                                           progressive_loss=-7, progressive_win=35)
        self.assertEqual(ongoing['terminal'], 0)
        env = DummyVecEnv([lambda: LbalGym(seed=0, max_steps=1, reward_mode='progressive', reward_gamma=1,
                                           enable_management=True, progressive_loss=-7, progressive_win=35)])
        try:
            env.reset()
            _, _, dones, infos = env.step([0])
            self.assertTrue(dones[0])
            self.assertTrue(infos[0]['TimeLimit.truncated'])
            self.assertEqual(infos[0]['step_record']['reward_parts']['terminal'], 0)
        finally:
            env.close()
        for invalid in ((-21, 20), (1, 20), (-3, -1), (-3, 51), (float('nan'), 20), (-3, float('inf')), (True, 20)):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_progressive_outcomes(*invalid)

    def test_periodic_evaluation_uses_the_saved_rent_table(self):
        import periodic_evaluate
        rents = list(DEFAULT_PROGRESSIVE_RENTS)
        rents[5] = 1.4
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            summary = {'manifest': {'reward_mode': 'progressive', 'progressive_rents': rents,
                                    'progressive_loss': -7, 'progressive_win': 35}, 'training_games': 2}
            (directory / 'summary.json').write_text(json.dumps(summary), encoding='utf-8')
            (directory / 'model_final.zip').write_bytes(b'evaluation checkpoint fixture')
            def fake_evaluate(model_path, out, label, seeds, **kwargs):
                self.assertEqual(kwargs['progressive_rents'], rents)
                self.assertEqual((kwargs['progressive_loss'], kwargs['progressive_win']), (-7, 35))
                row = {'seed': seeds.start, 'won': False, 'rents_paid': 2, 'actions': {}}
                (out / f'{label}_games.jsonl').write_text(json.dumps(row) + '\n', encoding='utf-8')
                return {'label': label, 'games': 1, 'valid_games': 1, 'wins': 0, 'win_rate': 0.0, 'avg_rents': 2}
            with patch.object(periodic_evaluate, 'evaluate', side_effect=fake_evaluate), \
                 patch.object(periodic_evaluate.torch, 'set_num_threads'), \
                 patch('sys.argv', ['periodic_evaluate', str(directory), '--games', '1']):
                periodic_evaluate.main()
            self.assertTrue((directory / '定期评估报告.html').is_file())

    def test_progressive_rents_and_terminal_outcomes_are_scaled(self):
        path = [{'rents_paid': k, 'done': False, 'won': False} for k in range(13)]
        rents = [outcome_reward_breakdown(a, b, 'progressive', 1)['rent'] for a, b in zip(path, path[1:])]
        self.assertAlmostEqual(rents[0], 0.5 / 20)
        self.assertAlmostEqual(rents[-1], 1.5 / 20)
        self.assertAlmostEqual(sum(rents), 12 / 20)
        for successes, expected_raw in ((0, -3), (4, -0.4545454545454546), (11, 7.5), (12, 9)):
            end = dict(path[successes], done=True)
            result = sum(rents[:successes]) + sum(outcome_reward_breakdown(path[successes], end, 'progressive', 1).values())
            self.assertAlmostEqual(result, expected_raw / 20)
        win = dict(path[12], done=True, won=True)
        self.assertAlmostEqual(sum(rents) + sum(outcome_reward_breakdown(path[12], win, 'progressive', 1).values()), 32 / 20)
        self.assertEqual(outcome_reward_breakdown(path[12], dict(path[12], rents_paid=13), 'progressive', 1)['rent'], 0)

    def test_progressive_time_limit_bootstraps_without_failure_penalty(self):
        env = DummyVecEnv([lambda: LbalGym(seed=0, max_steps=1, reward_mode='progressive', reward_gamma=1,
                                           enable_management=True, toggle_reward=0)])
        try:
            env.reset()
            _, rewards, dones, infos = env.step([0])
            self.assertTrue(dones[0])
            self.assertTrue(infos[0]['TimeLimit.truncated'])
            self.assertIn('terminal_observation', infos[0])
            self.assertEqual(infos[0]['step_record']['reward_parts']['terminal'], 0)
            self.assertEqual(rewards[0], 0)
        finally:
            env.close()

    def test_gym_adapter_propagates_reward_configuration(self):
        env = LbalGym(seed=0)
        env.reset(seed=0)
        _, reward, _, _, info = env.step(0)
        self.assertEqual(env._env.reward_gamma, 1)
        self.assertEqual(info["step_record"]["reward_mode"], "progressive")
        self.assertAlmostEqual(reward, sum(info["step_record"]["reward_parts"].values()), places=4)

    def test_invalid_modes_and_gamma_are_rejected(self):
        for mode in ("fixed", "win_potential", "progress_potential", "legacy", "sparse", "potential", "unknown"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                RustGymEnv(reward_mode=mode)
        with self.assertRaises(ValueError):
            RustGymEnv(reward_gamma=0)


if __name__ == "__main__":
    unittest.main()
