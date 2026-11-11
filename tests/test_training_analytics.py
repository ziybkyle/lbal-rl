"""Analysis, evaluation registration, rolling samples and cumulative scheduling."""
import gzip
import json
from pathlib import Path
import queue
import sys
import tempfile
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'python'))
from training_analytics import aggregate_records,training_records,build_analysis,analysis_history
from evaluation_history import register_evaluation,latest_evaluations,next_evaluation_total
from training_outcomes import outcome_summary
from training_ui import TrainingTrend,TrainingApp,training_history


class AnalyticsTests(unittest.TestCase):
    def test_rolling_window_replaces_old_games_and_excludes_truncations_from_win_rate(self):
        trend=TrainingTrend()
        for _ in range(1000):trend.add(dict(rents_paid=1,won=False))
        for _ in range(500):trend.add(dict(rents_paid=9,won=True))
        point=trend.rolling_points(1000)[-1]
        self.assertEqual((point['count'],point['rate'],point['rents']),(1000,50,5))
        trend.add(dict(rents_paid=10,won=False,truncated=True))
        point=trend.rolling_points(1000)[-1]
        self.assertEqual(point['valid'],999)
        self.assertAlmostEqual(point['rate'],500/999*100)

    def test_candidate_first_selection_reroll_and_owned_are_counted_separately(self):
        choices=[dict(name='coal',kind='symbol',rarity='common',index=0),dict(name='cat',kind='symbol',rarity='common',index=1)]
        rows=[dict(won=True,rents_paid=12,owned=[choices[0]],decisions=[
            dict(action='reroll',candidates=choices,rents_paid=0),
            dict(action='take',choice='coal',candidates=choices,rents_paid=0)])]
        dataset,_,coverage=aggregate_records(rows,'test')
        coal=next(r for r in dataset['rows'] if r['name']=='coal')
        self.assertEqual((coal['runs_offered'],coal['runs_picked'],coal['first_pick_rate'],coal['acq_runs']),(1,1,0,1))
        self.assertEqual(coal['buckets']['25']['offers'],2)
        self.assertEqual(coverage['detailed_games'],1)

    def test_old_records_do_not_invent_deck_counts_or_decisions(self):
        data=outcome_summary([dict(won=True,rents_paid=12),dict(won=False,rents_paid=5,deck_len=27),
                              dict(won=False,rents_paid=0,truncated=True,deck_len=2)])
        self.assertIsNone(data['win_deck']['mean'])
        self.assertEqual(data['loss_deck']['mean'],27)
        self.assertEqual(data['deaths'],{5:1})
        dataset,_,coverage=aggregate_records([dict(won=False,rents_paid=5)],'old')
        self.assertEqual(coverage['detailed_games'],0)
        self.assertFalse(dataset['rows'])

    def test_custom_training_range_crosses_saved_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            folders=[Path(tmp)/str(i) for i in range(2)];history=[]
            for i,folder in enumerate(folders):
                folder.mkdir();data=dict(manifest=dict(previous_training_games=i*2),training_games=2)
                (folder/'training_games.jsonl').write_text(''.join(json.dumps(dict(game=j+1,seed=i*2+j))+'\n' for j in range(2)),encoding='utf8')
                with gzip.open(folder/'training_decisions.jsonl.gz','wt',encoding='utf8') as f:
                    for j in range(2):f.write(json.dumps(dict(game=j+1,decisions=[],owned=[]))+'\n')
                history.append((folder,data))
            rows=list(training_records(history,2,3))
            self.assertEqual([r['seed'] for r in rows],[1,2])
            self.assertEqual([r['absolute_game'] for r in rows],[2,3])

    def test_live_analysis_reads_flushed_games_without_a_gzip_footer_or_finished_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);live=root/'live';live.mkdir()
            (live/'manifest.json').write_text(json.dumps(dict(previous_training_games=0,previous_run=None)),encoding='utf8')
            (live/'training_games.jsonl').write_text(json.dumps(dict(game=1,seed=7,won=False,rents_paid=5))+'\n'+ '{"unfinished":',encoding='utf8')
            with gzip.open(live/'training_decisions.jsonl.gz','wt',encoding='utf8') as file:
                file.write(json.dumps(dict(game=1,decisions=[],owned=[]))+'\n');file.flush()
                history=analysis_history(root,live)
                rows=list(training_records(history,1,1))
                self.assertEqual(len(rows),1)
                self.assertEqual(rows[0]['decisions'],[])
                self.assertEqual(rows[0]['seed'],7)

    def test_manual_evaluation_is_attached_to_its_checkpoint_and_kept_as_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);model=root/'model_final.zip';model.write_bytes(b'model')
            games=root/'manual_games.jsonl';games.write_text('{}\n',encoding='utf8')
            entry=dict(label='after_continuation',games=1000,valid_games=1000,wins=12,avg_rents=5.5,seed_start=1900000000)
            register_evaluation(root,entry,games,root/'report.html','manual')
            self.assertEqual(latest_evaluations(root)['after_continuation']['result']['wins'],12)
            summary=dict(manifest=dict(training_route='scratch',expert_score_input=False,continuous_training=True,
                previous_training_games=0,train_games=2,reward_mode='fixed'),training_games=2,evaluations=[])
            (root/'summary.json').write_text(json.dumps(summary),encoding='utf8')
            (root/'training_games.jsonl').write_text(''.join(json.dumps(dict(won=False,rents_paid=3))+'\n' for _ in range(2)),encoding='utf8')
            history,warning=training_history(root)
            self.assertFalse(warning)
            self.assertEqual((history[0]['rate'],history[0]['rents']),(1.2,5.5))
            model.write_bytes(b'a changed model')
            self.assertFalse(latest_evaluations(root))

    def test_evaluation_boundary_is_cumulative_across_new_sessions(self):
        self.assertEqual(next_evaluation_total(9000),10000)
        self.assertEqual(next_evaluation_total(10000),20000)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);previous=root/'previous';out=root/'stage'
            previous.mkdir();out.mkdir()
            (previous/'summary.json').write_text(json.dumps(dict(manifest=dict(previous_training_games=8000),training_games=1000)),encoding='utf8')
            summary=dict(manifest=dict(previous_training_games=9000),training_games=1000,
                checkpoints=[dict(tag='before_continuation',updated_decisions=0),dict(tag='after_continuation',updated_decisions=512)])
            (out/'summary.json').write_text(json.dumps(summary),encoding='utf8')
            (out/'training_games.jsonl').write_text(''.join(json.dumps(dict(won=False,rents_paid=5))+'\n' for _ in range(1000)),encoding='utf8')
            stop=threading.Event();phases=[]
            def command(cmd,log,phase):
                phases.append(phase)
                if phase=='evaluation':stop.set()
                return 0
            app=SimpleNamespace(runs_dir=root,periodic_config=dict(interval=10000,games=1000),
                stop_requested=stop,thread_config_lock=threading.Lock(),requested_threads=1,
                events=queue.Queue(),stop_file=None,process=None,_run_command=command)
            TrainingApp._run_continuous(app,['--previous',str(previous)],out)
            self.assertEqual(phases,['training','report','evaluation'])


if __name__=='__main__':unittest.main()
