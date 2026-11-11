"""Past assessments stay separate from training windows and other branches."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'python'))

from evaluation_history import evaluation_sources, register_evaluation
from training_analytics import lineage
from training_outcomes import evaluation_window_pair, outcome_summary, OutcomePanel


class EvaluationHistoryTests(unittest.TestCase):
    def make_run(self, root, name, total, previous=None):
        directory=root/name;directory.mkdir()
        data=dict(manifest=dict(training_route='fresh',experiment_id='one',
                               previous_training_games=total-1000,
                               previous_run=str(previous) if previous else None),
                  training_games=1000,evaluations=[])
        (directory/'summary.json').write_text(json.dumps(data),encoding='utf8')
        (directory/'model_final.zip').write_bytes(b'checkpoint')
        return directory

    def assessment(self, directory, name, date, rents, label='after_continuation'):
        path=directory/f'{name}.jsonl'
        rows=[dict(game=i+1,rents_paid=r,won=False,deck_len=20+r) for i,r in enumerate(rents)]
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf8')
        result=dict(label=label,games=len(rows),avg_rents=sum(rents)/len(rents))
        register_evaluation(directory,result,path,directory/f'{name}.html','单独评估',created_at=date)
        return path

    def test_keeps_each_final_assessment_and_ignores_initial_and_sibling(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            first=self.make_run(root,'first',1000)
            second=self.make_run(root,'second',2000,first)
            sibling=self.make_run(root,'other_branch',2000,first)
            self.assessment(first,'old','2026-11-10T01:00:00Z',[2]*4)
            self.assessment(second,'manual','2026-11-10T04:00:00Z',[5]*4)
            self.assessment(second,'repeat','2026-11-10T05:00:00Z',[6]*4)
            self.assessment(second,'initial','2026-11-10T06:00:00Z',[1]*4,label='random_initial')
            self.assessment(sibling,'foreign','2026-11-10T07:00:00Z',[10]*4)
            entries=evaluation_sources(lineage(second))
            self.assertEqual([Path(e['games_path']).stem for e in entries],['repeat','manual','old'])
            rows,prior,current,previous=evaluation_window_pair(entries,count=3)
            self.assertEqual([r['rents_paid'] for r in rows],[6]*3)
            self.assertEqual([r['rents_paid'] for r in prior],[5]*3)
            rows,prior,current,previous=evaluation_window_pair(entries,entries[1]['key'],3)
            self.assertEqual([r['rents_paid'] for r in prior],[2]*3)
            self.assertEqual((current['total'],previous['total']),(2000,1000))

    def test_legacy_periodic_and_summary_report_deduplicate_registered_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=self.make_run(Path(tmp),'run',1000)
            path=self.assessment(directory,'periodic_eval_games','2026-11-10T02:00:00Z',[4]*3,label='periodic_eval')
            (directory/'periodic_eval.json').write_text(json.dumps(dict(label='periodic_eval',games=3)),encoding='utf8')
            summary=json.loads((directory/'summary.json').read_text(encoding='utf8'))
            summary['evaluations']=[dict(label='after_continuation',games=2)]
            (directory/'summary.json').write_text(json.dumps(summary),encoding='utf8')
            (directory/'after_continuation_games.jsonl').write_text('{}\n{}\n',encoding='utf8')
            entries=evaluation_sources(lineage(directory))
            self.assertEqual(len(entries),2)
            self.assertEqual(sum(e['games_path']==str(path.resolve()) for e in entries),1)
            (directory/'evaluation_history.json').unlink()
            entries=evaluation_sources(lineage(directory))
            self.assertEqual(len(entries),2)

    def test_previous_assessment_is_not_same_report_and_does_not_fake_missing_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=self.make_run(Path(tmp),'run',1000)
            self.assessment(directory,'short','2026-11-10T01:00:00Z',[2,3])
            self.assessment(directory,'current','2026-11-10T02:00:00Z',[4]*6)
            entries=evaluation_sources(lineage(directory))
            rows,prior,_,_=evaluation_window_pair(entries,count=4)
            self.assertEqual((len(rows),len(prior)),(4,2))
            rows,prior,_,previous=evaluation_window_pair(entries,entries[-1]['key'],4)
            self.assertEqual((len(rows),len(prior)),(2,0))
            self.assertIsNone(previous)
            self.assertEqual(outcome_summary(prior)['games'],0)

    def test_changed_checkpoint_cannot_reappear_through_legacy_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=self.make_run(Path(tmp),'run',1000)
            self.assessment(directory,'periodic_eval_games','2026-11-10T01:00:00Z',[2]*3,label='periodic_eval')
            (directory/'periodic_eval.json').write_text(json.dumps(dict(label='periodic_eval',games=3)),encoding='utf8')
            (directory/'model_final.zip').write_bytes(b'a replaced checkpoint')
            self.assertEqual(evaluation_sources(lineage(directory)),[])

    def test_panel_switches_to_past_assessment_and_compares_previous_run(self):
        from types import SimpleNamespace
        import tkinter as tk
        from tkinter import ttk
        with tempfile.TemporaryDirectory() as tmp:
            directory=self.make_run(Path(tmp),'run',1000)
            for i in range(3):
                self.assessment(directory,f'eval{i}',f'2026-11-10T0{i}:00:00Z',[i+2]*5)
            root=tk.Tk();root.withdraw()
            try:
                app=SimpleNamespace(root=root,selected_run=lambda:directory,training_active=False,
                                    current_dir=None,open_analysis=lambda mode:None)
                panel=OutcomePanel(app,ttk.Frame(root))
                panel.source.set('模型评估（含历史）');panel.refresh()
                self.assertEqual(panel.rows[0]['rents_paid'],4)
                self.assertEqual(panel.previous[0]['rents_paid'],3)
                panel.evaluation_select.current(1);panel.select_evaluation()
                self.assertEqual(panel.rows[0]['rents_paid'],3)
                self.assertEqual(panel.previous[0]['rents_paid'],2)
                self.assertIn('上一次独立评估',panel.table.heading('previous')['text'])
            finally:
                root.destroy()


if __name__=='__main__':
    unittest.main()
