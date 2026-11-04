"""Action-mask and Gym-wrapper checks for the Rust environment."""
from pathlib import Path
import random
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from gym_env import (  # noqa: E402
    ACTION_SIZE,
    OFFER_BASE,
    REMOVE_BASE,
    RustGymEnv,
    VecRustEnv,
    random_policy,
)
from rust_env import CompactBatchEnv, parse_compact  # noqa: E402
from rust_env import BatchRustEnv  # noqa: E402


class GymEnvTests(unittest.TestCase):
    def test_ready_mask_only_allows_spin_without_tokens(self):
        env = RustGymEnv(0)
        env.reset()
        mask = env.action_mask()
        self.assertEqual(len(mask), ACTION_SIZE)
        self.assertEqual([index for index, allowed in enumerate(mask) if allowed], [0])

    def test_reward_mask_exposes_offers_and_skip(self):
        env = RustGymEnv(0)
        env.reset()
        env.step(0)
        obs = env.observation
        self.assertEqual(obs["phase"], "spin_reward")
        mask = env.action_mask()
        self.assertTrue(mask[1])
        offer_actions = [OFFER_BASE + entry["index"] for entry in obs["offer"]]
        self.assertTrue(offer_actions)
        self.assertTrue(all(mask[action] for action in offer_actions))
        env.step(offer_actions[0])
        self.assertFalse(env.done)

    def test_removal_mask_skips_unremovable_symbols(self):
        env = RustGymEnv(0)
        obs = {
            "done": False,
            "phase": "item_reward",
            "can_remove": True,
            "can_reroll": False,
            "offer": [],
            "deck": [{"can_remove": False}, {"can_remove": True}],
        }
        mask = env.action_mask(obs)
        self.assertFalse(mask[REMOVE_BASE])
        self.assertTrue(mask[REMOVE_BASE + 1])

    def test_invalid_action_raises(self):
        env = RustGymEnv(1)
        env.reset()
        with self.assertRaises(ValueError):
            env.step(OFFER_BASE)

    def test_rent_payment_returns_rent_reward(self):
        env = RustGymEnv(2)
        env.reset()
        rng = random.Random(2)
        paid = False
        for _ in range(4000):
            if env.done:
                env.reset()
            before = env.observation["rents_paid"]
            _, reward, _, _, _ = env.step(random_policy(env, rng))
            if env.observation["rents_paid"] > before:
                self.assertGreaterEqual(reward, 10.0)
                paid = True
                break
        self.assertTrue(paid)

    def test_batch_env_steps_and_resets_slots(self):
        batch = BatchRustEnv(4, seed=10)
        observations = batch.reset_all(10)
        self.assertEqual(len(observations), 4)
        observations = batch.step_batch([0, 0, 0, 0])
        self.assertEqual(len(observations), 4)
        self.assertTrue(all(obs["phase"] != "ready" for obs in observations))
        single = batch.step(0, -1)
        self.assertEqual(single["total_spins"], observations[0]["total_spins"])
        reset_obs = batch.reset_at(2, 99)
        self.assertEqual(reset_obs["total_spins"], 0)
        self.assertEqual(len(batch._env.observe_all()), 4)

    def test_compact_observation_layout(self):
        env = RustGymEnv(0)
        env.reset()
        compact = parse_compact(env.env._env.observe_compact())
        self.assertEqual(compact["header"]["magic"], 0x4C42414C)
        self.assertEqual(compact["header"]["phase"], 0)
        self.assertEqual(compact["header"]["deck_len"], 8)
        self.assertEqual(len(compact["board"]), 20)
        env.step(0)
        compact = parse_compact(env.env._env.observe_compact())
        self.assertEqual(compact["header"]["phase"], 1)
        self.assertEqual(compact["header"]["candidate_count"], 3)

    def test_compact_batch_env_steps(self):
        env = CompactBatchEnv(4, seed=0)
        env.reset_all(0)
        observations = env.step_batch([0, 0, 0, 0])
        self.assertEqual(len(observations), 4)
        self.assertTrue(all(obs["header"]["phase"] == 1 for obs in observations))
        self.assertEqual(len(observations[0]["candidates"]), 3)

    def test_vector_env_steps_and_auto_resets(self):
        vec = VecRustEnv(3, seed=0, max_steps=5)
        vec.reset([0, 1, 2])
        for _ in range(12):
            actions = [random_policy(env, random.Random(0)) if not env.done else 0
                       for env in vec.envs]
            obs, rewards, terminated, truncated, infos = vec.step(actions)
            self.assertEqual(len(obs), 3)
            self.assertEqual(len(rewards), 3)
        self.assertTrue(any(info.get("auto_reset") for info in infos))


if __name__ == "__main__":
    unittest.main()
