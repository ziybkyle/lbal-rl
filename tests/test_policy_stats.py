"""First-offer statistics must distinguish the first offer from later ones."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from report_policy_stats import StatsCollector, is_offer_decision
from gym_env import REMOVE_BASE, offer_action


class PolicyStatsTests(unittest.TestCase):
    def test_credit_card_last_candidate_counts_as_pick_not_item_management(self):
        stats = StatsCollector()
        offers = [{'name': f'card_{index}', 'index': index} for index in range(30)]
        action = offer_action(29)
        self.assertTrue(is_offer_decision('symbol_reward', action))
        stats.note_first_offer(offers, action)
        stats.record_decision(4, offers, action)
        self.assertEqual(stats.first_pick_n['card_29'], 1)
        self.assertEqual(stats.buckets['card_29']['150']['picks'], 1)

    def test_removing_does_not_count_as_offer_decision(self):
        self.assertFalse(is_offer_decision("spin_reward", REMOVE_BASE))
        self.assertTrue(is_offer_decision("spin_reward", 3))
        self.assertTrue(is_offer_decision("spin_reward", 1))
        self.assertTrue(is_offer_decision("spin_reward", 2))
        self.assertFalse(is_offer_decision("ready", 0))

    def test_first_pick_is_only_counted_on_first_offer(self):
        stats = StatsCollector()
        offers = [{"name": "cat", "index": 0}, {"name": "dog", "index": 1}]
        stats.note_first_offer(offers, 3)
        stats.note_first_offer(offers, 4)
        self.assertEqual(stats.first_offer_n["cat"], 1)
        self.assertEqual(stats.first_offer_n["dog"], 1)
        self.assertEqual(stats.first_pick_n["cat"], 1)
        self.assertEqual(stats.first_pick_n["dog"], 0)


if __name__ == "__main__":
    unittest.main()
