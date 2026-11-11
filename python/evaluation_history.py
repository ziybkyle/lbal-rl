"""Persist evaluations beside their immutable training checkpoint."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path


def _evaluation_time(value):
    """Use one comparable UTC timestamp for old and current saved reports."""
    if isinstance(value, (int, float)):
        return float(value)
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.replace(tzinfo=timezone.utc).timestamp() if parsed.tzinfo is None else parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def evaluation_sources(history):
    """List completed final-model assessments on this exact ancestral branch.

    A manual and periodic assessment of the same checkpoint are separate runs.
    Initial/before-training assessments belong to the comparison page instead.
    Old reports without a registry remain readable without inventing metadata.
    """
    found = []
    seen = set()
    rejected = set()
    expected = None
    for directory, summary in history:
        directory = Path(directory).resolve()
        meta = summary.get('manifest', {})
        identity = (meta.get('training_route'), meta.get('experiment_id'))
        if expected is None:
            expected = identity
        if identity != expected:
            raise ValueError('不能混合不同训练路线或实验的评估。')
        total = int(meta.get('previous_training_games', 0)) + int(summary.get('training_games', 0))
        model = directory / 'model_final.zip'

        def add(entry, *, registered=False):
            result = entry.get('result') or {}
            tag = entry.get('tag', result.get('label'))
            if tag not in ('after_continuation', 'final', 'periodic_eval'):
                return
            raw_path = entry.get('games_path')
            if not raw_path:
                return
            path = Path(raw_path).resolve()
            key = str(path).casefold()
            if key in seen or key in rejected or not path.is_file():
                return
            # A completed report announces its actual sample count. Empty or
            # unfinished reports must not appear as the previous assessment.
            games = int(result.get('games', 0))
            if games <= 0:
                return
            if registered:
                checkpoint = Path(entry.get('checkpoint') or model).resolve()
                if checkpoint != model or not checkpoint.is_file():
                    rejected.add(key)
                    return
                stat = checkpoint.stat()
                if ('model_size' in entry and stat.st_size != entry['model_size']) or (
                        'model_mtime_ns' in entry and stat.st_mtime_ns != entry['model_mtime_ns']):
                    rejected.add(key)
                    return
            created_at = entry.get('created_at')
            if not created_at:
                created_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
            source = entry.get('source') or ('定期评估' if tag == 'periodic_eval' else '训练后评估')
            seen.add(key)
            found.append(dict(entry, result=result, tag='after_continuation', source=source,
                              games_path=str(path), directory=str(directory), total=total,
                              games=games, created_at=created_at, key=key,
                              report_path=entry.get('report_path') or str(directory/'报告.html')))

        registry = directory / 'evaluation_history.json'
        if registry.is_file():
            try:
                entries = json.loads(registry.read_text(encoding='utf8'))
            except (OSError, ValueError):
                entries = []
            for entry in entries if isinstance(entries, list) else []:
                try:
                    add(entry, registered=True)
                except (OSError, ValueError, TypeError, KeyError):
                    # One damaged old entry must not hide other saved reports.
                    continue
        periodic = directory / 'periodic_eval.json'
        if periodic.is_file():
            try:
                add(dict(result=json.loads(periodic.read_text(encoding='utf8')), tag='periodic_eval',
                         games_path=str(directory/'periodic_eval_games.jsonl'),
                         report_path=str(directory/'定期评估报告.html'), source='定期评估'))
            except (OSError, ValueError, TypeError):
                pass
        for result in summary.get('evaluations', []):
            if result.get('label') in ('after_continuation', 'final'):
                try:
                    add(dict(result=result, games_path=str(directory/f'{result["label"]}_games.jsonl'),
                             source='训练后评估'))
                except (OSError, ValueError, TypeError):
                    continue
    return sorted(found, key=lambda entry: (_evaluation_time(entry['created_at']), entry['key']), reverse=True)


def register_evaluation(directory, result, games_path, report_path, source, created_at=None):
    directory=Path(directory).resolve()
    path=directory/'evaluation_history.json'
    entries=json.loads(path.read_text(encoding='utf8')) if path.exists() else []
    tag=result['label']
    if tag=='periodic_eval':tag='after_continuation'
    checkpoint=Path(result.get('model_path') or directory/'model_final.zip')
    stat=checkpoint.stat()
    entry=dict(result=result,tag=tag,source=source,created_at=created_at or datetime.now(timezone.utc).isoformat(),
               games_path=str(Path(games_path).resolve()),report_path=str(Path(report_path).resolve()),
               checkpoint=str(checkpoint.resolve()),model_size=stat.st_size,model_mtime_ns=stat.st_mtime_ns,
               model_sha256=hashlib.sha256(checkpoint.read_bytes()).hexdigest())
    entries=[e for e in entries if e['games_path']!=entry['games_path']]
    entries.append(entry)
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(entries,ensure_ascii=False,indent=2),encoding='utf8')
    os.replace(temporary,path)
    return entry


def latest_evaluations(directory):
    path=Path(directory)/'evaluation_history.json'
    if not path.is_file():return {}
    result={}
    try:
        for entry in sorted(json.loads(path.read_text(encoding='utf8')),key=lambda e:e['created_at']):
            checkpoint=Path(entry['checkpoint'])
            if not checkpoint.is_file():continue
            stat=checkpoint.stat()
            if (stat.st_size,stat.st_mtime_ns)!=(entry['model_size'],entry['model_mtime_ns']):continue
            if not Path(entry['games_path']).is_file():continue
            result[entry['tag']]=entry
    except (OSError,ValueError,KeyError,TypeError):return {}
    return result


def next_evaluation_total(total, interval=10000):
    return (int(total)//interval+1)*interval


def saved_total(directory):
    data=json.loads((Path(directory)/'summary.json').read_text(encoding='utf8'))
    return int(data['manifest'].get('previous_training_games',0))+int(data['training_games'])
