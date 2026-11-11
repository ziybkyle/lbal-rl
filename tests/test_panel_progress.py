"""Progress persistence and rent-boundary accounting, without production training."""
import json
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'python'))
from candidate_stages import decision_ordinal, stage_label, staged_decisions
from training_progress import ProgressLog


class PanelProgressTests(unittest.TestCase):
    def test_history_keeps_last_500_lines_across_restart_without_mixing_branches(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = ProgressLog(tmp, 'fresh_rules', Path(tmp) / 'A')
            log.append('\n'.join(f'line {i}' for i in range(620)))
            loaded = ProgressLog(tmp, 'fresh_rules', Path(tmp) / 'A')
            self.assertEqual(len(loaded.lines), 500)
            self.assertEqual((loaded.lines[0], loaded.lines[-1]), ('line 120', 'line 619'))
            self.assertFalse(ProgressLog(tmp, 'fresh_rules', Path(tmp) / 'B').lines)
            self.assertFalse(ProgressLog(tmp, 'baseline', Path(tmp) / 'A').lines)
            log.path.write_text('{broken', encoding='utf8')
            self.assertFalse(ProgressLog(tmp, 'fresh_rules', Path(tmp) / 'A').lines)

    def test_rent_rewards_and_all_rerolls_stay_in_earned_interval(self):
        for paid in (1, 2, 12, 13):
            for phase in ('symbol_reward', 'item_reward', 'essence_reward'):
                for action in ('take', 'reroll', 'skip'):
                    self.assertEqual(decision_ordinal(dict(rents_paid=paid, phase=phase, action=action)), paid)
            self.assertEqual(decision_ordinal(dict(rents_paid=paid, phase='spin_reward')), paid + 1)
        self.assertEqual([stage_label(i) for i in (1, 2, 3, 13)],
                         ['交租 25 前', '交租 25～50', '交租 50～100', '交租 777～1000'])
        decisions = [dict(phase=p, spin=s, rents_paid=1) for p, s in [
            ('essence_reward', 5), ('essence_reward', 5), ('symbol_reward', 5),
            ('symbol_reward', 5), ('spin_reward', 5), ('item_reward', 5),
            ('ready', 5), ('spin_reward', 5), ('spin_reward', 6), ('item_reward', 6)]]
        self.assertEqual([o for _, o in staged_decisions(dict(decisions=decisions))],
                         [1, 1, 1, 1, 1, 1, 2, 2, 2, 2])

    def test_ui_restores_history_and_formats_eval_speed_win_rate(self):
        import training_ui as ui
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(ui, 'PROGRESS_DIRECTORY', Path(tmp) / 'progress'), \
                patch.object(ui, 'PREFERENCE_SETTINGS', Path(tmp) / 'preferences.json'), \
                patch.object(ui, 'WINDOW_SETTINGS', Path(tmp) / 'windows.json'), \
                patch.object(ui, 'completed_runs', return_value=[]):
            root = ui.tk.Tk()
            root.withdraw()
            try:
                app = ui.TrainingApp(root)
                app.append('\n'.join(f'record {i}' for i in range(600)))
                self.assertEqual(len(app.log.get('1.0', 'end').strip().splitlines()), 500)
                app.restore_progress_log()
                self.assertIn('record 599', app.log.get('1.0', 'end'))
                app.started_at = time.monotonic()
                app._progress_line('eval periodic_eval: 250/1000; avg_rents=7.25; wins=8; valid_games=249; games_per_min=120.5')
                text = app.log.get('1.0', 'end').splitlines()[-2:]
                self.assertIn('3.21%（8/249）', text[0])
                self.assertIn('120.5 局/分钟', text[0])
                self.assertIn('续训后模型（定期）', text[0])
                self.assertIn('7.250', text[0])
            finally:
                root.destroy()

    def test_evaluator_reports_valid_denominator_and_speed_without_training(self):
        import train_from_scratch as trainer
        model = SimpleNamespace(policy=SimpleNamespace(), predict=lambda *a, **k: (0, None))
        class FakeEnv:
            def __init__(self, seed, **kwargs):
                self.seed = seed
            def reset(self, seed):
                return dict(phase='ready', total_spins=0, rents_paid=0), {}
            def action_mask(self):
                return [True]
            def step(self, action):
                obs = dict(won=self.seed == 0, rents_paid=3, total_spins=2, rules_version='test')
                return obs, 0, self.seed != 2, self.seed == 2, dict(step_record=dict(action_kind='spin'))
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(trainer.MaskablePPO, 'load', return_value=model), \
                patch.object(trainer, 'RustGymEnv', FakeEnv), \
                patch.object(trainer, 'features_for_model', return_value=[0]), \
                contextlib.redirect_stdout(output):
            result = trainer.evaluate(Path(tmp)/'unused.zip', Path(tmp), 'after_continuation',
                                      range(3), scratch=False, reward_mode='progressive', gamma=1)
        self.assertEqual((result['wins'], result['valid_games'], result['truncated_games']), (1, 2, 1))
        self.assertIn('wins=1; valid_games=2; games_per_min=', output.getvalue())


if __name__ == '__main__':
    unittest.main()
