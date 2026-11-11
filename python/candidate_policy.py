"""Shared, permutation-equivariant candidate scoring for scratch PPO."""
from __future__ import annotations

import numpy as np
import torch
from torch import nn

from sb3_contrib.common.maskable.policies import MaskableActorCriticPolicy
from stable_baselines3.common.torch_layers import BaseFeaturesExtractor

from candidate_rules import VERSION, LATEST_VERSION, catalog, DECK_FEATURES, DECK_SLOTS
from gym_env import MAX_OFFERS, OFFER_BASE, MANAGED_ACTION_SIZE
from train_ppo import (MANAGED_OBS_DIM, OFFER_FEATURES, OFFER_SLOTS, LEGACY_OBS_DIM,
                       PHASES, DECK_VEC, ITEM_VEC, DECK_SLOT_FEATURES)
import item_state_features as item_states


def offer_indices(capacity=MAX_OFFERS):
    first = 15 + len(PHASES) + 7 + 1 + DECK_VEC + ITEM_VEC
    extra = LEGACY_OBS_DIM + DECK_SLOTS * DECK_SLOT_FEATURES
    columns = [[(first + slot * OFFER_FEATURES if slot < OFFER_SLOTS else
              extra + (slot - OFFER_SLOTS) * OFFER_FEATURES) + feature
             for feature in range(OFFER_FEATURES)] for slot in range(MAX_OFFERS)]
    extension = MANAGED_OBS_DIM + catalog().extra_dim + capacity - MAX_OFFERS
    columns += [[extension + slot * OFFER_FEATURES + feature for feature in range(OFFER_FEATURES)]
                for slot in range(capacity - MAX_OFFERS)]
    return columns


def observation_layout(observation_space):
    rules = catalog()
    for capacity in (MAX_OFFERS, rules.max_offers):
        base = MANAGED_OBS_DIM + rules.observation_extra_dim(capacity)
        if observation_space.shape == (base,):
            return capacity, None
        for version in (item_states.VERSION, item_states.LATEST_VERSION):
            if observation_space.shape == (base + item_states.dimension(version, capacity),):
                return capacity, version
    raise ValueError('Unexpected rule observation width')


def observation_capacity(observation_space):
    return observation_layout(observation_space)[0]


class RuleContextExtractor(BaseFeaturesExtractor):
    def __init__(self, observation_space):
        observation_capacity(observation_space)
        super().__init__(observation_space, features_dim=MANAGED_OBS_DIM)
        keep = torch.ones(MANAGED_OBS_DIM)
        keep[np.asarray(offer_indices()).ravel()] = 0
        self.register_buffer('keep', keep)

    def forward(self, observations):
        # All candidate positions are excluded from the global context.
        return observations[:, :MANAGED_OBS_DIM] * self.keep


class RuleCandidatePolicy(MaskableActorCriticPolicy):
    candidate_rules_version = VERSION

    def __init__(self, *args, rule_schema_hash=None, item_state_schema_hash=None, **kwargs):
        current = catalog().schema_hash
        if rule_schema_hash is not None and rule_schema_hash != current:
            raise ValueError('Game-rule catalog differs from this model; restore its rule files before continuing')
        self.rule_schema_hash = current
        space = args[0] if args else kwargs['observation_space']
        self.offer_capacity, self.item_state_version = observation_layout(space)
        item_hash = item_states.schema_hash(self.item_state_version) if self.item_state_version else None
        if item_state_schema_hash is not None and item_state_schema_hash != item_hash:
            raise ValueError('Item-state observation schema differs from this model')
        self.item_state_schema_hash = item_hash
        self.candidate_rules_version = VERSION if self.offer_capacity == MAX_OFFERS else LATEST_VERSION
        kwargs['features_extractor_class'] = RuleContextExtractor
        super().__init__(*args, **kwargs)

    def _build(self, lr_schedule):
        super()._build(lr_schedule)
        rules = catalog()
        if self.offer_capacity > MAX_OFFERS:
            expected = MANAGED_ACTION_SIZE + self.offer_capacity - MAX_OFFERS
            if self.action_space.n != expected:
                raise ValueError('Rule observation and action capacities differ')
            # Extra candidates use the existing shared scoring head. Keep
            # this layer's exact v1 shape, so its Adam state also transfers.
            self.action_net = nn.Linear(self.mlp_extractor.latent_dim_pi, MANAGED_ACTION_SIZE)
            self.init_weights(self.action_net, gain=0.01)
        self.register_buffer('rule_table', torch.from_numpy(rules.table.copy()))
        self.register_buffer('rule_references', torch.from_numpy(rules.references.copy()))
        self.register_buffer('transform_targets', torch.from_numpy(rules.transform_targets.copy()))
        self.register_buffer('candidate_columns', torch.tensor(offer_indices(self.offer_capacity), dtype=torch.long))
        self.identity_embedding = nn.Embedding(rules.count, 32, padding_idx=0)
        nn.init.normal_(self.identity_embedding.weight, std=0.05)
        with torch.no_grad():
            self.identity_embedding.weight[0].zero_()
        width = 32 * 3 + rules.table.shape[1] + 3
        # Reuse the old two-layer context network, with explicit new inputs.
        self.rule_to_candidate = nn.Linear(width, 256, bias=False)
        self.legacy_to_candidate = nn.Linear(OFFER_FEATURES, 256, bias=False)
        self.candidate_output = nn.Linear(256, 1)
        context_width = 2 * width + 32 + DECK_SLOTS * DECK_FEATURES
        self.rule_to_actor = nn.Linear(context_width, 256, bias=False)
        self.rule_to_value = nn.Linear(context_width, 256, bias=False)
        for layer in (self.rule_to_candidate, self.legacy_to_candidate, self.rule_to_actor, self.rule_to_value):
            nn.init.zeros_(layer.weight)
        nn.init.normal_(self.candidate_output.weight, std=0.01)
        nn.init.zeros_(self.candidate_output.bias)
        if self.item_state_version:
            state_width = item_states.FEATURES * 32 + len(item_states.GLOBALS)
            self.item_state_to_actor = nn.Linear(state_width, 256, bias=False)
            self.item_state_to_value = nn.Linear(state_width, 256, bias=False)
            nn.init.zeros_(self.item_state_to_actor.weight)
            nn.init.zeros_(self.item_state_to_value.weight)
        if self.item_state_version == item_states.LATEST_VERSION:
            from rule_context_features import schema, CLAUSES
            context = schema()
            self.rule_target_embedding = nn.Embedding(rules.count, 8, padding_idx=0)
            nn.init.normal_(self.rule_target_embedding.weight, std=0.05)
            with torch.no_grad():
                self.rule_target_embedding.weight[0].zero_()
            width = context['context_dim'] + 2 * CLAUSES * (8 - 1)
            self.rule_context_to_actor = nn.Linear(width, 256, bias=False)
            self.rule_context_to_value = nn.Linear(width, 256, bias=False)
            self.rule_context_to_candidate = nn.Linear(context['candidate_dim'], 256, bias=False)
            for layer in (self.rule_context_to_actor, self.rule_context_to_value, self.rule_context_to_candidate):
                nn.init.zeros_(layer.weight)
        self.optimizer = self.optimizer_class(self.parameters(), lr=lr_schedule(1), **self.optimizer_kwargs)

    def _context(self, observations):
        features = self.extract_features(observations)
        start = MANAGED_OBS_DIM
        ids = observations[:, start:start + MAX_OFFERS].long()
        start += MAX_OFFERS
        owned = observations[:, start:start + self.rule_table.shape[0]] * 10
        details_start = start + self.rule_table.shape[0]
        details = observations[:, details_start:details_start + DECK_SLOTS * DECK_FEATURES]
        if self.offer_capacity > MAX_OFFERS:
            extra_start = MANAGED_OBS_DIM + catalog().extra_dim
            ids = torch.cat((ids, observations[:, extra_start:extra_start + self.offer_capacity - MAX_OFFERS].long()), dim=1)
        # Gather only real candidates. Empty slots never enter embeddings,
        # rule lookups or reference matrix products, including during PPO.
        active_columns = (ids != 0).any(0).nonzero().flatten()
        active_ids = ids[:, active_columns]
        present = active_ids != 0
        batch, active_slot = present.nonzero(as_tuple=True)
        slot = active_columns[active_slot]
        width = self.rule_to_candidate.in_features
        pooled = observations.new_zeros((len(observations), 2, width))
        candidates = observations.new_empty((0, width))
        if batch.numel():
            valid_ids = active_ids[batch, active_slot]
            identity = self.identity_embedding(valid_ids)
            rules = self.rule_table[valid_ids]
            reference_mask = self.rule_references[valid_ids]
            candidate_owned = owned[batch]
            forward = (reference_mask * candidate_owned).sum(-1)
            reverse = (self.rule_references.T[valid_ids] * candidate_owned).sum(-1)
            duplicate = candidate_owned.gather(1, valid_ids.unsqueeze(1)).squeeze(1)
            references = torch.log1p(torch.stack((forward, reverse, duplicate), dim=-1)) / np.log(21)
            related_identities = reference_mask @ self.identity_embedding.weight
            related_identities = related_identities / reference_mask.sum(-1, keepdim=True).clamp_min(1)
            transformed_identity = self.identity_embedding(self.transform_targets[valid_ids])
            candidates = torch.cat((identity, related_identities, transformed_identity, rules, references), dim=-1)
            # Preserve torch.max's first-occurrence gradient for ties. An
            # amax scatter would silently split those gradients instead.
            padded = candidates.new_zeros((*active_ids.shape, candidates.shape[-1]))
            padded[batch, active_slot] = candidates
            pooled[:, 0] = padded.sum(1) / present.sum(1, keepdim=True).clamp_min(1)
            maximum = padded.masked_fill(~present.unsqueeze(-1), -1e9).max(1).values
            pooled[:, 1] = torch.where(present.any(1, keepdim=True), maximum, torch.zeros_like(maximum))
        held = owned @ self.identity_embedding.weight / owned.sum(1, keepdim=True).clamp_min(1)
        context = torch.cat((pooled.flatten(1), held, details), dim=-1)
        return features, context, candidates, batch, slot

    def _item_context(self, observations):
        start = MANAGED_OBS_DIM + catalog().observation_extra_dim(self.offer_capacity)
        ids = observations[:, start:start + item_states.SLOTS].long()
        offset = start + item_states.SLOTS
        states = observations[:, offset:offset + item_states.SLOTS * item_states.FEATURES]
        states = states.reshape(-1, item_states.SLOTS, item_states.FEATURES)
        batch, slot = (ids != 0).nonzero(as_tuple=True)
        pooled = observations.new_zeros((len(observations), item_states.FEATURES * 32))
        if batch.numel():
            # Bind each counter to its actual item identity, independent of
            # acquisition order; padding never enters an embedding lookup.
            identity = self.identity_embedding(ids[batch, slot])
            weighted = states[batch, slot, :, None] * identity[:, None, :]
            pooled.index_add_(0, batch, weighted.flatten(1))
            pooled = pooled / (ids != 0).sum(1, keepdim=True).clamp_min(1).sqrt()
        globals_start = offset + item_states.SLOTS * item_states.FEATURES
        return torch.cat((pooled, observations[:, globals_start:globals_start + len(item_states.GLOBALS)]), dim=1)

    def _rule_context(self, observations):
        from rule_context_features import schema, CLAUSES
        info = schema()
        start = MANAGED_OBS_DIM + catalog().observation_extra_dim(self.offer_capacity) + item_states.DIM
        raw_context = observations[:, start:start + info['context_dim']]
        targets = raw_context[:, 2 * CLAUSES:4 * CLAUSES].long()
        context = torch.cat((raw_context[:, :2 * CLAUSES], self.rule_target_embedding(targets).flatten(1),
                             raw_context[:, 4 * CLAUSES:]), dim=1)
        candidates = observations[:, start + info['context_dim']:].reshape(-1, self.offer_capacity, info['candidate_dim'])
        return context, candidates

    def _parts(self, observations, *, actor=True, critic=True):
        features, context, candidates, batch, slot = self._context(observations)
        item_context = self._item_context(observations) if self.item_state_version else None
        rule_context, rule_candidates = self._rule_context(observations) if self.item_state_version == item_states.LATEST_VERSION else (None, None)
        pi_net = self.mlp_extractor.policy_net
        vf_net = self.mlp_extractor.value_net
        logits = values = None
        if actor:
            pi_first = pi_net[0](features) + self.rule_to_actor(context)
            if item_context is not None:
                pi_first = pi_first + self.item_state_to_actor(item_context)
            if rule_context is not None:
                pi_first = pi_first + self.rule_context_to_actor(rule_context)
            latent_pi = pi_net[3](pi_net[2](pi_net[1](pi_first)))
            logits = self.action_net(latent_pi)
            scores = observations.new_full((len(observations), self.offer_capacity), -1e9)
            if batch.numel():
                legacy = observations[batch[:, None], self.candidate_columns[slot]].clone()
                legacy[:, -1] = 0  # ignore expert scores from every caller
                first = (pi_first[batch] + self.legacy_to_candidate(legacy) +
                         self.rule_to_candidate(candidates))
                if rule_candidates is not None:
                    first = first + self.rule_context_to_candidate(rule_candidates[batch, slot])
                hidden = pi_net[3](pi_net[2](pi_net[1](first)))
                scores[batch, slot] = self.candidate_output(hidden).squeeze(-1)
            logits = torch.cat((logits[:, :OFFER_BASE], scores[:, :MAX_OFFERS],
                                logits[:, OFFER_BASE + MAX_OFFERS:], scores[:, MAX_OFFERS:]), dim=1)
        if critic:
            vf_first = vf_net[0](features) + self.rule_to_value(context)
            if item_context is not None:
                vf_first = vf_first + self.item_state_to_value(item_context)
            if rule_context is not None:
                vf_first = vf_first + self.rule_context_to_value(rule_context)
            latent_vf = vf_net[3](vf_net[2](vf_net[1](vf_first)))
            values = self.value_net(latent_vf)
        return logits, values

    def _distribution(self, observations, action_masks=None, *, need_values=True):
        logits, values = self._parts(observations, critic=need_values)
        distribution = self.action_dist.proba_distribution(action_logits=logits)
        if action_masks is not None:
            distribution.apply_masking(action_masks)
        return distribution, values

    def forward(self, obs, deterministic=False, action_masks=None):
        distribution, values = self._distribution(obs, action_masks)
        actions = distribution.get_actions(deterministic=deterministic)
        return actions.reshape((-1, *self.action_space.shape)), values, distribution.log_prob(actions)

    def evaluate_actions(self, obs, actions, action_masks=None):
        distribution, values = self._distribution(obs, action_masks)
        return values, distribution.log_prob(actions), distribution.entropy()

    def get_distribution(self, obs, action_masks=None):
        return self._distribution(obs, action_masks, need_values=False)[0]

    def predict_values(self, obs):
        return self._parts(obs, actor=False)[1]


def _new_rule_model(source, env, seed):
    from sb3_contrib import MaskablePPO
    rules = catalog()
    return MaskablePPO(RuleCandidatePolicy, env, seed=seed, device='cpu',
                        n_steps=source.n_steps, batch_size=source.batch_size, n_epochs=source.n_epochs,
                        gamma=source.gamma, gae_lambda=source.gae_lambda,
                        learning_rate=source.learning_rate, ent_coef=source.ent_coef,
                        vf_coef=source.vf_coef, max_grad_norm=source.max_grad_norm,
                        clip_range=source.clip_range, clip_range_vf=source.clip_range_vf,
                        normalize_advantage=source.normalize_advantage, target_kl=source.target_kl,
                        policy_kwargs={'net_arch': {'pi': [256, 256], 'vf': [256, 256]},
                                       'rule_schema_hash': rules.schema_hash,
                                       'item_state_schema_hash': item_states.schema_hash(observation_layout(env.observation_space)[1])
                                       if observation_layout(env.observation_space)[1] else None})


def expand_rule_model(source, env, seed):
    """Append candidate/state inputs, preserving learned parameters and Adam."""
    import copy
    model = _new_rule_model(source, env, seed)
    old_version = getattr(source.policy, 'item_state_version', None)
    if old_version and not model.policy.item_state_version:
        raise ValueError('Cannot discard learned item-state inputs')
    if old_version == item_states.LATEST_VERSION and model.policy.item_state_version != old_version:
        raise ValueError('Cannot discard learned landlord/history rule inputs')
    old_parameters = dict(source.policy.named_parameters())
    new_parameters = dict(model.policy.named_parameters())
    if any(name not in new_parameters or new_parameters[name].shape != value.shape
           for name, value in old_parameters.items()):
        raise ValueError('Observation expansion must preserve every learned parameter shape')
    state = {name: value for name, value in source.policy.state_dict().items() if name != 'candidate_columns'}
    result = model.policy.load_state_dict(state, strict=False)
    allowed_missing = {'candidate_columns', 'item_state_to_actor.weight', 'item_state_to_value.weight',
                       'rule_context_to_actor.weight', 'rule_context_to_value.weight', 'rule_context_to_candidate.weight',
                       'rule_target_embedding.weight'}
    if set(result.missing_keys) - allowed_missing or result.unexpected_keys:
        raise ValueError(f'Unexpected candidate expansion state: {result}')
    old_optimizer, optimizer = source.policy.optimizer, model.policy.optimizer
    if len(old_optimizer.param_groups) != 1 or len(optimizer.param_groups) != 1:
        raise ValueError('Unexpected PPO optimizer parameter groups')
    for key, value in old_optimizer.param_groups[0].items():
        if key != 'params':
            optimizer.param_groups[0][key] = copy.deepcopy(value)
    for name, parameter in old_parameters.items():
        if parameter in old_optimizer.state:
            optimizer.state[new_parameters[name]] = copy.deepcopy(old_optimizer.state[parameter])
    model.num_timesteps = source.num_timesteps
    model._n_updates = source._n_updates
    return model


upgrade_item_state_model = expand_rule_model


def upgrade_model(source, env, seed):
    """Keep learned context/value weights; replace positional candidate scoring."""
    model = _new_rule_model(source, env, seed)
    old, new = source.policy, model.policy
    with torch.no_grad():
        new.mlp_extractor.load_state_dict(old.mlp_extractor.state_dict())
        new.action_net.load_state_dict(old.action_net.state_dict())
        new.value_net.load_state_dict(old.value_net.state_dict())
        columns = offer_indices()[:OFFER_SLOTS]
        weights = old.mlp_extractor.policy_net[0].weight
        new.legacy_to_candidate.weight.copy_(torch.stack([weights[:, c] for c in columns]).mean(0))
        new.legacy_to_candidate.weight[:, -1].zero_()
        new.candidate_output.weight.copy_(old.action_net.weight[OFFER_BASE:OFFER_BASE + OFFER_SLOTS].mean(0, keepdim=True))
        new.candidate_output.bias.copy_(old.action_net.bias[OFFER_BASE:OFFER_BASE + OFFER_SLOTS].mean().reshape(1))
    model.num_timesteps = source.num_timesteps
    model._n_updates = source._n_updates
    # The scoring architecture has changed. Adam moments are deliberately fresh
    # for this one upgrade; later continuations restore the saved optimizer.
    return model
