"""Rule observations, positional invariance, and train/save compatibility."""
import copy
import itertools
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
import torch
from sb3_contrib import MaskablePPO
from sb3_contrib.common.wrappers import ActionMasker
from stable_baselines3.common.vec_env import DummyVecEnv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'python'))
from candidate_rules import catalog, DECK_FEATURES  # noqa: E402
from candidate_policy import RuleCandidatePolicy, offer_indices, upgrade_model  # noqa: E402
from gym_env import RustGymEnv, OFFER_BASE, MAX_OFFERS  # noqa: E402
from scratch_policy import NoExpertScoresExtractor  # noqa: E402
from train_ppo import LbalGym, MANAGED_OBS_DIM, featurize  # noqa: E402


def observed(names=('coal', 'coin', 'toddler'), kind='symbol'):
    obs = copy.deepcopy(RustGymEnv(0, enable_management=True).observation)
    obs['phase'] = {'symbol': 'spin_reward', 'item': 'item_reward', 'essence': 'essence_reward'}[kind]
    obs['offer'] = []
    c = catalog()
    for index, name in enumerate(names):
        card = c.cards[c.identity(kind, name) - 1]
        obs['offer'].append({'index': index, 'name': name, 'kind': kind,
                             'type_id': sum(x['kind'] == kind and x['name'] < name for x in c.cards),
                             'rarity': card.get('rarity', 'common'), 'expert_score': 0})
    return obs


class CandidateRuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        torch.set_num_threads(1)

    def make_policy(self):
        torch.manual_seed(7)
        env = LbalGym(0, enable_management=True, use_expert_scores=False, candidate_rules=True)
        policy = RuleCandidatePolicy(env.observation_space, env.action_space, lambda _: 3e-4,
                                     net_arch={'pi': [256, 256], 'vf': [256, 256]})
        # Exercise trained (nonzero) rule/context paths as well as initialization.
        for layer in (policy.rule_to_candidate, policy.rule_to_actor, policy.rule_to_value):
            torch.nn.init.normal_(layer.weight, std=0.02)
        policy.set_training_mode(False)
        return env, policy

    def test_catalog_contains_actual_rules_and_disjoint_identities(self):
        c = catalog()
        self.assertEqual(len(set(c.identities.values())), len(c.cards))
        self.assertEqual(set(card['kind'] for card in c.cards), {'symbol', 'item', 'essence'})
        self.assertNotEqual(c.identity('item', 'copycat'), c.identity('essence', 'copycat_essence'))
        self.assertEqual(c.delays['coal'], (20, 0))
        self.assertEqual(c.delays['rabbit'], (0, 10))
        self.assertEqual(c.transform_targets[c.identity('symbol', 'coal')], c.identity('symbol', 'diamond'))
        self.assertGreater(c.table[c.identity('symbol', 'coal'), c.feature_names.index('transform_target_base_value')], 0)
        self.assertEqual(c.references[c.identity('symbol', 'toddler'), c.identity('symbol', 'candy')], 1)
        self.assertEqual(c.references[c.identity('symbol', 'moon'), c.identity('symbol', 'rabbit')], 1)
        self.assertEqual(c.references[c.identity('item', 'time_machine'), c.identity('symbol', 'coal')], 1)
        row = c.table[c.identity('essence', 'copycat_essence')]
        self.assertGreater(row[c.feature_names.index('role:conditional')], 0)
        self.assertGreater(row[c.feature_names.index('value:1')], 0)

    def test_growth_state_tracks_coal_and_once_only_rabbit_maturity(self):
        c = catalog()
        obs = observed()
        obs['deck'] = [dict(index=0, type_id=0, name='coal', rarity='common', bonus=0, multiplier=1,
                            times_displayed=19, times_coins_given=0, saved=0, can_remove=True),
                       dict(index=1, type_id=0, name='rabbit', rarity='uncommon', bonus=2, multiplier=1,
                            times_displayed=2, times_coins_given=12, saved=0, can_remove=True)]
        new = featurize(obs, False, True, True)
        start = MANAGED_OBS_DIM + MAX_OFFERS + c.count
        self.assertAlmostEqual(float(new[start + 3]), 1 / 20)
        self.assertEqual(float(new[start + DECK_FEATURES + 4]), 0)
        obs['deck'][1]['times_coins_given'] = 2
        new = featurize(obs, False, True, True)
        self.assertAlmostEqual(float(new[start + DECK_FEATURES + 4]), 8 / 20)
        obs['items'] = [dict(name='time_machine', kind='item', type_id=0, rarity='uncommon')]
        new = featurize(obs, False, True, True)
        self.assertEqual(new[start + 3], 0)

    def test_candidate_permutation_moves_scores_without_changing_other_actions_or_value(self):
        env, policy = self.make_policy()
        for kind, names in [('symbol', ('coal', 'coin', 'toddler')),
                            ('item', ('time_machine', 'green_pepper', 'copycat')),
                            ('essence', ('copycat_essence', 'time_machine_essence', 'green_pepper_essence'))]:
            original = observed(names, kind)
            base = None
            for order in itertools.permutations(range(3)):
                obs = copy.deepcopy(original)
                obs['offer'] = [dict(original['offer'][i], index=j) for j, i in enumerate(order)]
                f = torch.from_numpy(featurize(obs, False, True, True)).unsqueeze(0)
                mask = np.asarray(env._env.action_mask(obs))
                with torch.no_grad():
                    distribution, value = policy._distribution(f, mask)
                    probs = distribution.distribution.probs[0].numpy()
                reordered = probs.copy()
                for j, old in enumerate(order):
                    reordered[OFFER_BASE + old] = probs[OFFER_BASE + j]
                if base is None:
                    base = reordered, value
                else:
                    np.testing.assert_allclose(reordered, base[0], rtol=2e-5, atol=2e-6)
                    torch.testing.assert_close(value, base[1], rtol=2e-5, atol=2e-6)

    def test_identity_rule_parameters_receive_gradients_and_expert_scores_are_ignored(self):
        env, policy = self.make_policy()
        obs = observed()
        features = featurize(obs, False, True, True)
        altered = features.copy()
        altered[np.asarray(offer_indices())[:, -1]] = 1000
        mask = np.asarray(env._env.action_mask(obs))
        x = torch.from_numpy(features).unsqueeze(0)
        y = torch.from_numpy(altered).unsqueeze(0)
        a, value_a = policy._parts(x)
        b, value_b = policy._parts(y)
        torch.testing.assert_close(a, b)
        torch.testing.assert_close(value_a, value_b)
        values, log_prob, _ = policy.evaluate_actions(x, torch.tensor([OFFER_BASE]), mask)
        (-log_prob.mean() + values.square().mean()).backward()
        self.assertGreater(float(policy.identity_embedding.weight.grad.abs().sum()), 0)
        self.assertGreater(float(policy.rule_to_candidate.weight.grad.abs().sum()), 0)
        self.assertGreater(float(policy.rule_to_value.weight.grad.abs().sum()), 0)

    def test_sixteen_candidates_and_empty_offers_are_finite(self):
        env, policy = self.make_policy()
        for n in (0, 16):
            names = [card['name'] for card in catalog().cards if card['kind'] == 'symbol'][:n]
            obs = observed(names)
            if n == 0:
                obs['phase'] = 'ready'
            x = torch.from_numpy(featurize(obs, False, True, True)).unsqueeze(0)
            mask = np.asarray(env._env.action_mask(obs))
            action, value, log_prob = policy(x, action_masks=mask)
            self.assertTrue(np.asarray(mask)[action.item()])
            self.assertTrue(torch.isfinite(value).all())
            self.assertTrue(torch.isfinite(log_prob).all())

    def test_rule_version_mismatch_is_rejected_instead_of_silently_relabeling_ids(self):
        env = LbalGym(0, enable_management=True, use_expert_scores=False, candidate_rules=True)
        with self.assertRaisesRegex(ValueError, 'catalog differs'):
            RuleCandidatePolicy(env.observation_space, env.action_space, lambda _: 3e-4,
                                rule_schema_hash='another rule version')

    def test_upgrade_training_and_checkpoint_reload(self):
        def vector(rules):
            return DummyVecEnv([lambda: ActionMasker(LbalGym(7, max_steps=100,
                        enable_management=True, allow_item_toggle=False, use_expert_scores=False,
                        reward_mode='progressive', reward_gamma=1, candidate_rules=rules), lambda e: e.action_masks())])
        old_env, new_env = vector(False), vector(True)
        source = MaskablePPO('MlpPolicy', old_env, n_steps=16, batch_size=8, n_epochs=1,
                            policy_kwargs={'net_arch': {'pi': [256, 256], 'vf': [256, 256]},
                                           'features_extractor_class': NoExpertScoresExtractor})
        before = {k: v.clone() for k, v in source.policy.state_dict().items()}
        model = upgrade_model(source, new_env, 4)
        for key in ('policy_net.0.weight', 'value_net.2.weight'):
            torch.testing.assert_close(model.policy.mlp_extractor.state_dict()[key], source.policy.mlp_extractor.state_dict()[key])
        for key, tensor in source.policy.state_dict().items():
            torch.testing.assert_close(tensor, before[key])
        model.learn(32)
        self.assertGreater(float(model.policy.rule_to_candidate.weight.detach().abs().sum()), 0)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'rules.zip'
            model.save(path)
            loaded = MaskablePPO.load(path, env=new_env, device='cpu')
            obs = new_env.reset()
            masks = np.asarray(new_env.env_method('action_masks'))
            a, _ = model.predict(obs, action_masks=masks, deterministic=True)
            b, _ = loaded.predict(obs, action_masks=masks, deterministic=True)
            np.testing.assert_array_equal(a, b)
            self.assertEqual(loaded.policy.rule_schema_hash, catalog().schema_hash)
            loaded.learn(16, reset_num_timesteps=False)
            self.assertEqual(loaded.num_timesteps, 48)
        old_env.close()
        new_env.close()


if __name__ == '__main__':
    unittest.main()
