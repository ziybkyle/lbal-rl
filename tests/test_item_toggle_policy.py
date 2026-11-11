"""Selected toggle permissions must match in UI, learning and saved evaluations."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'python'))
from gym_env import RustGymEnv, MANAGED_SLOTS, REMOVE_BASE, TOGGLE_BASE, USE_BASE
from item_toggle_policy import (ITEM_TOGGLES, ESSENCE_TOGGLES, DEFAULT_ALLOWLIST,
                                normalize_allowlist, toggle_policy_metadata, saved_toggle_policy)
from train_ppo import LbalGym
from training_ui import TrainingApp, display_history, training_history, tk


class ItemTogglePolicyTests(unittest.TestCase):
    def test_all_54_names_exist_in_their_actual_game_catalog(self):
        self.assertEqual((len(ITEM_TOGGLES), len(ESSENCE_TOGGLES), len(DEFAULT_ALLOWLIST)), (26, 28, 54))
        for entries, filename, kind in ((ITEM_TOGGLES, 'Items - JSON.json', 'item'),
                                         (ESSENCE_TOGGLES, 'Essences - JSON.json', 'essence')):
            definitions = json.loads((ROOT / 'game/JSON' / filename).read_text(encoding='utf-8'))
            self.assertEqual(len(entries), len({key for key, _ in entries}))
            self.assertFalse({name for name, _ in entries} - definitions.keys())
            self.assertTrue(all((kind, name) in DEFAULT_ALLOWLIST for name, _ in entries))
        self.assertIn(('essence', 'checkered_flag_essence'), DEFAULT_ALLOWLIST)
        self.assertNotIn(('item', 'checkered_flag'), DEFAULT_ALLOWLIST)
        self.assertIn(('essence', 'coffee_essence'), DEFAULT_ALLOWLIST)
        self.assertNotIn(('item', 'coffee'), DEFAULT_ALLOWLIST)

    def test_mask_checks_every_catalog_entry_and_keeps_use_independent(self):
        env = RustGymEnv(0, enable_management=True, limited_item_toggles=True)
        base, _ = env.reset()
        entries = []
        for kind, filename in (('item', 'Items - JSON.json'), ('essence', 'Essences - JSON.json')):
            definitions = json.loads((ROOT / 'game/JSON' / filename).read_text(encoding='utf-8'))
            entries.extend(dict(kind=kind, name=name, can_toggle=True, can_use=True) for name in definitions)
        entries.extend([dict(kind='item', name='coffee_essence', can_toggle=True, can_use=False),
                        dict(kind='symbol', name='egg_carton', can_toggle=True, can_use=False),
                        dict(kind='item', name='unknown', can_toggle=True, can_use=False)])
        for start in range(0, len(entries), MANAGED_SLOTS):
            owned = entries[start:start + MANAGED_SLOTS]
            obs = dict(base, managed_items=owned)
            mask = env.action_mask(obs)
            for index, entry in enumerate(owned):
                self.assertEqual(mask[TOGGLE_BASE + index], (entry['kind'], entry['name']) in DEFAULT_ALLOWLIST, entry)
                self.assertEqual(mask[USE_BASE + index], entry['can_use'])
        obs = dict(base, managed_items=[dict(kind='item', name='egg_carton', can_toggle=False, can_use=True)])
        self.assertFalse(env.action_mask(obs)[TOGGLE_BASE], 'list permission must not override game eligibility')
        env.allow_item_toggle = False
        obs['managed_items'][0]['can_toggle'] = True
        self.assertFalse(env.action_mask(obs)[TOGGLE_BASE], 'master permission overrides the list')
        self.assertTrue(env.action_mask(obs)[USE_BASE])
        env.allow_item_toggle = True
        obs['phase'] = 'spin_reward'
        self.assertFalse(any(env.action_mask(obs)[TOGGLE_BASE:USE_BASE]))

    def test_use_actions_for_emergency_items_and_pillows_are_not_blocked(self):
        for kind, name in [('item', 'coffee'), ('item', 'devils_deal'), ('item', 'comfy_pillow'),
                           ('essence', 'comfy_pillow_essence')]:
            with self.subTest(name=name):
                env = RustGymEnv(0, enable_management=True, limited_item_toggles=True)
                obs = copy.deepcopy(env.observation)
                obs['managed_items'] = [dict(kind=kind, name=name, type_id=7, count=1,
                                            disabled=False, can_toggle=True, can_use=True)]
                fake = Mock(observation=obs, seed=0)
                env.env = fake
                self.assertFalse(env.action_mask()[TOGGLE_BASE])
                self.assertTrue(env.action_mask()[USE_BASE])
                env.step(USE_BASE)
                fake.use_managed.assert_called_once_with(kind == 'essence', 7)

    def test_rerolls_removals_and_old_unrestricted_actions_are_unchanged(self):
        limited = RustGymEnv(0, enable_management=True, limited_item_toggles=True)
        normal = RustGymEnv(0, enable_management=True)
        obs = copy.deepcopy(limited.observation)
        obs.update(phase='spin_reward', can_reroll=True, can_remove=True,
                   managed_items=[dict(kind='item', name='birdhouse', can_toggle=True, can_use=False)])
        self.assertEqual(limited.action_mask(obs), normal.action_mask(obs))
        self.assertTrue(limited.action_mask(obs)[2])
        self.assertTrue(limited.action_mask(obs)[REMOVE_BASE])
        obs['phase'] = 'ready'
        self.assertTrue(normal.action_mask(obs)[TOGGLE_BASE])
        self.assertFalse(limited.action_mask(obs)[TOGGLE_BASE])

    def test_real_rust_toggle_rejects_nonlisted_item_and_keeps_same_spin_lock(self):
        env = RustGymEnv(0, reward_mode='progressive', reward_gamma=1, enable_management=True,
                         limited_item_toggles=True, toggle_reward=-0.05)
        obs, _ = env.reset()
        for _ in range(100):
            if obs['phase'] == 'ready' and any(x['name'] == 'swear_jar' for x in obs['managed_items']):
                break
            self.assertFalse(obs['done'])
            obs, *_ = env.step(0 if obs['phase'] == 'ready' else 3)
        owned = obs['managed_items']
        bird = next(i for i, item in enumerate(owned) if item['name'] == 'birdhouse')
        jar = next(i for i, item in enumerate(owned) if item['name'] == 'swear_jar')
        self.assertTrue(owned[bird]['can_toggle'])
        with self.assertRaises(ValueError):
            env.step(TOGGLE_BASE + bird)
        _, reward, _, _, info = env.step(TOGGLE_BASE + jar)
        self.assertAlmostEqual(reward, -0.05 / 20)
        self.assertEqual(info['step_record']['managed_target']['name'], 'swear_jar')
        self.assertFalse(env.action_mask()[TOGGLE_BASE + jar])
        with self.assertRaises(ValueError):
            env.step(TOGGLE_BASE + jar)

    def test_saved_snapshots_round_trip_and_empty_list_does_not_become_default(self):
        meta = toggle_policy_metadata(True)
        self.assertEqual(normalize_allowlist(meta['item_toggle_allowlist']), DEFAULT_ALLOWLIST)
        self.assertEqual(saved_toggle_policy({}), {'limited_item_toggles': False, 'item_toggle_allowlist': None})
        snapshot = [('item', 'egg_carton')]
        env = LbalGym(0, enable_management=True, limited_item_toggles=True, item_toggle_allowlist=snapshot)
        self.assertEqual(env._env.item_toggle_allowlist, frozenset(snapshot))
        snapshot.clear()
        self.assertEqual(env._env.item_toggle_allowlist, frozenset([('item', 'egg_carton')]))
        self.assertFalse(normalize_allowlist([]))
        for invalid in ('egg_carton', [['symbol', 'egg_carton']], [['item']], [['item', 1]]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                normalize_allowlist(invalid)


class TogglePanelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.patches = [patch('training_ui.PREFERENCE_SETTINGS', self.folder/'prefs.json'),
                        patch('training_ui.RUNS', self.folder/'scratch'),
                        patch('training_ui.BASELINE_RUNS', self.folder/'baseline'),
                        patch('training_ui.GROUPED_RUNS', self.folder/'grouped'),
                        patch('training_ui.FRESH_RUNS', self.folder/'fresh'),
                        patch('training_ui.completed_runs', side_effect=lambda *_: [])]
        for item in self.patches: item.start()
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        self.root.destroy()
        for item in reversed(self.patches): item.stop()
        self.temp.cleanup()

    def test_checkbox_sync_preferences_and_list_are_independent_per_route(self):
        app = TrainingApp(self.root, route='fresh_rules')
        app.allow_item_toggle.set(False)
        app.limited_toggle_permission.invoke()
        self.assertTrue(app.allow_item_toggle.get())
        self.assertTrue(app.limited_item_toggles.get())
        app.flush_preferences()
        restored = TrainingApp(self.root, route='fresh_rules')
        self.assertTrue(restored.limited_item_toggles.get())
        baseline = TrainingApp(self.root, route='baseline')
        self.assertFalse(baseline.limited_item_toggles.get())
        app.toggle_permission.invoke()
        self.assertFalse(app.allow_item_toggle.get())
        self.assertFalse(app.limited_item_toggles.get())
        app.show_toggle_list()
        boxes = []
        def visit(widget):
            if isinstance(widget, tk.Listbox): boxes.append(widget)
            for child in widget.winfo_children(): visit(child)
        visit(app.toggle_list_window)
        self.assertEqual([box.size() for box in boxes], [26, 28])
        self.assertIn('格子旗精华', boxes[1].get(0, 'end'))
        app.set_busy(True)
        self.assertIn('disabled', app.limited_toggle_permission.state())

    def test_all_routes_forward_the_restriction_to_the_training_process(self):
        from training_ui import BASELINE_TRAINER, GROUPED_TRAINER
        optional = {'baseline': BASELINE_TRAINER, 'grouped': GROUPED_TRAINER}
        for route in ('scratch', 'baseline', 'grouped', 'fresh_rules'):
            if route in optional and not optional[route].is_file():
                continue
            with self.subTest(route=route):
                app = TrainingApp(self.root, route=route)
                app.limited_item_toggles.set(True)
                with patch.object(app, 'selected_run', return_value=app.runs_dir), \
                     patch('training_ui.choose_seeds', return_value=(8000000, 1900000000)), \
                     patch('training_ui.threading.Thread') as thread:
                    app.start()
                command = thread.call_args.kwargs['args'][0]
                self.assertIn('--limited-item-toggles', command)
                self.assertNotIn('--no-toggle-actions', command)

    def test_loading_models_restores_old_or_selected_permissions_and_history_labels(self):
        directory = self.folder/'run'
        directory.mkdir()
        (directory/'training_games.jsonl').write_text(json.dumps(dict(won=False, rents_paid=3))+'\n')
        meta = {'continuous_training': True, 'allow_item_toggle': True, **toggle_policy_metadata(True)}
        def save():
            (directory/'summary.json').write_text(json.dumps(dict(training_games=1, manifest=meta, evaluations=[])), encoding='utf-8')
        save()
        app = TrainingApp(self.root, route='baseline')
        with patch.object(app, 'selected_run', return_value=directory):
            app.load_selected_reward()
            app.refresh_comparison()
            self.assertTrue(app.limited_item_toggles.get())
            self.assertEqual(app.history_table.set('0', 'management'), '指定清单')
            meta.pop('limited_item_toggles')
            save()
            app.load_selected_reward()
            self.assertFalse(app.limited_item_toggles.get())
            self.assertTrue(app.allow_item_toggle.get())

    def test_grouped_history_does_not_hide_a_change_in_toggle_permissions(self):
        stats = dict(games=2, wins=0, valid=2, truncated=0, average_rents=3)
        history = [dict(directory=Path(str(i)), total=2*i+2, round_games=2,
                        continuous=True, training=stats, threads=1, reward_mode='fixed', toggle_reward=0,
                        allow_item_toggle=True, limited_item_toggles=bool(i),
                        training_seconds=1, elapsed_seconds=1, games=0) for i in range(2)]
        combined = display_history(history, group_size=4)
        self.assertIn('limited_item_toggles', combined[0]['mixed_fields'])


class ToggleEvaluationTests(unittest.TestCase):
    def test_manual_and_periodic_evaluation_use_saved_permissions_not_the_current_list(self):
        import manual_evaluate
        import periodic_evaluate
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); directory = root/'model'; directory.mkdir()
            initial = directory/'model_initial.zip'; initial.touch()
            final = directory/'model_final.zip'; final.touch()
            snapshot = [['item', 'egg_carton']]
            meta = dict(reward_mode='progressive', allow_item_toggle=True, previous_training_games=0,
                        **toggle_policy_metadata(True, snapshot))
            summary = dict(manifest=meta, training_games=2, checkpoints=[
                dict(tag='random_initial', path=str(initial)), dict(tag='before_continuation', path=str(initial))])
            (directory/'summary.json').write_text(json.dumps(summary), encoding='utf-8')
            def evaluate(model_path, out, label, seeds, **kwargs):
                self.assertTrue(kwargs['limited_item_toggles'])
                self.assertEqual(kwargs['item_toggle_allowlist'], snapshot)
                row = dict(seed=seeds.start, won=False, rents_paid=2, actions={})
                (out/f'{label}_games.jsonl').write_text(json.dumps(row)+'\n')
                return dict(label=label, games=1, valid_games=1, wins=0, win_rate=0., avg_rents=2, truncated_games=0)
            with patch.object(periodic_evaluate, 'evaluate', side_effect=evaluate) as periodic, \
                 patch.object(periodic_evaluate.torch, 'set_num_threads'), \
                 patch('sys.argv', ['periodic_evaluate', str(directory), '--games', '1']):
                periodic_evaluate.main()
            self.assertEqual(periodic.call_count, 1)
            with patch.object(manual_evaluate, 'evaluate', side_effect=evaluate) as manual, \
                 patch.object(manual_evaluate.torch, 'set_num_threads'), \
                 patch('sys.argv', ['manual_evaluate', str(directory), '--out', str(root/'manual'), '--games', '1',
                                    '--models', 'random_initial', 'before_continuation', 'after_continuation']):
                manual_evaluate.main()
            self.assertEqual(manual.call_count, 3)

    def test_real_training_resume_and_evaluation_keep_the_selected_list_and_existing_models(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); first = root/'first'; second = root/'second'
            def run(*args):
                result = subprocess.run([sys.executable, *map(str, args)], cwd=ROOT,
                                        env=dict(os.environ, PYTHONUTF8='1'), capture_output=True,
                                        text=True, encoding='utf-8', errors='replace', timeout=90)
                self.assertEqual(result.returncode, 0, result.stdout+'\n'+result.stderr)
            def train(out, seed, previous=None):
                command = [ROOT/'python/train_fresh_rules.py', '--out', out, '--games', 24 if previous is None else 2,
                           '--eval-games', 1, '--train-seed-start', seed, '--eval-seed-start', 1900000000,
                           '--sampling-seed', 71, '--torch-threads', 1, '--limited-item-toggles']
                if previous: command += ['--previous', previous]
                run(*command)
                data = json.loads((out/'summary.json').read_text(encoding='utf-8'))
                self.assertEqual(normalize_allowlist(data['manifest']['item_toggle_allowlist']), DEFAULT_ALLOWLIST)
                self.assertTrue(data['manifest']['limited_item_toggles'])
                self.assertTrue(all(ev['limited_item_toggles'] for ev in data['evaluations']))
                for log in out.glob('*_games.jsonl'):
                    for line in log.read_text(encoding='utf-8').splitlines():
                        game = json.loads(line)
                        for decision in game.get('decisions', []):
                            if decision['action'] == 'toggle_item':
                                target = decision['managed_target']
                                self.assertIn((target['kind'], target['name']), DEFAULT_ALLOWLIST)
                return data
            data = train(first, 8100000)
            self.assertGreater(data['checkpoints'][-1]['updated_decisions'], 0)
            sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
            before = {file: sha(first/file) for file in ['model_initial.zip', 'model_final.zip', 'summary.json']}
            continued = train(second, 8100100, first)
            self.assertEqual(continued['manifest']['previous_training_games'], 24)
            self.assertEqual(continued['manifest']['experiment_id'], data['manifest']['experiment_id'])
            self.assertEqual(before, {file: sha(first/file) for file in before})
            history, warning = training_history(second)
            self.assertFalse(warning)
            self.assertTrue(all(row['limited_item_toggles'] for row in history))
            run(ROOT/'python/resume_scratch_report.py', second)
            run(ROOT/'python/continuous_report.py', first)
            self.assertIn('指定清单', (second/'报告.html').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
