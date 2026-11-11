"""Numerical/gradient parity and complete credit-card candidate actions."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

import numpy as np
import torch
from sb3_contrib import MaskablePPO
from sb3_contrib.common.wrappers import ActionMasker
from stable_baselines3.common.vec_env import DummyVecEnv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'python'))
from candidate_policy import RuleCandidatePolicy, expand_rule_model
from candidate_rules import catalog, VERSION, LATEST_VERSION
from gym_env import (RustGymEnv, MAX_OFFERS, OFFER_BASE, MANAGED_ACTION_SIZE,
                     offer_action, offer_index)
from train_ppo import LbalGym, MANAGED_OBS_DIM, featurize
from test_candidate_rules import observed


def original_v1_parts(policy, observations):
    """Frozen pre-optimization calculation, also used by the local benchmark."""
    features = policy.extract_features(observations)
    start = MANAGED_OBS_DIM
    ids = observations[:, start:start + MAX_OFFERS].long()
    present = ids != 0
    start += MAX_OFFERS
    owned = observations[:, start:start + policy.rule_table.shape[0]] * 10
    details = observations[:, start + policy.rule_table.shape[0]:]
    identity = policy.identity_embedding(ids)
    rules = policy.rule_table[ids]
    reference_mask = policy.rule_references[ids]
    forward = (reference_mask * owned[:, None, :]).sum(-1)
    reverse = (policy.rule_references.T[ids] * owned[:, None, :]).sum(-1)
    duplicate = owned.gather(1, ids)
    references = torch.log1p(torch.stack((forward, reverse, duplicate), dim=-1)) / np.log(21)
    related = reference_mask @ policy.identity_embedding.weight
    related = related / reference_mask.sum(-1, keepdim=True).clamp_min(1)
    transformed = policy.identity_embedding(policy.transform_targets[ids])
    candidates = torch.cat((identity, related, transformed, rules, references), dim=-1)
    candidates = candidates * present.unsqueeze(-1)
    mean = candidates.sum(1) / present.sum(1, keepdim=True).clamp_min(1)
    maximum = candidates.masked_fill(~present.unsqueeze(-1), -1e9).max(1).values
    maximum = torch.where(present.any(1, keepdim=True), maximum, torch.zeros_like(maximum))
    held = owned @ policy.identity_embedding.weight / owned.sum(1, keepdim=True).clamp_min(1)
    context = torch.cat((mean, maximum, held, details), dim=-1)
    pi, vf = policy.mlp_extractor.policy_net, policy.mlp_extractor.value_net
    pi_first = pi[0](features) + policy.rule_to_actor(context)
    vf_first = vf[0](features) + policy.rule_to_value(context)
    latent_pi = pi[3](pi[2](pi[1](pi_first)))
    latent_vf = vf[3](vf[2](vf[1](vf_first)))
    logits = policy.action_net(latent_pi)
    scores = torch.full(ids.shape, -1e9, device=observations.device, dtype=observations.dtype)
    batch, slot = present.nonzero(as_tuple=True)
    if batch.numel():
        legacy = observations[:, policy.candidate_columns][batch, slot].clone()
        legacy[:, -1] = 0
        first = pi_first[batch] + policy.legacy_to_candidate(legacy) + policy.rule_to_candidate(candidates[batch, slot])
        hidden = pi[3](pi[2](pi[1](first)))
        scores[batch, slot] = policy.candidate_output(hidden).squeeze(-1)
    logits = torch.cat((logits[:, :OFFER_BASE], scores, logits[:, OFFER_BASE + MAX_OFFERS:]), dim=1)
    return logits, policy.value_net(latent_vf)


def make_policy(version=VERSION):
    torch.manual_seed(37)
    env = LbalGym(7, enable_management=True, use_expert_scores=False, candidate_rules=version)
    policy = RuleCandidatePolicy(env.observation_space, env.action_space, lambda _: 3e-4,
                                 net_arch={'pi': [256, 256], 'vf': [256, 256]})
    for layer in (policy.rule_to_candidate, policy.legacy_to_candidate, policy.rule_to_actor, policy.rule_to_value):
        torch.nn.init.normal_(layer.weight, std=0.02)
    policy.set_training_mode(False)
    return env, policy


def mixed_observations():
    names = [card['name'] for card in catalog().cards if card['kind'] == 'symbol']
    empty = observed(())
    empty['phase'] = 'ready'
    symbol = observed(('coal', 'toddler', 'moon'))
    symbol['deck'] += [dict(symbol['deck'][0], name='candy'), dict(symbol['deck'][0], name='rabbit')]
    return [empty, symbol, observed(names[:16]),
            observed(('time_machine', 'green_pepper', 'copycat'), 'item'),
            observed(('copycat_essence', 'time_machine_essence', 'green_pepper_essence'), 'essence')]


class CandidateEfficiencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        torch.set_num_threads(1)

    def test_optimized_probabilities_values_gradients_and_adam_match_original(self):
        env, original = make_policy()
        optimized = copy.deepcopy(original)
        observations = mixed_observations()
        x = torch.from_numpy(np.stack([featurize(obs, False, True, VERSION) for obs in observations]))
        masks = np.asarray([env._env.action_mask(obs) for obs in observations])
        actions = torch.tensor([0, 3, 18, 4, 5])
        logits_a, values_a = original_v1_parts(original, x)
        logits_b, values_b = optimized._parts(x)
        torch.testing.assert_close(logits_a, logits_b, rtol=2e-5, atol=2e-6)
        torch.testing.assert_close(values_a, values_b, rtol=2e-5, atol=2e-6)
        def terms(policy, logits, values):
            dist = policy.action_dist.proba_distribution(action_logits=logits)
            dist.apply_masking(masks)
            log_prob, entropy = dist.log_prob(actions), dist.entropy()
            loss = (values - 0.25).square().mean() - log_prob.mean() - 0.01 * entropy.mean()
            return log_prob, entropy, loss
        a, b = terms(original, logits_a, values_a), terms(optimized, logits_b, values_b)
        for lhs, rhs in zip(a, b):
            torch.testing.assert_close(lhs, rhs, rtol=2e-5, atol=2e-6)
        a[-1].backward()
        b[-1].backward()
        for (name_a, lhs), (name_b, rhs) in zip(original.named_parameters(), optimized.named_parameters()):
            self.assertEqual(name_a, name_b)
            self.assertEqual(lhs.grad is None, rhs.grad is None, name_a)
            if lhs.grad is not None:
                torch.testing.assert_close(lhs.grad, rhs.grad, rtol=2e-4, atol=2e-6, msg=name_a)
        original.optimizer.step()
        optimized.optimizer.step()
        for (name, lhs), (_, rhs) in zip(original.named_parameters(), optimized.named_parameters()):
            torch.testing.assert_close(lhs, rhs, rtol=2e-5, atol=2e-6, msg=name)

    def test_inference_skips_unused_network_but_training_retains_both(self):
        env, policy = make_policy()
        x = torch.from_numpy(featurize(observed(), False, True, VERSION)).unsqueeze(0)
        mask = np.asarray(env._env.action_mask(observed()))
        calls = []
        layers = {'actor': policy.action_net, 'critic': policy.value_net, 'candidate': policy.candidate_output}
        handles = [module.register_forward_hook(lambda m, a, b, name=name: calls.append(name))
                   for name, module in layers.items()]
        try:
            with torch.no_grad():
                policy.get_distribution(x, mask)
                self.assertCountEqual(calls, ['actor', 'candidate'])
                calls.clear()
                policy.predict_values(x)
                self.assertEqual(calls, ['critic'])
                calls.clear()
                policy.forward(x, action_masks=mask)
                self.assertCountEqual(calls, ['actor', 'candidate', 'critic'])
                calls.clear()
                policy.evaluate_actions(x, torch.tensor([3]), mask)
                self.assertCountEqual(calls, ['actor', 'candidate', 'critic'])
        finally:
            for handle in handles:
                handle.remove()
        ids_seen = []
        handle = policy.identity_embedding.register_forward_hook(lambda m, args, out: ids_seen.append(args[0].numel()))
        try:
            policy.predict_values(x)
            self.assertEqual(ids_seen, [3, 3])
            ids_seen.clear()
            policy.predict_values(torch.from_numpy(featurize(observed(()), False, True, VERSION)).unsqueeze(0))
            self.assertEqual(ids_seen, [])
        finally:
            handle.remove()

    def test_credit_card_actions_include_last_candidate_and_remain_distinct_from_management(self):
        c = catalog()
        names = [card['name'] for card in c.cards if card['kind'] == 'symbol']
        env, policy = make_policy(LATEST_VERSION)
        for count in (3, 20, 30, 47, c.max_offers):
            with self.subTest(count=count):
                obs = observed(names[:count])
                x = torch.from_numpy(featurize(obs, False, True, LATEST_VERSION)).unsqueeze(0)
                # The complete old observation is an unchanged prefix.
                old = featurize(obs, False, True, VERSION)
                np.testing.assert_array_equal(x.numpy()[0, :len(old)], old)
                mask = np.asarray(env._env.action_mask(obs))
                actions = [offer_action(i) for i in range(count)]
                self.assertTrue(all(mask[action] for action in actions))
                self.assertEqual([offer_index(action) for action in actions], list(range(count)))
                self.assertFalse(mask[128:MANAGED_ACTION_SIZE].any())
                forced = np.zeros_like(mask)
                forced[actions[-1]] = True
                action, value, log_prob = policy(x, action_masks=forced)
                self.assertEqual(action.item(), actions[-1])
                self.assertTrue(torch.isfinite(value).all() and torch.isfinite(log_prob).all())
                after = copy.deepcopy(obs)
                after['phase'], after['offer'] = 'ready', []
                after['deck'].append(dict(obs['deck'][0], name=names[count - 1]))
                rust = env._env
                backend = rust.env
                rust.env = Mock(seed=7, observation=obs)
                rust.env.step.return_value = after
                try:
                    _, _, _, _, info = rust.step(actions[-1])
                    rust.env.step.assert_called_once_with(count - 1)
                    self.assertEqual(info['step_record']['action_kind'], 'take')
                    self.assertIsNone(info['step_record']['managed_target'])
                    self.assertEqual(info['step_record']['deck_added'], [names[count - 1]])
                finally:
                    rust.env = backend
        too_many = observed(names)
        too_many['offer'].append(dict(too_many['offer'][0], index=c.max_offers))
        with self.assertRaisesRegex(ValueError, 'exceeds'):
            env._env.action_mask(too_many)
        with self.assertRaisesRegex(ValueError, 'exceeds'):
            featurize(too_many, False, True, LATEST_VERSION)

    def test_expansion_preserves_all_parameters_optimizer_and_predictions_then_can_train(self):
        def vector(version):
            return DummyVecEnv([lambda: ActionMasker(LbalGym(9, max_steps=100, enable_management=True,
                allow_item_toggle=False, use_expert_scores=False, reward_mode='progressive', reward_gamma=1,
                candidate_rules=version), lambda env: env.action_masks())])
        old_env, new_env = vector(VERSION), vector(LATEST_VERSION)
        old = MaskablePPO(RuleCandidatePolicy, old_env, seed=13, n_steps=16, batch_size=8, n_epochs=1,
                         policy_kwargs={'net_arch': {'pi': [256, 256], 'vf': [256, 256]}})
        old.learn(32)
        snapshot = copy.deepcopy(old.policy.state_dict())
        expanded = expand_rule_model(old, new_env, 15)
        self.assertEqual(expanded.num_timesteps, old.num_timesteps)
        self.assertEqual(expanded._n_updates, old._n_updates)
        for (name, lhs), (_, rhs) in zip(old.policy.named_parameters(), expanded.policy.named_parameters()):
            torch.testing.assert_close(lhs, rhs, rtol=0, atol=0, msg=name)
        a, b = old.policy.optimizer.state_dict(), expanded.policy.optimizer.state_dict()
        self.assertEqual(a['param_groups'], b['param_groups'])
        for param, state in a['state'].items():
            for key, value in state.items():
                torch.testing.assert_close(value, b['state'][param][key], rtol=0, atol=0)
        for obs in mixed_observations():
            x = torch.from_numpy(featurize(obs, False, True, VERSION)).unsqueeze(0)
            y = torch.from_numpy(featurize(obs, False, True, LATEST_VERSION)).unsqueeze(0)
            mask_a = np.asarray(old_env.envs[0].unwrapped._env.action_mask(obs))
            mask_b = np.pad(mask_a, (0, new_env.action_space.n - len(mask_a)))
            with torch.no_grad():
                dist_a = old.policy.get_distribution(x, mask_a).distribution.probs
                dist_b = expanded.policy.get_distribution(y, mask_b).distribution.probs
                torch.testing.assert_close(dist_a, dist_b[:, :MANAGED_ACTION_SIZE], rtol=2e-5, atol=2e-6)
                torch.testing.assert_close(old.policy.predict_values(x), expanded.policy.predict_values(y), rtol=2e-5, atol=2e-6)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'expanded.zip'
            expanded.save(path)
            restored = MaskablePPO.load(path, env=new_env, device='cpu')
            self.assertEqual(restored.policy.candidate_rules_version, LATEST_VERSION)
            restored.learn(16, reset_num_timesteps=False)
            self.assertEqual(restored.num_timesteps, 48)
        for name, value in old.policy.state_dict().items():
            torch.testing.assert_close(value, snapshot[name], rtol=0, atol=0)
        old_env.close()
        new_env.close()

    def test_thirty_candidate_scores_follow_identity_across_the_old_slot_boundary(self):
        env, policy = make_policy(LATEST_VERSION)
        names = [card['name'] for card in catalog().cards if card['kind'] == 'symbol'][:30]
        original = observed(names)
        x = torch.from_numpy(featurize(original, False, True, LATEST_VERSION)).unsqueeze(0)
        with torch.no_grad():
            logits, value = policy._parts(x)
            for order in (list(reversed(range(30))), list(range(17, 30)) + list(range(17))):
                reordered = observed([names[i] for i in order])
                y = torch.from_numpy(featurize(reordered, False, True, LATEST_VERSION)).unsqueeze(0)
                other, other_value = policy._parts(y)
                for slot, original_slot in enumerate(order):
                    torch.testing.assert_close(other[:, offer_action(slot)], logits[:, offer_action(original_slot)],
                                               rtol=2e-5, atol=2e-6)
                torch.testing.assert_close(other[:, :3], logits[:, :3], rtol=2e-5, atol=2e-6)
                torch.testing.assert_close(other[:, 19:640], logits[:, 19:640], rtol=2e-5, atol=2e-6)
                torch.testing.assert_close(other_value, value, rtol=2e-5, atol=2e-6)

    def test_real_rust_credit_card_rewards_can_select_the_twentieth_and_thirtieth_choices(self):
        # These full-game fixtures use the legacy expert's automatic items.
        # Stacked widths are verified with controlled Rust and mask fixtures;
        # gameplay seeds change when the underlying game rules are corrected.
        for seed, target_count in ((59, 30), (254, 20)):
            with self.subTest(seed=seed, count=target_count):
                env = LbalGym(seed, enable_management=True, use_expert_scores=False, candidate_rules=True)
                env._env.env._env.set_manual_decisions(False)
                for _ in range(600):
                    obs = env._env.observation
                    self.assertFalse(obs['done'], 'game ended before the credit-card reward')
                    offer = obs.get('offer', [])
                    if len(offer) == target_count:
                        chosen = offer[-1]['name']
                        deck_size = len(obs['deck'])
                        features, _, _, _, info = env.step(offer_action(target_count - 1))
                        self.assertEqual(features.shape, env.observation_space.shape)
                        self.assertEqual(len(env._env.observation['deck']), deck_size + 1)
                        self.assertEqual(env._env.observation['deck'][-1]['name'], chosen)
                        self.assertEqual(info['step_record']['action_kind'], 'take')
                        self.assertIsNone(info['step_record']['managed_target'])
                        break
                    wanted = 'credit_card_essence' if target_count == 30 else 'credit_card'
                    special = next((entry for entry in offer if entry['name'] == wanted), None)
                    action = (offer_action(special['index']) if special else
                              offer_action(0) if len(offer) > MAX_OFFERS else env._env.env._env.expert_action())
                    if not env.action_masks()[action]:
                        action = 0 if obs['phase'] == 'ready' else offer_action(0)
                    env.step(action)
                else:
                    self.fail('credit-card reward was not reached')
                env.close()


if __name__ == '__main__':
    unittest.main()
