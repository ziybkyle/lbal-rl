"""A clean UI route and a real train/save/resume/evaluate cycle in isolation."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'python'))
from training_ui import TrainingApp, completed_runs, training_history, read_preferences, current_rules_directory, tk
from train_fresh_rules import load_ancestors, validate_seeds


def fixture_run(path, route='fresh_rules', parent=None, experiment='fresh-experiment', previous=0):
    path.mkdir(parents=True)
    meta = {'training_route': route, 'experiment_id': experiment, 'reward_mode': 'progressive',
            'expert_score_input': False, 'previous_run': str(parent) if parent else None,
            'previous_training_games': previous, 'train_games': 2, 'continuous_training': True,
            'rules_version': 'test-rules', 'simulator_sha256': 'test-hash',
            'initialization': 'random_weights', 'eval_seed_start': 1900000000, 'eval_games': 0}
    (path / 'summary.json').write_text(json.dumps({'manifest': meta, 'training_games': 2, 'evaluations': []}), encoding='utf-8')
    (path / 'model_final.zip').touch()
    (path / 'model_initial.zip').touch()
    records = [dict(seed=8000000 + previous + n, rents_paid=3+n, won=False) for n in range(2)]
    (path / 'training_games.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in records), encoding='utf-8')


class FreshRulesPanelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.preferences = self.folder / 'preferences.json'
        self.fresh_dir = self.folder / 'fresh_rules_training'
        self.patches = [patch('training_ui.PREFERENCE_SETTINGS', self.preferences),
                        patch('training_ui.RUNS', self.folder),
                        patch('training_ui.FRESH_RUNS', self.fresh_dir)]
        for item in self.patches: item.start()
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        self.root.destroy()
        for item in reversed(self.patches): item.stop()
        self.temp.cleanup()

    def test_new_route_starts_at_zero_and_constructs_a_fresh_training_command(self):
        app = TrainingApp(self.root, route='fresh_rules')
        self.assertEqual(app.selected_run(), self.fresh_dir)
        self.assertEqual(app.train_games.get(), '1000')
        self.assertFalse(hasattr(app, 'reward_select'))
        self.assertTrue(app.candidate_rules.get())
        self.assertFalse(app.history)
        self.assertIn('0 局', app.origin_caption.get())
        with patch('training_ui.choose_seeds', return_value=(8000000,1900000000)), \
             patch('training_ui.threading.Thread') as thread:
            app.start()
            command, out = thread.call_args.kwargs['args']
        self.assertEqual(Path(command[2]).name, 'train_fresh_rules.py')
        self.assertNotIn('--previous', command)
        self.assertEqual(command[command.index('--reward-mode') + 1], 'progressive')
        self.assertIn('--candidate-rules', command)
        self.assertEqual(command[command.index('--expected-rules-version')+1], app.rules_version)
        self.assertTrue(out.is_relative_to(self.fresh_dir))
        self.assertFalse(out.exists(), 'UI must not pre-create trainer output or train on opening')

    def test_old_and_new_routes_reject_each_others_models_even_with_manual_folder_selection(self):
        old = self.folder / 'original_run'
        new = self.fresh_dir / 'new_run'
        fixture_run(old, route='scratch')
        fixture_run(new)
        # A copied fresh model at the old route's top level must also be filtered.
        copied = self.folder / 'copied_new_run'
        fixture_run(copied)
        self.assertEqual([p for _,p in completed_runs(self.folder,'scratch')], [old])
        fresh = TrainingApp(self.root, route='fresh_rules')
        scratch = TrainingApp(self.root, route='scratch')
        self.assertEqual(fresh.selected_run(), new)
        with patch('training_ui.messagebox.showerror') as error:
            fresh.refresh_runs(old)
            scratch.refresh_runs(new)
        self.assertEqual(error.call_count, 2)
        self.assertNotIn(old, [p for _,p in fresh.run_options])
        self.assertNotIn(new, [p for _,p in scratch.run_options])

    def test_rule_versions_have_separate_storage_and_cannot_import_the_old_experiment(self):
        first = current_rules_directory(self.folder, 'rules-v14')
        second = current_rules_directory(self.folder, 'rules-v15')
        self.assertNotEqual(first, second)
        old = self.folder/'old_rules_experiment'
        fixture_run(old)
        app = TrainingApp(self.root, route='fresh_rules')
        with patch('training_ui.messagebox.showerror') as error:
            app.refresh_runs(old)
        error.assert_called_once()
        self.assertEqual(app.selected_run(), self.fresh_dir)
        self.assertFalse(app.history)

    def test_engine_update_while_panel_is_open_requires_reopening_before_training(self):
        app = TrainingApp(self.root, route='fresh_rules')
        with patch('training_ui.installed_rules_version', return_value='a-new-rule-version'), \
             patch('training_ui.threading.Thread') as worker, \
             patch('training_ui.messagebox.showerror') as error:
            app.start()
        worker.assert_not_called()
        self.assertIn('重新打开', error.call_args.args[1])

    def test_new_start_keeps_existing_experiments_and_uses_separate_preferences(self):
        source = self.fresh_dir / 'existing_run'
        fixture_run(source)
        fresh = TrainingApp(self.root, route='fresh_rules')
        self.assertEqual(fresh.history[-1]['total'], 2)
        fresh.select_fresh_start()
        self.assertEqual(fresh.selected_run(), self.fresh_dir)
        self.assertEqual(fresh.history, [])
        self.assertTrue((source/'model_final.zip').exists())
        fresh.train_games.set('2000'); fresh.compute_threads.set('3'); fresh.flush_preferences()
        old = TrainingApp(self.root, route='scratch')
        old.train_games.set('500'); old.compute_threads.set('1'); old.flush_preferences()
        restored = TrainingApp(self.root, route='fresh_rules')
        self.assertEqual((restored.train_games.get(),restored.compute_threads.get()),('2000','3'))
        prefs = read_preferences(self.preferences)['routes']
        self.assertEqual(prefs['scratch']['train_games'],'500')
        self.assertEqual(prefs['fresh_rules']['train_games'],'2000')

    def test_fresh_history_will_not_follow_a_foreign_parent_or_experiment(self):
        for foreign_route, foreign_id in [('scratch','same'),('fresh_rules','different')]:
            with self.subTest(route=foreign_route):
                parent = self.folder / ('parent_'+foreign_route)
                child = self.folder / ('child_'+foreign_route)
                fixture_run(parent, route=foreign_route, experiment=foreign_id)
                fixture_run(child, parent=parent, experiment='same', previous=2)
                history, warning = training_history(child)
                self.assertEqual(len(history),1)
                self.assertTrue(warning)

    def test_new_tabs_work_with_route_switching(self):
        from tkinter import ttk
        from training_ui import show_route
        notebook = ttk.Notebook(self.root)
        apps = [TrainingApp(self.root,notebook,route) for route in ('scratch','baseline','grouped','fresh_rules')]
        self.assertEqual(len(notebook.tabs()),16)
        show_route(notebook,apps,'fresh_rules')
        visible = [notebook.tab(tab,'text') for tab in notebook.tabs() if notebook.tab(tab,'state')!='hidden']
        self.assertEqual(visible,['训练','结果对比','走势图','死亡与牌组'])


class FreshRulesLineageTests(unittest.TestCase):
    def test_mismatching_engine_version_is_rejected_without_creating_a_run(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)/'not-created'
            result = subprocess.run([sys.executable,str(ROOT/'python/train_fresh_rules.py'),
                '--out',str(output),'--expected-rules-version','not-the-installed-version'],
                cwd=ROOT,capture_output=True,encoding='utf8',env=dict(os.environ,PYTHONUTF8='1'),
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.assertNotEqual(result.returncode,0)
            self.assertIn('规则与界面所选版本不一致',result.stderr)
            self.assertFalse(output.exists())

    def test_training_backend_rejects_foreign_models_versions_and_experiments(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old = root/'old'; fixture_run(old,route='scratch')
            with self.assertRaisesRegex(ValueError,'只能续训'):
                load_ancestors(old,'test-rules','test-hash')
            new = root/'new'; fixture_run(new)
            with self.assertRaisesRegex(ValueError,'规则与当前版本不同'):
                load_ancestors(new,'changed-rules','test-hash')
            with self.assertRaisesRegex(ValueError,'规则与当前版本不同'):
                load_ancestors(new,'test-rules','changed-hash')
            child = root/'child'; fixture_run(child,parent=new,experiment='another',previous=2)
            with self.assertRaisesRegex(ValueError,'另一个独立实验'):
                load_ancestors(child,'test-rules','test-hash')
            valid = load_ancestors(new,'test-rules','test-hash')
            with self.assertRaisesRegex(ValueError,'历史训练重复'):
                validate_seeds(range(8000000,8000010),range(1900000000,1900000001),valid)

    def test_real_random_initialization_resume_reports_and_evaluation_do_not_touch_other_models(self):
        import torch
        from sb3_contrib import MaskablePPO
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); first = root/'first'; second = root/'second'
            env = dict(os.environ, PYTHONUTF8='1')
            def run(*args):
                result = subprocess.run([sys.executable,*map(str,args)],cwd=ROOT,env=env,
                                        capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=90)
                self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)
            def train(out,seed,games,previous=None,no_eval=False,stop_file=None):
                command = [ROOT/'python/train_fresh_rules.py','--out',out,'--games',games,'--eval-games',1,
                           '--train-seed-start',seed,'--eval-seed-start',1900000000,'--sampling-seed',71,
                           '--torch-threads',1,'--reward-mode','progressive']
                if previous:command += ['--previous',previous]
                if no_eval:command += ['--no-eval']
                if stop_file:command += ['--stop-file',stop_file]
                run(*command)
                return json.loads((out/'summary.json').read_text(encoding='utf-8'))
            fresh = train(first,8000000,20,no_eval=True)
            self.assertEqual(fresh['manifest']['training_route'],'fresh_rules')
            self.assertTrue(fresh['manifest']['fresh_start'])
            self.assertIsNone(fresh['manifest']['previous_run'])
            self.assertEqual(fresh['manifest']['previous_training_games'],0)
            self.assertFalse(fresh['manifest']['expert_score_input'])
            self.assertEqual(fresh['evaluations'],[])
            seeds = [json.loads(line)['seed'] for line in (first/'training_games.jsonl').read_text().splitlines()]
            self.assertEqual(seeds,list(range(8000000,8000020)))
            import gzip
            with gzip.open(first/'training_decisions.jsonl.gz','rt',encoding='utf8') as details:
                detailed=[json.loads(line) for line in details]
            self.assertEqual(len(detailed),20)
            self.assertTrue(all(isinstance(row['decisions'],list) and row['owned'] for row in detailed))
            self.assertTrue(all('deck_len' in json.loads(line) for line in (first/'training_games.jsonl').read_text().splitlines()))
            self.assertFalse((first/'model_middle.zip').exists())
            run(ROOT/'python/training_analytics.py',first,'--mode','training')
            self.assertEqual(len(list((first/'analyses').rglob('分析.html'))),1)
            torch.set_num_threads(1)
            initial = MaskablePPO.load(first/'model_initial.zip',device='cpu')
            trained = MaskablePPO.load(first/'model_final.zip',device='cpu')
            self.assertEqual(initial.num_timesteps,0)
            self.assertFalse(initial.policy.optimizer.state)
            self.assertGreater(trained.num_timesteps,512)
            self.assertTrue(trained.policy.optimizer.state)
            self.assertTrue(any(not torch.equal(a,b) for a,b in zip(initial.policy.parameters(),trained.policy.parameters())))
            sha = lambda path:hashlib.sha256(path.read_bytes()).hexdigest()
            before = {file:sha(first/file) for file in ['model_initial.zip','model_final.zip','summary.json']}
            continued = train(second,8000100,2,previous=first)
            self.assertFalse(continued['manifest']['fresh_start'])
            self.assertEqual(continued['manifest']['previous_training_games'],20)
            self.assertEqual(continued['manifest']['experiment_id'],fresh['manifest']['experiment_id'])
            self.assertEqual(continued['manifest']['previous_sampled_decisions'],trained.num_timesteps)
            self.assertEqual(continued['checkpoints'][0]['path'],str(first/'model_initial.zip'))
            self.assertEqual([e['label'] for e in continued['evaluations']],['random_initial','before_continuation','after_continuation'])
            self.assertEqual(before,{file:sha(first/file) for file in before})
            stop = root/'stop'
            stop.touch()
            stopped = train(root/'stopped',8000200,2,previous=first,no_eval=True,stop_file=stop)
            self.assertEqual(stopped['training_games'],2,'a stop request must finish the current save stage')
            history,warning = training_history(second)
            self.assertFalse(warning)
            self.assertEqual([row['total'] for row in history],[20,22])
            run(ROOT/'python/continuous_report.py',first)
            run(ROOT/'python/resume_scratch_report.py',second)
            self.assertIn('新规则从零训练报告',(second/'报告.html').read_text(encoding='utf-8'))
            final_hash = sha(second/'model_final.zip')
            run(ROOT/'python/periodic_evaluate.py',second,'--games',1,'--threads',1)
            run(ROOT/'python/manual_evaluate.py',second,'--out',root/'manual','--games',1,
                '--threads',1,'--models','after_continuation')
            self.assertEqual(sha(second/'model_final.zip'),final_hash)
            self.assertEqual(before,{file:sha(first/file) for file in before})


if __name__ == '__main__':
    unittest.main()
