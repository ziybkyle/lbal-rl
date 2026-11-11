"""Features for policies trained without the hand-written expert score."""
import torch
from stable_baselines3.common.torch_layers import BaseFeaturesExtractor

from train_ppo import (
    DECK_SLOT_FEATURES, DECK_SLOTS, DECK_VEC, EXTRA_OFFER_SLOTS, ITEM_VEC,
    LEGACY_OBS_DIM, OBS_DIM, MANAGED_OBS_DIM, OFFER_FEATURES, OFFER_SLOTS, PHASES,
)


def expert_score_indices() -> list[int]:
    """Indices are documented centrally so even generic evaluators mask them."""
    first = 15 + len(PHASES) + 7 + 1 + DECK_VEC + ITEM_VEC
    extra = LEGACY_OBS_DIM + DECK_SLOTS * DECK_SLOT_FEATURES
    return ([first + i * OFFER_FEATURES + OFFER_FEATURES - 1 for i in range(OFFER_SLOTS)] +
            [extra + i * OFFER_FEATURES + OFFER_FEATURES - 1 for i in range(EXTRA_OFFER_SLOTS)])


class NoExpertScoresExtractor(BaseFeaturesExtractor):
    """Zero expert-score fields inside the saved model, including at inference."""
    def __init__(self, observation_space):
        assert observation_space.shape in ((OBS_DIM,), (MANAGED_OBS_DIM,))
        width = observation_space.shape[0]
        super().__init__(observation_space, features_dim=width)
        keep = torch.ones(width)
        keep[expert_score_indices()] = 0
        self.register_buffer("keep", keep)

    def forward(self, observations: torch.Tensor) -> torch.Tensor:
        return observations * self.keep
