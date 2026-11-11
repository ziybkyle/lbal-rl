"""Import a release checkpoint as a new local lineage, without old training logs."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / 'data' / 'published_scratch_142k.json'
SCHEMA = 'lbal_published_checkpoint_v1'


def is_published_origin(meta):
    return (meta.get('training_route') == 'fresh_rules'
            and meta.get('initialization') == 'published_checkpoint'
            and not meta.get('previous_run') and meta.get('train_games') == 0
            and int(meta.get('origin_training_games', 0)) > 0
            and meta.get('previous_training_games') == meta.get('origin_training_games'))


def sha256(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def json_digest(path):
    value = json.loads(Path(path).read_text(encoding='utf8'))
    payload = json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf8')
    return hashlib.sha256(payload).hexdigest()


def import_checkpoint(bundle, runs_dir=None):
    # SB3 checkpoints can contain pickled code. Authenticate the bytes against
    # this checkout's release registry before allowing the trainer to load one.
    expected = json.loads(REGISTRY.read_text(encoding='utf8'))
    if expected.get('schema') != SCHEMA:
        raise ValueError('Unsupported published checkpoint registry')
    with zipfile.ZipFile(bundle) as archive:
        if sorted(archive.namelist()) != ['checkpoint.json', 'model.zip']:
            raise ValueError('请选择本项目发布的模型包（包含 checkpoint.json 和 model.zip）。')
        if archive.getinfo('checkpoint.json').file_size > 131072 or json.loads(archive.read('checkpoint.json')) != expected:
            raise ValueError('模型包与本项目发布的检查点信息不一致。')
        if archive.getinfo('model.zip').file_size != expected['bytes']:
            raise ValueError('模型文件大小不符，请重新下载。')
        from rust_env import lbal_py
        if lbal_py.RULES_VERSION != expected['rules_version']:
            raise ValueError('请先安装与模型包一致的 Rust 规则版本。')
        # Report the exact mismatched input file before constructing a policy.
        for filename, digest in expected['game_files_sha256'].items():
            path = ROOT / 'game' / filename
            if not path.is_file() or sha256(path) != digest:
                raise ValueError(f'游戏数据与模型不匹配：game/{filename}。请参照发布说明准备同版数据。')
        from candidate_rules import FACTS_FILE, catalog, LATEST_VERSION
        from item_state_features import LATEST_VERSION as state_version, LATEST_SCHEMA_HASH
        if (json_digest(FACTS_FILE) != expected['rule_facts_sha256']
                or catalog().schema_hash != expected['candidate_rules_schema']
                or LATEST_SCHEMA_HASH != expected['item_state_schema']
                or LATEST_VERSION != expected['candidate_rules_version']
                or state_version != expected['item_state_version']):
            raise ValueError('模型输入版本不匹配，请使用该 Release 对应的源代码。')
        total = expected['training_games']
        runs = Path(runs_dir or ROOT/'runs/current_rules_training'/lbal_py.RULES_VERSION).resolve()
        destination = runs / f"published_{expected['id']}_{expected['sha256'][:8]}"
        if not destination.resolve().is_relative_to(runs):
            raise ValueError('Invalid checkpoint destination')
        if destination.exists():
            meta = json.loads((destination/'summary.json').read_text(encoding='utf8'))['manifest']
            if not is_published_origin(meta) or sha256(destination/'model_final.zip') != expected['sha256']:
                raise ValueError('同名模型目录已存在，内容与发布检查点不符。')
            return destination
        runs.mkdir(parents=True, exist_ok=True)
        checkpoint = destination/'model_final.zip'
        manifest = dict(expected['settings'], training_route='fresh_rules', expert_score_input=False,
            initialization='published_checkpoint', experiment_id=uuid.uuid4().hex,
            previous_run=None, initial_checkpoint=str(checkpoint), train_games=0,
            previous_training_games=total, origin_training_games=total,
            origin_seed_ranges=expected['training_seed_ranges'], eval_games=0,
            eval_seed_start=expected['evaluation']['seed_start'], continuous_training=True,
            rules_version=expected['rules_version'], simulator_sha256=sha256(lbal_py.__file__),
            candidate_rules_version=LATEST_VERSION, item_state_version=state_version,
            published_model_sha256=expected['sha256'], published_model_id=expected['id'],
            model_label=f'发布模型 · 累计 {total:,} 局', random_model_seed=expected['random_model_seed'])
        points = [dict(tag=tag,path=str(checkpoint),completed_games=0,
                       sampled_decisions=expected['sampled_decisions'],updated_decisions=expected['updated_decisions'])
                  for tag in ('random_initial','before_continuation','after_continuation')]
        summary = dict(manifest=manifest, training_games=0, training_wins=0,
                       training_seconds=0, checkpoints=points, evaluations=[])
        with tempfile.TemporaryDirectory(dir=runs, prefix='.import-') as temp:
            staging = Path(temp)/'checkpoint'
            staging.mkdir()
            with archive.open('model.zip') as source, (staging/'model_final.zip').open('wb') as target:
                shutil.copyfileobj(source,target)
            if sha256(staging/'model_final.zip') != expected['sha256']:
                raise ValueError('模型校验失败，请重新下载。')
            for name, value in (('manifest.json',manifest),('summary.json',summary)):
                (staging/name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf8')
            (staging/'training_games.jsonl').touch()
            staging.rename(destination)
        return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', type=Path)
    parser.add_argument('--runs-dir', type=Path)
    args = parser.parse_args()
    try:
        print(import_checkpoint(args.bundle,args.runs_dir),flush=True)
    except (OSError,ValueError,ImportError,zipfile.BadZipFile) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
