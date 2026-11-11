"""The scratch policy cannot use expert ratings, including after loading."""
from pathlib import Path
import copy
import sys
import unittest

import numpy as np
import torch
from gymnasium import spaces

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from gym_env import RustGymEnv  # noqa: E402
from scratch_policy import NoExpertScoresExtractor, expert_score_indices  # noqa: E402
from train_ppo import MAX_OFFERS, OBS_DIM, featurize  # noqa: E402


class ScratchPolicyTests(unittest.TestCase):
    def test_every_expert_score_is_removed_without_losing_other_features(self):
        env = RustGymEnv(0)
        env.reset(0)
        obs, *_ = env.step(0)
        obs = copy.deepcopy(obs)
        obs["offer"] = [dict(obs["offer"][0], index=i, expert_score=12.0)
                        for i in range(MAX_OFFERS)]
        with_scores = featurize(obs)
        without_scores = featurize(obs, use_expert_scores=False)
        differing = np.flatnonzero(with_scores != without_scores).tolist()
        self.assertEqual(differing, expert_score_indices())
        self.assertEqual(len(differing), MAX_OFFERS)
        extractor = NoExpertScoresExtractor(spaces.Box(-np.inf, np.inf, (OBS_DIM,), dtype=np.float32))
        with torch.no_grad():
            a = extractor(torch.from_numpy(with_scores).unsqueeze(0))
            b = extractor(torch.from_numpy(without_scores).unsqueeze(0))
        torch.testing.assert_close(a, b)


if __name__ == "__main__":
    unittest.main()
