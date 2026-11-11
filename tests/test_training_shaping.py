"""Rewards, provenance, UI persistence, and a real isolated PPO update."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'python'))
from training_shaping import DEFAULTS, validate, overflow_cost, TrainingShaper, freeze_snapshot


def obs(count, paid=0):
    return dict(deck=[{}]*count, rents_paid=paid)


class TrainingShapingTests(unittest.TestCase):
    def test_concave_cost_matches_requested_example_and_no_per_spin_tax(self):
        c = validate(dict(deck_enabled=True, deck_first=1, deck_cap=20))
        shaper = TrainingShaper(c)
        self.assertAlmostEqual(shaper.step(obs(20), obs(23))[0]['deck_overflow'], -1)
        self.assertAlmostEqual(shaper.step(obs(23), obs(24))[0]['deck_overflow'], -.8)
        self.assertAlmostEqual(overflow_cost(25, c), 2.44)
        self.assertEqual(shaper.step(obs(24), obs(24))[0]['deck_overflow'], 0)
        self.assertEqual(shaper.step(obs(24), obs(23))[0]['deck_overflow'], 0, 'deletion must not mint rewards')

    def test_caps_reset_decay_edges_and_nonfinite_configuration(self):
        s = TrainingShaper(dict(deck_enabled=True, deck_first=1, deck_cap=1.3))
        self.assertAlmostEqual(s.step(obs(22), obs(24))[0]['deck_overflow'], -1.3)
        self.assertEqual(s.step(obs(24), obs(25))[0]['deck_overflow'], 0)
        s.reset();self.assertEqual(s.step(obs(22), obs(23))[0]['deck_overflow'], -1)
        self.assertEqual(overflow_cost(24, validate(dict(deck_first=1, deck_decay=0))), 1)
        self.assertEqual(overflow_cost(24, validate(dict(deck_first=1, deck_decay=1))), 2)
        for key, value in [('deck_threshold',22.5),('deck_decay',1.1),('deck_first',float('nan')),
                           ('deck_threshold',-1),('deck_enabled',1),('deck_cap',float('inf'))]:
            with self.subTest(key=key), self.assertRaises(ValueError):validate({key:value})

    def test_environment_scales_all_training_points_and_evaluation_is_unchanged(self):
        from gym_env import RustGymEnv, offer_action
        c=dict(deck_enabled=True,deck_threshold=0,deck_decay=1)
        env=RustGymEnv(1234567,reward_mode='progressive',enable_management=True,offer_capacity=168,
                       training_shaping=c)
        normal=RustGymEnv(1234567,reward_mode='progressive',enable_management=True,offer_capacity=168)
        a,_=env.reset(1234567);b,_=normal.reset(1234567)
        self.assertEqual(a,b)
        for _ in range(25):
            if a['phase']=='ready':action=0
            else:action=offer_action(a['offer'][0]['index'])
            a,reward,done,truncated,info=env.step(action)
            b,base_reward,base_done,base_truncated,base_info=normal.step(action)
            self.assertEqual(a,b);self.assertEqual((done,truncated),(base_done,base_truncated))
            self.assertNotIn('training_shaping_raw',base_info)
            self.assertAlmostEqual(reward-base_reward,sum(info['training_shaping_raw'].values())/20)
            self.assertAlmostEqual(sum(info['step_record']['reward_parts'].values()),reward,places=4)
            if done or truncated:break

    def test_ui_saves_decimals_freezes_options_and_keeps_busy_controls_disabled(self):
        import training_ui as ui
        from simple_training_ui import SimpleTrainingControls
        from training_shaping_ui import ShapingDialog
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(ui,'FRESH_RUNS',Path(tmp)/'models'), \
                patch.object(ui,'PREFERENCE_SETTINGS',Path(tmp)/'prefs.json'), \
                patch.object(ui,'WINDOW_SETTINGS',Path(tmp)/'windows.json'), \
                patch.object(ui,'PROGRESS_DIRECTORY',Path(tmp)/'logs'):
            root=ui.tk.Tk();root.withdraw()
            try:
                app=ui.TrainingApp(root,route='fresh_rules');controls=app.experiments
                dialog=ShapingDialog(controls)
                dialog.variables['deck_first'].set('0.65')
                dialog.variables['deck_enabled'].set(True)
                dialog.save()
                options=controls.options();self.assertEqual(options['shaping']['deck_first'],.65)
                restored=SimpleTrainingControls(app,app.training_tab)
                self.assertEqual(restored.options()['shaping'],options['shaping'])
                with patch('training_ui.threading.Thread') as thread,patch('training_ui.choose_seeds',return_value=(1950000000,1900000000)):
                    app.start(continuous=True)
                    command,out=thread.call_args.kwargs['args']
                frozen=json.loads(command[command.index('--training-options')+1])
                self.assertEqual(frozen['shaping'],options['shaping'])
                self.assertIn('控牌22',out.name)
                self.assertTrue(controls.shaping_button.instate(['disabled']))
                self.assertTrue(controls.deck_shaping_check.instate(['disabled']))
            finally:root.destroy()

    def test_real_ppo_records_shaping_and_evaluation_does_not_use_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'tiny_training'
            options=dict(shaping=dict(deck_enabled=True,deck_threshold=8,deck_first=.5))
            command=[sys.executable,'-X','utf8',str(ROOT/'python/train_fresh_rules.py'),
                     '--out',str(out),'--games','32','--no-eval','--torch-threads','1',
                     '--train-seed-start','1951050000','--training-options',json.dumps(options)]
            process=subprocess.run(command,cwd=ROOT,capture_output=True,text=True,encoding='utf8',
                                   env=dict(os.environ,PYTHONUTF8='1'),timeout=180)
            self.assertEqual(process.returncode,0,process.stdout+'\n'+process.stderr)
            data=json.loads((out/'summary.json').read_text(encoding='utf8'))
            meta=data['manifest'];self.assertEqual(meta['training_shaping_status'],'deck_only')
            self.assertTrue((out/'training_shaping_snapshot.json').is_file())
            self.assertGreater(data['checkpoints'][-1]['updated_decisions'],0)
            rows=[json.loads(line) for line in (out/'training_games.jsonl').read_text(encoding='utf8').splitlines()]
            self.assertEqual(len(rows),32)
            self.assertTrue(any(r['training_shaping_raw']['deck_overflow']<0 for r in rows))
            self.assertTrue(all(abs(r['training_shaping_raw']['deck_overflow'])<=3 for r in rows))
            report=subprocess.run([sys.executable,'-X','utf8',str(ROOT/'python/continuous_report.py'),str(out)],
                                  cwd=ROOT,capture_output=True,text=True,encoding='utf8',timeout=30)
            self.assertEqual(report.returncode,0,report.stderr)
            html=(out/'报告.html').read_text(encoding='utf8')
            for title in ('控牌扣分（原始）',):
                self.assertIn(title,html)
            from train_from_scratch import evaluate
            with patch('training_shaping.TrainingShaper',side_effect=AssertionError('evaluation must not construct the training shaper')):
                result=evaluate(out/'model_final.zip',out,'after_continuation',range(1900000000,1900000002),
                    scratch=True,reward_mode='progressive',gamma=1,toggle_reward=0)
            self.assertEqual(result['games'],2)
            evalrows=[json.loads(line) for line in (out/'after_continuation_games.jsonl').read_text(encoding='utf8').splitlines()]
            self.assertTrue(all('training_shaping_raw' not in r for r in evalrows))
            from evaluation_history import register_evaluation
            register_evaluation(out,result,out/'after_continuation_games.jsonl',out/'report.html','test')
            resumed=Path(tmp)/'resumed'
            cmd=list(command)
            cmd[cmd.index('--out')+1]=str(resumed)
            cmd[cmd.index('--games')+1]='4'
            cmd[cmd.index('--train-seed-start')+1]='1951050200'
            cmd[cmd.index('--training-options')+1]=json.dumps(options)
            cmd.extend(['--previous',str(out)])
            process=subprocess.run(cmd,cwd=ROOT,capture_output=True,text=True,encoding='utf8',
                                   env=dict(os.environ,PYTHONUTF8='1'),timeout=180)
            self.assertEqual(process.returncode,0,process.stdout+'\n'+process.stderr)
            frozen=json.loads((resumed/'training_shaping_snapshot.json').read_text(encoding='utf8'))
            self.assertEqual(frozen['status'],'deck_only')
            self.assertEqual(frozen['config']['deck_threshold'],8)


if __name__ == '__main__':
    unittest.main()
