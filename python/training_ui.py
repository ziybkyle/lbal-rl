"""本地幸运房东训练面板；训练仍由独立的无头进程执行。"""
from __future__ import annotations

import datetime as dt
from collections import Counter, deque
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from reward_schemes import (REWARD_EXPLANATIONS, REWARD_NAMES, REWARD_NAME_ALIASES,
                            DEFAULT_PROGRESSIVE_RENTS, validate_progressive_rents, parse_progressive_rents,
                            DEFAULT_PROGRESSIVE_LOSS, DEFAULT_PROGRESSIVE_WIN, validate_progressive_outcomes)
from seed_policy import FIXED_EVAL_START, FIXED_EVAL_STOP, MAX_EVAL_GAMES, overlaps_fixed_evaluation
from item_toggle_policy import ITEM_TOGGLES, ESSENCE_TOGGLES, toggle_scope_label
from evaluation_history import latest_evaluations, next_evaluation_total, saved_total
from window_layout import WindowLayouts, remember_popup
from training_progress import ProgressLog
from training_options import model_label, save_suffix, save_id
from published_checkpoint import is_published_origin

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / 'runs'
TRAINER = ROOT / 'python' / 'resume_scratch.py'
BASELINE_TRAINER = ROOT / 'python' / 'resume_current_baseline.py'
BASELINE_RUNS = RUNS / 'baseline_training'
GROUPED_TRAINER = ROOT / 'python' / 'train_grouped.py'
GROUPED_RUNS = RUNS / 'grouped_training'
FRESH_TRAINER = ROOT / 'python' / 'train_fresh_rules.py'
LEGACY_FRESH_RUNS = RUNS / 'fresh_rules_training'
GROUPED_SOURCE = RUNS / 'ui_training_20261107_155951_2025d7'
REPORTER = ROOT / 'python' / 'resume_scratch_report.py'
CONTINUOUS_REPORTER = ROOT / 'python' / 'continuous_report.py'
PERIODIC_EVALUATOR = ROOT / 'python' / 'periodic_evaluate.py'
MANUAL_EVALUATOR = ROOT / 'python' / 'manual_evaluate.py'
CONTINUOUS_CHUNK = 1000
TREND_WINDOW = 1000
HISTORY_GROUP_SIZE = 10000
LIMIT = 2**31
COLUMN_SETTINGS = ROOT / '训练面板表头设置.json'
PREFERENCE_SETTINGS = ROOT / '训练面板偏好设置.json'
WINDOW_SETTINGS = ROOT / '训练面板窗口设置.json'
PROGRESS_DIRECTORY = ROOT / '运行进度记录'
PREFERENCE_FIELDS = ('train_games', 'eval_games', 'compute_threads', 'toggle_reward', 'allow_item_toggle', 'limited_item_toggles', 'reward_choice', 'trend_range', 'trend_window', 'periodic_enabled', 'periodic_interval', 'progressive_rents_json', 'progressive_loss', 'progressive_win', 'candidate_rules', 'continuous_limit_enabled', 'continuous_limit_games', 'continuous_end_eval_enabled')
HISTORY_COLUMNS = (
    ('total', '累计训练局数', 100), ('round', '本轮训练局数', 110), ('train_rents', '本轮训练结果', 145),
    ('initial_eval', '初始模型评估', 150), ('before_eval', '续训前评估', 150),
    ('rents', '续训后评估', 150),
    ('train_time', '训练耗时', 115), ('elapsed', '总耗时', 115), ('threads', '计算线程', 90),
    ('toggle', '开关奖赏', 95), ('reward', '奖励机制', 100), ('management', '主动开关', 85),
    ('seed', '评估起始种子', 145),
    ('candidates', '候选表达', 105),
    ('item_state', '物品状态', 110),
)


def matches_route(meta, route):
    if route == 'scratch':
        return meta.get('expert_score_input') is False and meta.get('training_route') in (None, 'scratch')
    if route == 'fresh_rules':
        return (meta.get('training_route') == route and meta.get('expert_score_input') is False
                and bool(meta.get('experiment_id')) and meta.get('initialization') in ('random_weights', 'checkpoint_branch', 'published_checkpoint'))
    return meta.get('training_route') == route


@lru_cache(maxsize=8)
def _probe_rules(binding, modified_ns, size):
    """Inspect the installed extension in a short-lived process."""
    try:
        program = 'import sys;sys.path.insert(0,sys.argv[1]);import lbal_py;print(lbal_py.RULES_VERSION)'
        executable = Path(sys.executable)
        if executable.name.lower() == 'pythonw.exe':
            executable = executable.with_name('python.exe')
        result = subprocess.run([str(executable), '-X', 'utf8', '-c', program, str(Path(binding).parent)],
                                capture_output=True, text=True, encoding='utf8', timeout=8,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        version = result.stdout.strip()
        if result.returncode == 0 and re.fullmatch(r'[A-Za-z0-9_.-]+', version):
            return version
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        pass
    return '未安装 Rust 绑定'


def installed_rules_version():
    directory = ROOT / 'rust' / 'python_bindings'
    for filename in ('lbal_py.pyd', 'lbal_py.so'):
        binding = directory/filename
        if binding.is_file():
            stat = binding.stat()
            return _probe_rules(str(binding), stat.st_mtime_ns, stat.st_size)
    return '未安装 Rust 绑定'


def current_rules_directory(runs_root, version):
    """Give each engine version its own empty starting point and model list."""
    folder = re.sub(r'[^A-Za-z0-9_.-]+', '_', version).strip('_') or 'unversioned'
    return Path(runs_root) / 'current_rules_training' / folder


FRESH_RUNS = current_rules_directory(RUNS, installed_rules_version())
BASELINE_RUNS = RUNS / 'current_rules_baseline' / installed_rules_version()


def normalize_column_settings(data):
    """Keep known columns, append newly introduced ones and reject unusable settings."""
    ids = [name for name, _, _ in HISTORY_COLUMNS]
    if not isinstance(data, dict):
        data = {}
    stored_order = data.get('order', [])
    order = []
    if isinstance(stored_order, list):
        for name in stored_order:
            if isinstance(name, str) and name in ids and name not in order:
                order.append(name)
    # Insert newly introduced comparisons next to the existing after-training
    # column even when the user has an older saved column order.
    for name in ('initial_eval', 'before_eval'):
        if name not in order and 'rents' in order:
            order.insert(order.index('rents'), name)
    order.extend(name for name in ids if name not in order)
    stored_hidden = data.get('hidden', [])
    hidden = [name for name in ids if isinstance(stored_hidden, list) and name in stored_hidden]
    if len(hidden) == len(ids):
        hidden = []
    stored_names = data.get('names', {})
    names = {name: text.strip() for name, text in stored_names.items()
             if name in ids and isinstance(text, str) and text.strip()} if isinstance(stored_names, dict) else {}
    return {'order': order, 'hidden': hidden, 'names': names}


def load_column_settings(path: Path):
    try:
        return normalize_column_settings(json.loads(path.read_text(encoding='utf-8')))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return normalize_column_settings({})


def save_column_settings(path: Path, settings):
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as output:
            json.dump(normalize_column_settings(settings), output, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def valid_preference(name, value):
    """Return a validated preference, or None for an unfinished/invalid edit."""
    if name in ('allow_item_toggle', 'limited_item_toggles', 'periodic_enabled', 'candidate_rules', 'continuous_limit_enabled', 'continuous_end_eval_enabled'):
        return value if isinstance(value, bool) else None
    if not isinstance(value, str):
        return None
    value = value.strip()
    if name == 'reward_choice':
        value = REWARD_NAME_ALIASES.get(value, value)
        return value if value in REWARD_NAMES.values() else None
    if name == 'progressive_rents_json':
        try:
            return json.dumps(parse_progressive_rents(value), ensure_ascii=False)
        except ValueError:
            return None
    if name in ('progressive_loss', 'progressive_win'):
        try:
            number = float(value)
            loss = number if name == 'progressive_loss' else DEFAULT_PROGRESSIVE_LOSS
            win = number if name == 'progressive_win' else DEFAULT_PROGRESSIVE_WIN
            validate_progressive_outcomes(loss, win)
            return value
        except ValueError:
            return None
    if name == 'trend_range':
        return value if value in ('all', '10000', '50000') else None
    if name == 'trend_window':
        return value if value in ('1000', '2000', '5000', '10000') else None
    if name == 'continuous_limit_games':
        try:
            number = int(value)
            return value if 1000 <= number <= 10_000_000 and number % 1000 == 0 else None
        except ValueError:
            return None
    if name == 'periodic_interval':
        try:
            number = int(value)
            return value if 1000 <= number <= 1_000_000 and number % 1000 == 0 else None
        except ValueError:
            return None
    if name in ('train_games', 'eval_games', 'compute_threads'):
        try:
            number = int(value)
        except ValueError:
            return None
        low, high = {'train_games': (2, 1_000_000), 'eval_games': (1, MAX_EVAL_GAMES),
                     'compute_threads': (1, 64)}[name]
        return value if low <= number <= high else None
    if name == 'toggle_reward':
        try:
            number = float(value)
        except ValueError:
            return None
        return value if math.isfinite(number) and abs(number) <= 1 else None
    return None


def read_preferences(path: Path):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) and isinstance(data.get('routes'), dict) else {'version': 1, 'routes': {}}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {'version': 1, 'routes': {}}


def save_preferences(path: Path, route: str, values: dict):
    data = read_preferences(path)
    existing = data['routes'].get(route)
    existing = dict(existing) if isinstance(existing, dict) else {}
    for name, value in values.items():
        if name in PREFERENCE_FIELDS:
            normalized = valid_preference(name, value)
            if normalized is not None:
                existing[name] = normalized
    data['routes'][route] = existing
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as output:
            json.dump(data, output, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def completed_runs(runs: Path = RUNS, route: str = 'scratch') -> list[tuple[int, Path]]:
    """List compatible completed scratch experiments; ignore test runs."""
    found = []
    if not runs.is_dir():
        return found
    directories = list(runs.iterdir())
    if route == 'fresh_rules':
        directories.extend(runs.glob('experiments/*/*/ui_training_*'))
    for directory in directories:
        if not directory.is_dir() or directory.name.startswith('_') or 'smoke' in directory.name.lower():
            continue
        summary_path, model = directory / 'summary.json', directory / 'model_final.zip'
        if not summary_path.is_file() or not model.is_file():
            continue
        try:
            data = json.loads(summary_path.read_text(encoding='utf-8'))
            meta = data['manifest']
            compatible = matches_route(meta, route)
            if meta.get('reward_mode') not in REWARD_NAMES or not compatible:
                continue
            count = int(data['training_games'])
            target = int(meta.get('train_games', meta.get('games_budget', -1)))
            minimum = 0 if is_published_origin(meta) else 1 if meta.get('continuous_training') else 2
            if count != target or count < minimum or not (directory / 'training_games.jsonl').is_file():
                continue
            total = int(meta.get('previous_training_games', 0)) + count
            found.append((total, directory))
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
    return sorted(found, key=lambda item: (item[0], item[1].stat().st_mtime_ns), reverse=True)


def used_ranges(runs: Path = RUNS) -> list[tuple[int, int]]:
    """Reserve even aborted or unrelated experiments with readable manifests."""
    intervals = []
    if not runs.is_dir():
        return intervals
    for path in runs.rglob('manifest.json'):
        try:
            meta = json.loads(path.read_text(encoding='utf-8'))
            for lo, hi in meta.get('origin_seed_ranges', []):
                if 0 <= int(lo) < int(hi) <= LIMIT:
                    intervals.append((int(lo), int(hi)))
            for start_key, count_keys, margin in (
                ('train_seed_start', ('train_games', 'games_budget'), 2),
                ('eval_seed_start', ('eval_games',), 0),
            ):
                if start_key not in meta:
                    continue
                count = next((int(meta[k]) for k in count_keys if k in meta), 0)
                start = int(meta[start_key])
                if start >= 0 and count > 0:
                    intervals.append((start, start + count + margin))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return intervals


def find_gap(intervals: list[tuple[int, int]], width: int, start: int) -> int:
    pos = start
    for lo, hi in sorted(intervals):
        if hi <= pos:
            continue
        if pos + width <= lo:
            break
        pos = max(pos, hi)
    if width <= 0 or pos + width > LIMIT:
        raise ValueError('没有足够的可用种子，请检查训练记录。')
    return pos


def choose_seeds(games: int, eval_games: int, runs: Path = RUNS) -> tuple[int, int]:
    if not 1 <= eval_games <= MAX_EVAL_GAMES:
        raise ValueError('评估种子数超出允许范围。')
    intervals = used_ranges(runs)
    # Validate actual logs as well as metadata: one early training run used a
    # different first seed than its manifest recorded.
    for path in runs.rglob('training_games.jsonl') if runs.is_dir() else ():
        try:
            with path.open(encoding='utf-8') as source:
                for line in source:
                    if not line.strip():
                        continue
                    seed = int(json.loads(line)['seed'])
                    if overlaps_fixed_evaluation(seed, seed + 1):
                        raise ValueError(f'固定评估种子与历史训练冲突：{path}')
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f'无法核对历史训练种子：{path}，请检查记录。') from exc
    # A readable manifest also reserves seeds for interrupted runs, whose log
    # may be incomplete or absent.
    for path in runs.rglob('manifest.json') if runs.is_dir() else ():
        try:
            meta = json.loads(path.read_text(encoding='utf-8'))
            start = int(meta['train_seed_start'])
            count = int(meta.get('train_games', meta.get('games_budget', 0)))
            if count > 0 and overlaps_fixed_evaluation(start, start + count + 2):
                raise ValueError(f'固定评估种子与历史训练冲突：{path}')
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            continue
    intervals.append((FIXED_EVAL_START, FIXED_EVAL_STOP))
    train = find_gap(intervals, games + 2, 2_000_000)
    return train, FIXED_EVAL_START


def python_executable() -> str:
    path = Path(sys.executable)
    if path.name.lower() == 'pythonw.exe':
        console = path.with_name('python.exe')
        if console.is_file():
            return str(console)
    return str(path)


def format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f'{hours} 小时 {minutes} 分 {seconds} 秒'
    if minutes:
        return f'{minutes} 分 {seconds} 秒'
    return f'{seconds} 秒'


@lru_cache(maxsize=128)
def _training_stats_cached(path: Path, size: int, modified_ns: int, expected: int):
    count = wins = truncated = rents = 0
    try:
        with path.open(encoding='utf-8') as source:
            for line in source:
                row = json.loads(line)
                count += 1
                is_truncated = bool(row.get('truncated', False))
                truncated += int(is_truncated)
                wins += int(bool(row['won']) and not is_truncated)
                rents += int(row['rents_paid'])
        if count != expected or count == 0:
            return None
        return {'games': count, 'wins': wins, 'valid': count - truncated,
                'truncated': truncated, 'average_rents': rents / count}
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def training_stats(directory: Path, expected: int):
    path = directory / 'training_games.jsonl'
    try:
        stat = path.stat()
        return _training_stats_cached(path.resolve(), stat.st_size, stat.st_mtime_ns, expected)
    except OSError:
        return None


def panel_elapsed(directory: Path):
    try:
        data = json.loads((directory / 'panel_timing.json').read_text(encoding='utf-8'))
        seconds = float(data['elapsed_seconds'])
        return seconds if math.isfinite(seconds) and seconds >= 0 else None
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def optional_elapsed(value):
    try:
        seconds = float(value)
        return format_elapsed(seconds) if math.isfinite(seconds) and seconds >= 0 else '未记录'
    except (TypeError, ValueError):
        return '未记录'


def performance_display(rents, wins, valid):
    """Show average rents / win rate, with no invented value for missing data."""
    try:
        rents = float(rents)
        wins, valid = int(wins), int(valid)
        if not math.isfinite(rents) or valid < 0 or wins < 0 or wins > valid:
            return '未记录'
        rate = f'{wins / valid * 100:.2f}%' if valid else '无有效局'
        return f'{rents:.3f} / {rate}'
    except (TypeError, ValueError, OverflowError):
        return '未记录'


def evaluation_display(evaluation):
    if not isinstance(evaluation, dict):
        return '未记录'
    return performance_display(evaluation.get('avg_rents'), evaluation.get('wins'),
                               evaluation.get('valid_games', evaluation.get('games')))


class DeathDistribution:
    """Classify recorded training games without treating truncation as a loss."""
    def __init__(self):
        self.losses = Counter()
        self.wins = self.truncated = self.unknown = 0

    @property
    def total(self):
        return sum(self.losses.values()) + self.wins + self.truncated + self.unknown

    def add(self, game):
        if not isinstance(game, dict):
            self.unknown += 1
        elif game.get('truncated', False) is True:
            self.truncated += 1
        elif game.get('truncated', False) is not False or not isinstance(game.get('won'), bool):
            self.unknown += 1
        elif game['won']:
            self.wins += 1
        elif type(game.get('rents_paid')) is int and game['rents_paid'] >= 0:
            self.losses[min(game['rents_paid'], 12)] += 1
        else:
            self.unknown += 1


def history_death_distribution(row):
    """Read exactly the games represented by a history-table row."""
    parts = row.get('group_parts') or [(row['directory'], 0, row['round_games'])]
    distribution = DeathDistribution()
    for directory, start, stop in parts:
        seen = selected = 0
        with (Path(directory) / 'training_games.jsonl').open(encoding='utf-8') as source:
            for line in source:
                if not line.strip():
                    continue
                if start <= seen < stop:
                    distribution.add(json.loads(line))
                    selected += 1
                seen += 1
                if seen == stop:
                    break
        if selected != stop - start:
            raise ValueError('逐局记录不完整，无法统计所选轮次')
    return distribution


class TrainingTrend:
    """Non-overlapping 1000-game windows across the selected model ancestry."""
    def __init__(self, window=TREND_WINDOW):
        self.window = window
        self.total = self.count = self.wins = self.valid = self.rents = 0
        self.actions = Counter()
        self.action_games = 0
        self.points = []
        self._prefix = deque([(0, 0, 0)], maxlen=10001)
        self._rolling = {size: [] for size in (1000, 2000, 5000, 10000)}
        self._cumulative = (0, 0, 0)

    def add(self, row):
        rents = int(row['rents_paid'])
        truncated = bool(row.get('truncated', False))
        self.total += 1
        a, b, c = self._cumulative
        self._cumulative = (a + int(bool(row['won']) and not truncated), b + int(not truncated), c + rents)
        self._prefix.append(self._cumulative)
        if self.total % 100 == 0:
            for size in self._rolling:
                if self.total >= size:self._rolling[size].append(self._rolling_point(size))
        self.count += 1
        self.rents += rents
        self.valid += int(not truncated)
        self.wins += int(bool(row['won']) and not truncated)
        actions = row.get('actions')
        if isinstance(actions, dict):
            self.action_games += 1
            for key in ('reroll', 'remove', 'toggle_item', 'destroy_item'):
                self.actions[key] += int(actions.get(key, 0))
        if self.count == self.window:
            self.points.append({'total': self.total, 'wins': self.wins, 'valid': self.valid,
                                'rate': self.wins * 100 / self.valid if self.valid else None,
                                'rents': self.rents / self.count,
                                'actions': dict(self.actions), 'action_games': self.action_games})
            self.count = self.wins = self.valid = self.rents = 0
            self.actions.clear()
            self.action_games = 0

    def _rolling_point(self, window):
        count = min(window, self.total)
        old = self._prefix[-count - 1]
        wins, valid, rents = (a-b for a,b in zip(self._cumulative,old))
        return dict(total=self.total, count=count, wins=wins, valid=valid,
                    rate=wins*100/valid if valid else None, rents=rents/count,
                    partial=count<window)

    def rolling_points(self, window):
        points = list(self._rolling.get(window, []))
        if self.total and (not points or points[-1]['total'] != self.total):
            points.append(self._rolling_point(window))
        return points


def aggregate_trend(points, window):
    """Combine complete 1000-game bins, weighted by valid games for win rate."""
    if window == TREND_WINDOW:
        return points
    size = window // TREND_WINDOW
    result = []
    for start in range(0, len(points) - size + 1, size):
        group = points[start:start + size]
        wins = sum(point['wins'] for point in group)
        valid = sum(point['valid'] for point in group)
        result.append({'total': group[-1]['total'], 'wins': wins, 'valid': valid,
                       'rate': wins * 100 / valid if valid else None,
                       'rents': sum(point['rents'] for point in group) / size})
    return result


def training_trend(history, window=TREND_WINDOW):
    """Do not join across missing or incomplete records, which would mislabel windows."""
    trend = TrainingTrend(window)
    try:
        for row in history:
            path = row['directory'] / 'training_games.jsonl'
            seen = 0
            with path.open(encoding='utf-8') as source:
                for line in source:
                    if line.strip():
                        trend.add(json.loads(line))
                        seen += 1
            if seen != row['round_games']:
                raise ValueError(f'{path} 局数与记录不符')
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return trend, f'部分训练记录无法读取，后续走势图未显示：{exc}'
    return trend, ''


def training_history(directory: Path) -> tuple[list[dict], str]:
    """Follow the selected model's parent chain, keeping each round's own evaluation."""
    history, seen, warning = [], set(), ''
    expected_experiment = None
    current = directory.resolve()
    while current:
        if current in seen:
            warning = '历史记录存在循环，已停止读取。'
            break
        seen.add(current)
        try:
            data = json.loads((current / 'summary.json').read_text(encoding='utf-8'))
            meta = data['manifest']
            if not history and meta.get('training_route') == 'fresh_rules':
                if not matches_route(meta, 'fresh_rules'):
                    raise ValueError('缺少新规则实验的独立起点记录')
                expected_experiment = meta.get('experiment_id')
            if expected_experiment and (not matches_route(meta, 'fresh_rules') or meta.get('experiment_id') != expected_experiment):
                raise ValueError('新规则历史不能混入其他路线或实验')
            evaluations = {item['label']: item for item in data['evaluations']}
            registered = latest_evaluations(current)
            evaluations.update({tag:entry['result'] for tag,entry in registered.items()})
            final = evaluations.get('after_continuation') or evaluations.get('final')
            periodic = None
            periodic_path = current / 'periodic_eval.json'
            if not final and periodic_path.exists():
                try:
                    periodic = json.loads(periodic_path.read_text(encoding='utf-8'))
                    if not isinstance(periodic, dict) or 'seed_start' not in periodic:
                        raise ValueError('定期评估记录格式错误')
                    final = periodic
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    warning = f'定期评估记录无法读取：{current.name}'
            before = evaluations.get('before_continuation') or evaluations.get('initial')
            initial = evaluations.get('random_initial') or evaluations.get('initial')
            games = valid = wins = 0
            rents = rate = None
            if final:
                games = int(final['games'])
                valid = int(final.get('valid_games', games))
                wins = int(final['wins'])
                rents = float(final['avg_rents'])
                if games <= 0 or valid <= 0 or valid > games or not 0 <= wins <= valid or not math.isfinite(rents):
                    raise ValueError('无有效评估结果')
                rate = wins / valid * 100
            elif not meta.get('continuous_training'):
                raise ValueError('缺少评估结果')
            round_games = int(data['training_games'])
            total = int(meta.get('previous_training_games', 0)) + round_games
            sim = next((v for k, v in meta.get('files_sha256', {}).items() if k.lower().endswith('lbal_py.pyd')), None)
            progressive_rents = progressive_loss = progressive_win = None
            if meta.get('reward_mode') == 'progressive':
                try:
                    progressive_rents = validate_progressive_rents(meta.get('progressive_rents', DEFAULT_PROGRESSIVE_RENTS))
                    progressive_loss, progressive_win = validate_progressive_outcomes(
                        meta.get('progressive_loss', DEFAULT_PROGRESSIVE_LOSS), meta.get('progressive_win', DEFAULT_PROGRESSIVE_WIN))
                except (ValueError, TypeError):
                    warning = f'方案二奖励设置无法读取：{current.name}'
            history.append({'directory': current, 'total': total, 'wins': wins, 'valid': valid,
                            'games': games, 'rate': rate, 'rents': rents,
                            'initial': initial, 'before': before, 'simulator': sim,
                            'toggle_reward': float(meta.get('toggle_reward', -0.05)),
                            'allow_item_toggle': bool(meta.get('allow_item_toggle', True)),
                            'limited_item_toggles': bool(meta.get('limited_item_toggles', False)),
                            'item_toggle_allowlist': (tuple(sorted(tuple(pair) for pair in meta['item_toggle_allowlist']))
                                                     if meta.get('limited_item_toggles') and meta.get('item_toggle_allowlist') is not None else None),
                            'candidate_rules_version': meta.get('candidate_rules_version'),
                            'item_state_version': meta.get('item_state_version'),
                            'item_state_upgraded': bool(meta.get('item_state_upgraded')),
                            'rules_upgraded': bool(meta.get('rules_upgraded')),
                            'rules_version': meta.get('rules_version'),
                            'experiment_id': meta.get('experiment_id'),
                            'candidate_policy_upgraded': bool(meta.get('candidate_policy_upgraded')),
                            'candidate_capacity_upgraded': bool(meta.get('candidate_capacity_upgraded')),
                            'training': training_stats(current, round_games),
                            'round_games': round_games,
                            'continuous': bool(meta.get('continuous_training')),
                            'training_seconds': data.get('training_seconds'),
                            'elapsed_seconds': panel_elapsed(current),
                            'threads': meta.get('torch_threads'),
                            'reward_mode': meta.get('reward_mode', 'unknown'),
                            'progressive_rents': progressive_rents,
                            'progressive_loss': progressive_loss, 'progressive_win': progressive_win,
                            'evaluation_source': registered.get('after_continuation', {}).get('source'),
                            'evaluation_rules': final.get('rules_version') if final else None,
                            'seed': (final.get('seed_start', meta.get('eval_seed_start')) if final else None) if games else None})
            parent = meta.get('previous_run')
            current = Path(parent).resolve() if parent else None
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            warning = f'部分历史记录无法读取：{current.name}'
            break
    history.reverse()
    return history, warning


def display_history(history, group_size=HISTORY_GROUP_SIZE):
    """Coalesce continuous games for display, keeping every checkpoint on disk."""
    result, parts = [], []
    count = 0

    def flush():
        nonlocal parts, count
        if not parts:
            return
        last, _, end, _ = parts[-1]
        totals = {'games': 0, 'wins': 0, 'valid': 0, 'truncated': 0, 'rents': 0.0}
        for _, _, _, stats in parts:
            for key in ('games', 'wins', 'valid', 'truncated'):
                totals[key] += stats[key]
            totals['rents'] += stats['average_rents'] * stats['games']
        def common(key):
            values = [row.get(key) for row, _, _, _ in parts]
            return values[0] if all(v == values[0] for v in values) else None
        def elapsed(key):
            vals = [row[key] for row, start, stop, _ in parts
                    if start == 0 and stop == row['round_games']]
            if len(vals) != len(parts) or any(v is None for v in vals):
                return None
            return sum(float(v) for v in vals)
        row = dict(last)
        row.update({'total': last['total'] - last['round_games'] + end,
                    'round_games': count, 'grouped_continuous': True,
                    'group_partial': count < group_size,
                    'group_start': parts[0][0]['total'] - parts[0][0]['round_games'] + parts[0][1] + 1,
                    'group_sources': len({str(p[0]['directory']) for p in parts}),
                    'group_parts': [(p[0]['directory'], p[1], p[2]) for p in parts],
                    'group_checkpoint_total': last['total'],
                    'candidate_policy_upgraded': any(row.get('candidate_policy_upgraded') for row, _, _, _ in parts),
                    'candidate_capacity_upgraded': any(row.get('candidate_capacity_upgraded') for row, _, _, _ in parts),
                    'item_state_upgraded': any(row.get('item_state_upgraded') for row, _, _, _ in parts),
                    'rules_upgraded': any(row.get('rules_upgraded') for row, _, _, _ in parts),
                    'threads': common('threads'), 'reward_mode': common('reward_mode'),
                    'progressive_rents': common('progressive_rents'),
                    'progressive_loss': common('progressive_loss'), 'progressive_win': common('progressive_win'),
                    'toggle_reward': common('toggle_reward'), 'allow_item_toggle': common('allow_item_toggle'),
                    'limited_item_toggles': common('limited_item_toggles'),
                    'item_toggle_allowlist': common('item_toggle_allowlist'),
                    'training_seconds': elapsed('training_seconds'), 'elapsed_seconds': elapsed('elapsed_seconds'),
                    'training': {'games': totals['games'], 'wins': totals['wins'], 'valid': totals['valid'],
                                 'truncated': totals['truncated'], 'average_rents': totals['rents'] / totals['games']}})
        if end != last['round_games']:
            row.update({'wins': 0, 'valid': 0, 'games': 0, 'rate': None, 'rents': None,
                        'initial': None, 'before': None, 'seed': None})
        # Differing values need a distinct display from unknown values.
        row['mixed_fields'] = {key for key in ('threads', 'reward_mode', 'toggle_reward', 'allow_item_toggle', 'limited_item_toggles', 'item_toggle_allowlist', 'progressive_rents', 'progressive_loss', 'progressive_win')
                               if len({p[0].get(key) for p in parts}) > 1}
        result.append(row)
        parts, count = [], 0

    for row in history:
        if parts and any(parts[-1][0].get(key) != row.get(key)
                         for key in ('candidate_rules_version', 'item_state_version', 'rules_version')):
            flush()
        if not row.get('continuous') or not row.get('training'):
            flush()
            result.append(row)
            continue
        offset, size = 0, row['round_games']
        cached_rows = None
        while offset < size:
            end = min(size, offset + group_size - count)
            if offset == 0 and end == size:
                stats = row['training']
            else:
                if cached_rows is None:
                    with (row['directory'] / 'training_games.jsonl').open(encoding='utf-8') as source:
                        cached_rows = [json.loads(line) for line in source if line.strip()]
                    if len(cached_rows) != size:
                        raise ValueError('持续训练记录局数不符')
                chunk = cached_rows[offset:end]
                truncated = sum(bool(g.get('truncated', False)) for g in chunk)
                stats = {'games': len(chunk), 'wins': sum(bool(g['won']) and not g.get('truncated', False) for g in chunk),
                         'valid': len(chunk) - truncated, 'truncated': truncated,
                         'average_rents': sum(int(g['rents_paid']) for g in chunk) / len(chunk)}
            parts.append((row, offset, end, stats))
            count += end - offset
            offset = end
            if count == group_size:
                flush()
        if row.get('games'):
            flush()  # Keep each evaluation attached to its actual checkpoint.
    flush()
    return result


def commit_requested_stop(app, directory):
    """Atomically finish a pending manual stop at a saved boundary."""
    if not hasattr(app, 'stop_control_lock'):
        app.stop_control_lock = threading.Lock()
    if not hasattr(app, 'stop_committed'):
        app.stop_committed = threading.Event()
    with app.stop_control_lock:
        if app.stop_committed.is_set():
            return True
        if not app.stop_requested.is_set():
            return False
        app.stop_committed.set()
        app.events.put(('continuous_done', str(directory)))
        return True


class TrainingApp:
    def __init__(self, root: tk.Tk, notebook=None, route='scratch'):
        self.root = root
        self.route = route
        self.rules_version = installed_rules_version()
        self.runs_dir = (BASELINE_RUNS if route == 'baseline' else GROUPED_RUNS if route == 'grouped'
                         else FRESH_RUNS if route == 'fresh_rules' else RUNS)
        self.notebook = notebook
        self.root.title('幸运房东 · 训练面板')
        if not hasattr(self.root, '_window_layouts'):
            self.root.geometry('1020x880' if route == 'fresh_rules' or notebook is not None else '1020x780')
            self.root.minsize(790, 740)
            self.root._window_layouts = WindowLayouts(self.root, WINDOW_SETTINGS)
            self.root._window_layouts.remember(self.root, 'main')
        self.events: queue.Queue = queue.Queue()
        self.process: subprocess.Popen | None = None
        self.worker: threading.Thread | None = None
        self.cancelled = False
        self.continuous_mode = False
        self.training_active = False
        self.thread_config_lock = threading.Lock()
        self.requested_threads = 2
        self.current_stage_threads = 2
        self.thread_input_invalid = False
        self.preference_job = None
        self.preferences_dirty = False
        self.stop_requested = threading.Event()
        self.stop_control_lock = threading.Lock()
        self.stop_committed = threading.Event()
        self.stop_file: Path | None = None
        self.continuous_games = 0
        self.continuous_wins = 0
        self.continuous_rents = 0.0
        self.live_offset = 0
        self.trend = TrainingTrend()
        self.trend_warning = ''
        self.trend_canvases = {}
        self.trend_boxes = {}
        self.trend_points = {}
        self.session_deaths = DeathDistribution()
        self.death_offsets = {}
        self.death_warning = ''
        self.death_popup = None
        self.training_finished = 0
        self.training_average: float | None = None
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.current_dir: Path | None = None
        self.run_options: list[tuple[int, Path]] = []
        self.history: list[dict] = []
        self.display_history: list[dict] = []
        self.chart_points: dict[str, list[tuple[float, int]]] = {}
        self.extra_eval_canvases = {}
        self.chart_zoom = None
        self.column_settings = load_column_settings(COLUMN_SETTINGS)
        self.train_games = tk.StringVar(value='1000' if route == 'fresh_rules' else '500')
        self.eval_games = tk.StringVar(value='1000')
        self.compute_threads = tk.StringVar(value='2')
        self.thread_hint = tk.StringVar()
        self.toggle_reward = tk.StringVar(value='0')
        self.allow_item_toggle = tk.BooleanVar(value=True)
        self.limited_item_toggles = tk.BooleanVar(value=False)
        self.candidate_rules = tk.BooleanVar(value=self.route in ('scratch', 'fresh_rules', 'baseline'))
        self.reward_choice = tk.StringVar(value=REWARD_NAMES['progressive' if route in ('fresh_rules','baseline') else 'fixed'])
        self.progressive_rents_json = tk.StringVar(value=json.dumps(DEFAULT_PROGRESSIVE_RENTS))
        self.progressive_loss = tk.StringVar(value=str(DEFAULT_PROGRESSIVE_LOSS))
        self.progressive_win = tk.StringVar(value=str(DEFAULT_PROGRESSIVE_WIN))
        self.trend_range = tk.StringVar(value='all')
        self.trend_window = tk.StringVar(value='1000')
        self.periodic_enabled = tk.BooleanVar(value=False)
        self.periodic_interval = tk.StringVar(value='10000')
        self.continuous_limit_enabled = tk.BooleanVar(value=False)
        self.continuous_limit_games = tk.StringVar(value='1000')
        self.continuous_limit = None
        self.continuous_end_eval_enabled = tk.BooleanVar(value=False)
        self.continuous_end_eval_config = None
        self.overview = tk.StringVar(value='暂无训练概览。')
        self.periodic_config = None
        self.reward_caption = tk.StringVar()
        self.toggle_hint = tk.StringVar()
        self.choice = tk.StringVar()
        self.origin_caption = tk.StringVar()
        self.status = tk.StringVar(value='准备就绪')
        self.elapsed_display = tk.StringVar(value='本轮已用时间：—')
        self.latest_summary = tk.StringVar(value='选择已有模型，开始新一轮训练。')
        self.rolling_summary = tk.StringVar(value='最近 1000 局：暂无训练数据')
        self.analysis_running = False
        self.experiments = None
        self.death_summary = tk.StringVar(value='开始本轮训练后显示失败阶段。历史轮次可在结果对比中查看。')
        self._make_ui()
        self.compute_threads.trace_add('write', self.thread_setting_changed)
        self.trend_range.trace_add('write', self.trend_setting_changed)
        self.trend_window.trace_add('write', self.trend_setting_changed)
        self.progressive_rents_json.trace_add('write', lambda *_: self.update_reward_caption())
        self.progressive_loss.trace_add('write', lambda *_: self.update_reward_caption())
        self.progressive_win.trace_add('write', lambda *_: self.update_reward_caption())
        self.refresh_runs()
        self.restore_preferences()
        self.restore_progress_log()
        if self.experiments:
            self.experiments.restore()
        self.periodic_enabled.set(True)
        self.periodic_interval.set('10000')
        if route == 'fresh_rules':
            self.candidate_rules.set(True)
        self.continuous_limit_enabled.trace_add('write', self.update_continuous_controls)
        self.update_continuous_controls()
        self.allow_item_toggle.trace_add('write', self.toggle_permission_changed)
        self.limited_item_toggles.trace_add('write', self.limited_toggle_changed)
        for name in PREFERENCE_FIELDS:
            getattr(self, name).trace_add('write', self.schedule_preference_save)
        if notebook is None:
            self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.root.after(150, self.consume_events)
        self.root.after(1000, self.update_elapsed)

    def _make_ui(self):
        from training_ui_layout import build_training_page
        build_training_page(self)

    def open_advanced_settings(self):
        if self.experiments and self.experiments.branch:
            self.experiments.explain_locked()
            return
        if self.training_active:
            messagebox.showinfo('训练正在运行', '奖励与动作权限请在停止并保存后修改；计算线程可以直接在主页面调整。', parent=self.root)
            return
        window = tk.Toplevel(self.root)
        window.title('更多训练设置');window.transient(self.root)
        remember_popup(self, window, 'advanced')
        frame = ttk.Frame(window, padding=20);frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='物品操作', font=('Microsoft YaHei UI', 12, 'bold')).pack(anchor='w')
        ttk.Checkbutton(frame, text='允许主动开关物品和精华', variable=self.allow_item_toggle).pack(anchor='w', pady=6)
        ttk.Checkbutton(frame, text='只允许开关指定清单中的物品', variable=self.limited_item_toggles).pack(anchor='w')
        ttk.Button(frame, text='查看清单', command=self.show_toggle_list).pack(anchor='w', pady=6)
        row = ttk.Frame(frame);row.pack(fill='x', pady=10)
        ttk.Label(row, text='每次开关奖赏').pack(side='left')
        ttk.Entry(row, textvariable=self.toggle_reward, width=10).pack(side='left', padx=8)
        ttk.Label(frame, text='通常保持 0；试验范围建议 −0.05～0，允许输入负数。', foreground='#66768a').pack(anchor='w')
        ttk.Button(frame, text='当前奖励方案说明', command=self.show_reward_help).pack(anchor='w', pady=10)
        ttk.Label(frame, text='持续训练每 1000 局保存；本实验累计每 10000 局评估一次。\n训练和评估使用不同种子，评估不更新模型。', foreground='#66768a').pack(anchor='w')
        ttk.Button(frame, text='保存并关闭', command=lambda:(self.flush_preferences(),window.destroy())).pack(anchor='e', pady=(15,0))
        window.grab_set()

    def open_analysis(self, mode):
        directory = self.selected_run()
        if self.route == 'baseline' and directory == self.runs_dir:
            directory = self.runs_dir / '_initial'
        live = self.current_dir if self.training_active and mode != 'evaluation' else None
        if not directory or (not (directory/'summary.json').is_file() and not live):
            messagebox.showinfo('暂无已保存数据', '请先完成一个保存阶段，或选择已有模型。', parent=self.root);return
        if mode == 'custom':
            window = tk.Toplevel(self.root);window.title('自定义训练分析范围');window.transient(self.root)
            remember_popup(self, window, 'analysis_range')
            frame = ttk.Frame(window,padding=20);frame.pack()
            from training_analytics import analysis_history
            try:
                history = analysis_history(directory, live)
                total = int(history[-1][1]['manifest'].get('previous_training_games',0)) + int(history[-1][1]['training_games'])
                lower = int(history[0][1]['manifest'].get('previous_training_games',0)) + 1
            except (OSError,ValueError,KeyError) as exc:
                messagebox.showinfo('尚无完整数据',str(exc),parent=self.root);window.destroy();return
            ttk.Label(frame,text=f'本实验已保存 {total:,} 局；首尾局均包含在统计范围。').grid(row=0,column=0,columnspan=2,pady=(0,10))
            first,last = tk.StringVar(value=str(max(lower,total-999))),tk.StringVar(value=str(total))
            for row,(title,var) in enumerate([('从第几局',first),('到第几局',last)],1):
                ttk.Label(frame,text=title).grid(row=row,column=0,padx=6,pady=4);ttk.Entry(frame,textvariable=var,width=15).grid(row=row,column=1)
            def submit():
                try:
                    low,high=int(first.get()),int(last.get())
                    if not lower<=low<=high<=total:raise ValueError()
                except ValueError:
                    messagebox.showerror('范围无效',f'请输入 1～{total} 范围内的起止局数。',parent=window);return
                window.destroy();self.begin_analysis(directory,mode,low,high)
            ttk.Button(frame,text='生成并打开分析',command=submit).grid(row=3,column=0,columnspan=2,pady=12)
            window.grab_set();return
        self.begin_analysis(directory,mode)

    def begin_analysis(self, directory, mode, first=None, last=None):
        if self.analysis_running:return
        self.analysis_running=True
        live = self.current_dir if self.training_active and mode != 'evaluation' else None
        for button in self.analysis_buttons:button.configure(state='disabled')
        def work():
            try:
                command=[python_executable(),'-u',str(ROOT/'python/training_analytics.py'),str(directory),'--mode',mode]
                if first is not None:command.extend(['--first',str(first),'--last',str(last)])
                if live is not None:command.extend(['--live',str(live)])
                result=subprocess.run(command,cwd=ROOT,capture_output=True,text=True,encoding='utf8',
                    env=dict(os.environ,PYTHONUTF8='1'),creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                if result.returncode:raise ValueError(result.stderr[-3000:] or '分析生成失败')
                self.events.put(('analysis_done',json.loads(result.stdout.splitlines()[-1])['path']))
            except Exception as exc:self.events.put(('analysis_error',str(exc)))
        threading.Thread(target=work,daemon=True).start()

    def _make_comparison_ui(self):
        outer = ttk.Frame(self.comparison_tab, padding=14)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='训练结果对比', font=('Microsoft YaHei UI', 16, 'bold')).pack(anchor='w')
        self.comparison_caption = tk.StringVar()
        caption_label = ttk.Label(outer, textvariable=self.comparison_caption, foreground='#52606d', wraplength=700, justify='left')
        caption_label.pack(anchor='w', pady=(4, 8))
        ttk.Label(outer, text='每轮训练与评估（数据格式：平均交租次数 / 通关率；横向滚动查看全部列）', font=('Microsoft YaHei UI', 10, 'bold')).pack(anchor='w', pady=(10, 4))
        table_frame = ttk.Frame(outer)
        table_frame.pack(fill='both', expand=True)
        columns = tuple(name for name, _, _ in HISTORY_COLUMNS)
        self.history_table = ttk.Treeview(table_frame, columns=columns, show='headings', height=6, selectmode='browse')
        for name, title, width in HISTORY_COLUMNS:
            self.history_table.heading(name, text=title)
            self.history_table.column(name, width=width, minwidth=width, anchor='center', stretch=False)
        self.apply_column_settings(self.column_settings)
        bar = ttk.Scrollbar(table_frame, orient='vertical', command=self.history_table.yview)
        horizontal = ttk.Scrollbar(table_frame, orient='horizontal', command=self.history_table.xview)
        self.history_table.configure(yscrollcommand=bar.set, xscrollcommand=horizontal.set)
        bar.pack(side='right', fill='y')
        horizontal.pack(side='bottom', fill='x')
        self.history_table.pack(side='left', fill='both', expand=True)
        self.history_table.bind('<<TreeviewSelect>>', lambda event: self.update_selected_comparison())
        self.pair_summary = tk.StringVar(value='暂无结果。')
        pair_label = ttk.Label(outer, textvariable=self.pair_summary, wraplength=700, justify='left', font=('Microsoft YaHei UI', 10))
        pair_label.pack(side='bottom', before=table_frame, anchor='w', pady=(9, 6))
        buttons = ttk.Frame(outer)
        buttons.pack(side='bottom', before=pair_label, fill='x')
        ttk.Button(buttons, text='打开所选轮次报告', command=self.open_history_report).pack(side='left')
        ttk.Button(buttons, text='刷新对比', command=self.refresh_comparison).pack(side='left', padx=8)
        ttk.Button(buttons, text='自定义表头', command=self.open_column_settings).pack(side='left', padx=8)
        ttk.Button(buttons, text='决策统计', command=self.show_decision_stats).pack(side='left', padx=8)
        ttk.Button(buttons, text='打开评估报告', command=self.open_periodic_report).pack(side='left', padx=8)
        ttk.Button(buttons, text='本轮死亡统计', command=self.open_history_death_popup).pack(side='left', padx=8)
        ttk.Button(buttons, text='查看方案二', command=self.show_history_rent_rewards).pack(side='left', padx=8)
        outer.bind('<Configure>', lambda event: (caption_label.configure(wraplength=max(100, event.width - 30)),
                                                pair_label.configure(wraplength=max(100, event.width - 30))))

    def make_training_charts(self, parent, location, height):
        frame = ttk.Frame(parent)
        frame.pack(fill='both' if location != 'comparison' else 'x', expand=location != 'comparison', pady=(5, 0))
        if location != 'comparison':
            frame.rowconfigure(0, weight=1)
        for col, (metric, title) in enumerate((('rate', '训练通关率（%，每 1000 局）'), ('rents', '训练平均交租（每 1000 局）'))):
            frame.columnconfigure(col, weight=1, uniform='trend')
            box = ttk.LabelFrame(frame, text=title, padding=3)
            self.trend_boxes[(location, metric)] = box
            box.grid(row=0, column=col, sticky='nsew', padx=(0, 4) if col == 0 else (4, 0))
            canvas = tk.Canvas(box, height=height, background='white', highlightthickness=0)
            canvas.pack(fill='both', expand=True)
            canvas.bind('<Configure>', lambda event, m=metric, c=canvas: self.draw_training_chart(c, m))
            canvas.bind('<Button-1>', lambda event, m=metric, c=canvas: self.training_chart_click(c, m, event.x))
            canvas.bind('<Motion>', lambda event, m=metric, c=canvas: self.training_chart_hover(c, m, event.x))
            self.trend_canvases[(location, metric)] = canvas
        if not hasattr(self, 'trend_caption'):
            self.trend_caption = tk.StringVar(value='滚动窗口；末点含最新已完成对局。')
        controls = ttk.Frame(parent)
        if location != 'comparison':
            controls.pack(side='bottom', before=frame, fill='x', pady=(3, 0))
        else:
            controls.pack(fill='x', pady=(3, 0))
        for title, value in (('全部', 'all'), ('最近 1 万局', '10000'), ('最近 5 万局', '50000')):
            ttk.Radiobutton(controls, text=title, variable=self.trend_range, value=value).pack(side='left', padx=(0, 5))
        ttk.Label(controls, text='滚动最近').pack(side='left')
        ttk.Combobox(controls, textvariable=self.trend_window, state='readonly', width=6,
                     values=('1000', '2000', '5000', '10000')).pack(side='left', padx=3)
        ttk.Label(controls, text='局').pack(side='left', padx=(0, 8))
        if location == 'live':
            ttk.Button(controls, text='四图总览', command=self.open_chart_zoom).pack(side='right', padx=(5, 0))
        ttk.Label(controls, textvariable=self.trend_caption, foreground='#52606d', wraplength=350).pack(side='left', fill='x', expand=True)

    def apply_column_settings(self, settings):
        self.column_settings = normalize_column_settings(settings)
        for name, default, _ in HISTORY_COLUMNS:
            self.history_table.heading(name, text=self.column_settings['names'].get(name, default))
        shown = [name for name in self.column_settings['order'] if name not in self.column_settings['hidden']]
        self.history_table.configure(displaycolumns=shown)

    def open_column_settings(self):
        ColumnSettingsDialog(self)

    def refresh_comparison(self):
        directory = self.selected_run()
        if self.route in ('baseline', 'grouped', 'fresh_rules') and directory == self.runs_dir:
            directory = None
        self.history, warning = training_history(directory) if directory else ([], '')
        try:
            self.display_history = display_history(self.history)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            self.display_history = self.history[:]
            warning += f' 持续训练记录无法合并，已显示原始记录：{exc}'
        self.trend, self.trend_warning = training_trend(self.history)
        self.live_offset = 0
        self.history_table.delete(*self.history_table.get_children())
        for index, row in enumerate(self.display_history):
            mixed = row.get('mixed_fields', set())
            reward_name = {'fixed': '方案 1', 'progressive': '方案 2', 'win_potential': '方案 3',
                           'progress_potential': '方案 4'}.get(row['reward_mode'], '未知')
            if 'reward_mode' in mixed:
                reward_name = '多种'
            elif row['reward_mode'] == 'progressive':
                if mixed.intersection(('progressive_rents', 'progressive_loss', 'progressive_win')):
                    reward_name = '方案 2（多组）'
                elif any(row.get(key) is None for key in ('progressive_rents', 'progressive_loss', 'progressive_win')):
                    reward_name = '方案 2（未知）'
                elif (row['progressive_rents'] != DEFAULT_PROGRESSIVE_RENTS or
                      row['progressive_loss'] != DEFAULT_PROGRESSIVE_LOSS or row['progressive_win'] != DEFAULT_PROGRESSIVE_WIN):
                    reward_name = '方案 2（调整）'
            training = row['training']
            train_result = (performance_display(training['average_rents'], training['wins'], training['valid'])
                            if training else '未记录')
            initial_result = evaluation_display(row['initial']) if row['games'] else '未评估'
            before_result = evaluation_display(row['before']) if row['games'] else '未评估'
            after_result = performance_display(row['rents'], row['wins'], row['valid']) if row['games'] else '未评估'
            round_games = f"{row['round_games']}*" if row.get('group_partial') else row['round_games']
            thread_value = '多种' if 'threads' in mixed else row['threads'] if row['threads'] is not None else '未记录'
            toggle_value = '多种' if 'toggle_reward' in mixed else f"{row['toggle_reward']:+g}"
            allow_value = ('多种' if mixed.intersection(('allow_item_toggle', 'limited_item_toggles', 'item_toggle_allowlist'))
                           else toggle_scope_label(row['allow_item_toggle'], row.get('limited_item_toggles', False)))
            self.history_table.insert('', 'end', iid=str(index), values=(row['total'], round_games, train_result,
                initial_result, before_result, after_result,
                optional_elapsed(row['training_seconds']), optional_elapsed(row['elapsed_seconds']),
                thread_value, toggle_value, reward_name, allow_value, row['seed'] if row['seed'] is not None else '未知',
                '规则·完整候选' if row.get('candidate_rules_version') == 'candidate_rules_v2' else
                '规则·16候选' if row.get('candidate_rules_version') else '旧版',
                '完整状态 v1' if row.get('item_state_version') else '旧版输入'))
        hashes = {row['simulator'] for row in self.history if row['simulator']}
        initial_name = ('基线初始模型' if self.route == 'baseline' else
                        '分组起点：从零训练 19000 局的模型' if self.route == 'grouped' else '随机初始模型')
        caption = (f'初始模型评估指{initial_name}；训练数据来自探索过程，评估数据来自独立测试。'
                   '选中一轮可查看同种子前后对比。')
        if self.route == 'fresh_rules':
            caption = ('仅显示所选模型的来源历史；回滚后其他尝试不会混入图表。初始评估对应这条历史的起点。\n'
                       '训练数据来自探索过程，评估来自独立测试；选中一轮可查看同种子前后对比。')
            selected = self.selected_run()
            if selected == self.runs_dir:
                self.origin_caption.set('全新模型 · 0 局起步 · 本规则独立实验')
                self.latest_summary.set('准备好后点击“开始持续训练”。')
            elif self.history:
                self.origin_caption.set(self.saved_model_label(selected, self.history[-1]['total']))
        elif self.route == 'baseline' and self.selected_run() == self.runs_dir:
            assessed = latest_evaluations(self.runs_dir/'_initial').get('after_continuation')
            self.origin_caption.set('最新 Rust 规则 · 重新模仿手工策略 · 完整候选与物品状态 · 独立训练')
            if assessed:
                result = assessed['result']
                self.latest_summary.set(f'手工模仿起点评估：{result["games"]} 局，通关率 {result["win_rate"]:.2%}，平均交租 {result["avg_rents"]:.3f} 次；尚未进行 PPO 续训。')
        if any(row.get('grouped_continuous') for row in self.display_history):
            caption += '持续训练每 1 万局合并显示，* 表示尚未满 1 万局；仍每 1000 局保存。'
        evaluation_sets = {(row['seed'], row['games']) for row in self.history if row['games']}
        if len(evaluation_sets) > 1:
            caption += '历史轮次的评估种子或局数不同，请谨慎比较。'
        elif evaluation_sets and next(iter(evaluation_sets))[0] is not None:
            caption += '所显示的各轮使用同一批评估种子。'
        if any(row['valid'] != row['games'] for row in self.history):
            caption += '胜率按有效局计算，平均交租包含被截断的局。'
        if any(row['training'] and row['training']['truncated'] for row in self.history):
            caption += '训练平均交租包含截断局，胜场分母不含截断局。'
        if any(any(isinstance(ev, dict) and ev.get('valid_games', ev.get('games')) != ev.get('games')
                   for ev in (row['initial'], row['before'])) for row in self.history):
            caption += '初始或续训前评估的胜率也按有效局计算。'
        if len(hashes) > 1:
            caption += '部分历史轮次的模拟器版本不同。'
        if len({row.get('item_state_version') for row in self.history}) > 1:
            caption += '已按物品状态输入版本分开合并；旧模型评估仍使用自身原有输入。'
        if any(row.get('rules_upgraded') for row in self.history):
            caption += '新阶段已禁止物品和精华重掷，旧记录保留原规则。'
        if len({row['reward_mode'] for row in self.history}) > 1:
            caption += '历史轮次使用的训练奖励可能不同。'
        if len({(row.get('progressive_rents'), row.get('progressive_loss'), row.get('progressive_win'))
                for row in self.history if row['reward_mode'] == 'progressive'}) > 1:
            caption += '方案二的奖励数值在不同轮次有变化。'
        if len({(row['allow_item_toggle'], row.get('limited_item_toggles', False), row.get('item_toggle_allowlist'))
                for row in self.history}) > 1:
            caption += '历史轮次的主动开关权限不同。'
        if warning:
            caption += ' ' + warning
        if self.trend_warning:
            caption += ' ' + self.trend_warning
        self.comparison_caption.set(caption)
        if self.display_history:
            iid = str(len(self.display_history) - 1)
            self.history_table.selection_set(iid)
            self.history_table.see(iid)
        self.update_selected_comparison()
        self.draw_training_charts()
        if self.experiments:
            self.experiments.refresh_status()
        if self.notebook.select() == str(self.outcomes_tab):self.outcomes.refresh()

    def selected_history_index(self):
        selected = self.history_table.selection()
        return int(selected[0]) if selected else None

    def update_selected_comparison(self):
        index = self.selected_history_index()
        if index is None or not 0 <= index < len(self.display_history):
            self.pair_summary.set('暂无可对比的评估结果。')
        else:
            row = self.display_history[index]
            before = row['before']
            training = row['training']
            details = (f"本轮训练 {training['games']} 局，胜 {training['wins']}/{training['valid']}，平均交租 {training['average_rents']:.3f} 次；"
                       if training else f"本轮训练 {row['round_games']} 局，详细训练统计未记录；")
            details += f"训练耗时 {optional_elapsed(row['training_seconds'])}，总耗时 {optional_elapsed(row['elapsed_seconds'])}。\n"
            if row.get('candidate_policy_upgraded'):
                details += '本行包含首次候选表达升级；该阶段的续训前评估对应升级完成但尚未续训的模型。\n'
            if row.get('candidate_capacity_upgraded'):
                details += '本行已扩充信用卡等效果的候选容量；已学权重和优化器状态完整保留，旧动作编号不变。\n'
            if row.get('item_state_upgraded'):
                details += '本行首次补齐物品存款、触发计数与剩余进度；原权重和优化器完整保留，新增输入从零影响开始学习。\n'
            if row.get('rules_upgraded'):
                details += '本行已修正重掷规则：只有符号候选可重掷；旧阶段数据按原规则保留。\n'
            if row.get('grouped_continuous'):
                details += (f"持续训练第 {row['group_start']}～{row['total']} 局合并；打开报告将查看最后一个保存阶段。")
                if row['group_checkpoint_total'] != row['total']:
                    details += f"该保存阶段截止累计 {row['group_checkpoint_total']} 局。"
                details += '\n'
            try:
                if before is None:
                    raise ValueError('missing before evaluation')
                if row['rate'] is None or row['rents'] is None:
                    raise ValueError('missing after evaluation')
                valid = int(before.get('valid_games', before['games']))
                rate = int(before['wins']) / valid * 100
                rents = float(before['avg_rents'])
                self.pair_summary.set(details + f"本轮同种子评估：训练前胜率 {rate:.2f}%（{before['wins']}/{valid}），平均交租 {rents:.3f} 次  →  训练后胜率 {row['rate']:.2f}%（{row['wins']}/{row['valid']}），平均交租 {row['rents']:.3f} 次\n变化：胜率 {row['rate'] - rate:+.2f} 个百分点，平均交租 {row['rents'] - rents:+.3f} 次。")
            except (TypeError, KeyError, ValueError, ZeroDivisionError):
                if row['rate'] is not None and row['rents'] is not None:
                    source = row.get('evaluation_source') or '训练后评估'
                    self.pair_summary.set(details + f"{source}：胜率 {row['rate']:.2f}%（{row['wins']}/{row['valid']}），平均交租 {row['rents']:.3f} 次；没有训练前评估。")
                else:
                    self.pair_summary.set(details + '这一轮没有评估结果。')
        for metric in ('rate', 'rents'):
            self.draw_chart(metric)
            if metric in self.extra_eval_canvases:
                self.draw_chart(metric, self.extra_eval_canvases[metric])

    def draw_chart(self, metric, canvas=None):
        canvas = canvas or self.canvases[metric]
        if self.experiments and self.experiments.draw_pair(canvas, metric, training=False):
            return
        key = metric if canvas is self.canvases[metric] else (metric, str(canvas))
        canvas.delete('all')
        width, height = max(canvas.winfo_width(), 100), max(canvas.winfo_height(), 100)
        left, right, top, bottom = 50, width - 18, 15, height - 32
        self.chart_points[key] = []
        available = [(index, row) for index, row in enumerate(self.display_history) if row[metric] is not None]
        if not available:
            canvas.create_text(width / 2, height / 2, text='暂无数据', fill='#718096')
            return
        maximum = max(row[metric] for _, row in available)
        ceiling = max(1.0 if metric == 'rate' else 1.0, math.ceil(maximum * 1.15 * (10 if metric == 'rate' else 1)) / (10 if metric == 'rate' else 1))
        for i in range(5):
            y = bottom - (bottom - top) * i / 4
            canvas.create_line(left, y, right, y, fill='#e8edf2')
            canvas.create_text(left - 6, y, text=f'{ceiling * i / 4:.2f}' if metric == 'rate' else f'{ceiling * i / 4:.1f}', anchor='e', fill='#667580')
        totals = [row['total'] for _, row in available]
        low, high = min(totals), max(totals)
        points = []
        for index, row in available:
            x = (left + right) / 2 if high == low else left + (right - left) * (row['total'] - low) / (high - low)
            y = bottom - (bottom - top) * row[metric] / ceiling
            points.append((x, y))
            self.chart_points[key].append((x, index))
        color = '#2674d9' if metric == 'rate' else '#168a65'
        if len(points) > 1:
            canvas.create_line(*[coordinate for point in points for coordinate in point], fill=color, width=2)
        selected = self.selected_history_index()
        for point_index, (x, y) in enumerate(points):
            radius = 5 if selected == available[point_index][0] else 3
            canvas.create_oval(x-radius, y-radius, x+radius, y+radius, fill=color, outline='white')
        self.annotate_chart(canvas,points,[row[metric] for _,row in available],metric,left,right,top,bottom,low,high,height)
        canvas.create_text(left, height - 10, text=str(low), anchor='w', fill='#667580')
        canvas.create_text(right, height - 10, text=f'{high} 局', anchor='e', fill='#667580')

    def draw_training_charts(self):
        if self.trend.total:
            recent = self.trend.rolling_points(1000)[-1]
            rate = f'{recent["rate"]:.2f}%' if recent['rate'] is not None else '暂无有效局'
            self.rolling_summary.set(f'最近 {recent["count"]} 局 · 通关率 {rate} · 平均交租 {recent["rents"]:.3f} 次')
        else:self.rolling_summary.set('最近 1000 局：暂无训练数据')
        window = int(self.trend_window.get())
        for (_, metric), box in self.trend_boxes.items():
            title = '训练通关率（%）' if metric == 'rate' else '训练平均交租'
            box.configure(text=f'{title}（滚动最近 {window} 局）')
        for (_, metric), canvas in self.trend_canvases.items():
            self.draw_training_chart(canvas, metric)

    def trend_setting_changed(self, *_):
        self.trend_caption.set('每 100 局采样显示，末点含最新已完成对局。')
        self.draw_training_charts()

    def draw_training_chart(self, canvas, metric):
        if self.experiments and self.experiments.draw_pair(canvas, metric, training=True):
            return
        canvas.delete('all')
        width, height = max(canvas.winfo_width(), 100), max(canvas.winfo_height(), 60)
        left, right, top, bottom = 48, width - 15, 10, height - 26
        window = int(self.trend_window.get())
        bins = self.trend.rolling_points(window)
        games_limit = {'10000': 10000, '50000': 50000}.get(self.trend_range.get())
        visible = [p for p in bins if not games_limit or p['total'] > self.trend.total - games_limit]
        base = max(0, int(self.history[0]['total']) - int(self.history[0].get('round_games', self.history[0]['total']))) if self.history else 0
        points = [dict(p, total=p['total'] + base) for p in visible if p[metric] is not None]
        self.trend_points[(str(canvas), metric)] = []
        if not points:
            canvas.create_text(width / 2, height / 2, text='训练完成对局后显示滚动表现', fill='#718096')
            return
        maximum = max(p[metric] for p in points)
        ceiling = max(1.0, math.ceil(maximum * 1.15 * (10 if metric == 'rate' else 1)) / (10 if metric == 'rate' else 1))
        for i in range(3):
            y = bottom - (bottom - top) * i / 2
            canvas.create_line(left, y, right, y, fill='#e8edf2')
            canvas.create_text(left - 5, y, text=f'{ceiling * i / 2:.1f}', anchor='e', fill='#667580')
        low, high = points[0]['total'], points[-1]['total']
        coordinates = []
        for p in points:
            x = (left + right) / 2 if high == low else left + (right - left) * (p['total'] - low) / (high - low)
            y = bottom - (bottom - top) * p[metric] / ceiling
            coordinates.append((x, y))
            self.trend_points[(str(canvas), metric)].append((x, p))
        color = '#c5612f' if metric == 'rate' else '#7454ad'
        if len(coordinates) > 1:
            canvas.create_line(*[v for point in coordinates for v in point], fill=color, width=2)
        for x, y in coordinates:
            canvas.create_oval(x-2, y-2, x+2, y+2, fill=color, outline=color)
        self.annotate_chart(canvas,coordinates,[p[metric] for p in points],metric,left,right,top,bottom,low,high,height)
        canvas.create_text(left, height - 9, text=str(low), anchor='w', fill='#667580')
        canvas.create_text(right, height - 9, text=f'{high} 局', anchor='e', fill='#667580')

    @staticmethod
    def annotate_chart(canvas,coordinates,values,metric,left,right,top,bottom,low,high,height):
        if not coordinates:return
        count=min(6,max(1,int((right-left)/100)),len(coordinates))
        indices=sorted({round(i*(len(coordinates)-1)/max(1,count-1)) for i in range(count)})
        for index in indices:
            x,y=coordinates[index]
            label=f'{values[index]:.2f}%' if metric=='rate' else f'{values[index]:.2f}'
            canvas.create_text(min(right-20,max(left+20,x)),max(top+7,y-13),text=label,fill='#344c67',font=('Microsoft YaHei UI',9))
        if high>low:
            for i in (1,2,3):
                x=left+(right-left)*i/4
                canvas.create_line(x,top,x,bottom,fill='#eef2f7',dash=(2,4))
                canvas.create_text(x,height-10,text=f'{round(low+(high-low)*i/4):,}',fill='#718096',font=('Microsoft YaHei UI',8))

    def training_chart_click(self, canvas, metric, x):
        points = self.trend_points.get((str(canvas), metric), [])
        if points:
            _, p = min(points, key=lambda item: abs(item[0] - x))
            rate = f"{p['rate']:.2f}%" if p['rate'] is not None else '无有效局'
            self.trend_caption.set(f"第 {p['total'] - p.get('count', int(self.trend_window.get())) + 1}～{p['total']} 局：训练通关率 {rate}，平均交租 {p['rents']:.3f} 次（有效局 {p['valid']}）。")

    def training_chart_hover(self, canvas, metric, x):
        points = self.trend_points.get((str(canvas), metric), [])
        if points and min(abs(item[0] - x) for item in points) <= 10:
            self.training_chart_click(canvas, metric, x)

    @staticmethod
    def make_death_table(parent, height=13):
        frame = ttk.Frame(parent)
        frame.pack(fill='both', expand=True, pady=(4, 0))
        table = ttk.Treeview(frame, columns=('stage', 'losses', 'percentage', 'failure_share'), show='headings', height=height)
        for name, label, width in (('stage', '失败前成功交租', 160), ('losses', '失败局数', 80),
                                   ('percentage', '占全部记录', 100), ('failure_share', '占失败局', 100)):
            table.heading(name, text=label)
            table.column(name, width=width, minwidth=70, anchor='center')
        scrollbar = ttk.Scrollbar(frame, orient='vertical', command=table.yview)
        table.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        table.pack(side='left', fill='both', expand=True)
        return table

    @staticmethod
    def populate_death_table(table, distribution):
        table.delete(*table.get_children())
        failures = sum(distribution.losses.values())
        for stage in range(13):
            label = '0 次（首次交租前）' if stage == 0 else '12 次及以上' if stage == 12 else f'{stage} 次'
            count = distribution.losses[stage]
            ratio = f'{count / distribution.total * 100:.2f}%' if distribution.total else '—'
            failure_ratio = f'{count / failures * 100:.2f}%' if failures else '—'
            table.insert('', 'end', values=(label, count, ratio, failure_ratio))

    def render_live_deaths(self):
        d = self.session_deaths
        self.death_summary.set(f'本次已记录 {d.total} 局：失败 {sum(d.losses.values())}，通关 {d.wins}，截断 {d.truncated}，无法分类 {d.unknown}。'
                               + (f' {self.death_warning}' if self.death_warning else ''))
        self.populate_death_table(self.death_table, d)
        if self.death_popup and self.death_popup.winfo_exists():
            self.death_popup_refresh()

    def refresh_live_deaths(self):
        if not self.current_dir or self.death_warning:
            return
        path = self.current_dir / 'training_games.jsonl'
        try:
            with path.open(encoding='utf-8') as source:
                source.seek(self.death_offsets.get(path, 0))
                while True:
                    position = source.tell()
                    line = source.readline()
                    if not line or not line.endswith('\n'):
                        self.death_offsets[path] = position
                        break
                    self.session_deaths.add(json.loads(line))
                    self.death_offsets[path] = source.tell()
            self.render_live_deaths()
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self.death_warning = f'部分记录读取失败：{exc}'
            self.render_live_deaths()

    def open_live_death_popup(self):
        if self.death_popup and self.death_popup.winfo_exists():
            self.death_popup.lift()
            return
        self.show_death_popup(lambda: self.session_deaths, '本次训练的死亡阶段', live=True)

    def open_history_death_popup(self):
        index = self.selected_history_index()
        if index is None or not 0 <= index < len(self.display_history):
            messagebox.showinfo('暂无数据', '请先在表格中选择一轮训练。', parent=self.root)
            return
        row = self.display_history[index]
        try:
            distribution = history_death_distribution(row)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            messagebox.showerror('无法读取逐局记录', str(exc), parent=self.root)
            return
        start = row.get('group_start', row['total'] - row['round_games'] + 1)
        self.show_death_popup(lambda: distribution, f"所选训练：第 {start}～{row['total']} 局")

    def show_death_popup(self, get_distribution, title, live=False):
        window = tk.Toplevel(self.root)
        window.title(title)
        window.geometry('920x600')
        remember_popup(self, window, 'death_distribution')
        window.minsize(720, 450)
        outer = ttk.Frame(window, padding=12)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text=title, font=('Microsoft YaHei UI', 13, 'bold')).pack(anchor='w')
        summary = tk.StringVar()
        ttk.Label(outer, textvariable=summary, wraplength=850).pack(anchor='w', pady=(5, 3))
        ttk.Label(outer, text='按失败前成功交租次数分组；表格显示占全部记录和失败局的比例，图中比例以全部记录为分母。12 次及以上合并；通关与截断不计入失败。',
                  foreground='#52606d', wraplength=850).pack(anchor='w', pady=(0, 4))
        content = ttk.Frame(outer)
        content.pack(fill='both', expand=True)
        left = ttk.Frame(content)
        left.pack(side='left', fill='both', expand=True)
        table = self.make_death_table(left)
        chart = tk.Canvas(content, background='white', highlightthickness=0, width=400)
        chart.pack(side='left', fill='both', expand=True, padx=(12, 0))
        def redraw(event=None):
            d = get_distribution()
            summary.set(f'已记录 {d.total} 局  |  失败 {sum(d.losses.values())} 局  |  通关 {d.wins} 局  |  截断 {d.truncated} 局  |  无法分类 {d.unknown} 局'
                        + (f'  |  {self.death_warning}' if live and self.death_warning else ''))
            self.populate_death_table(table, d)
            chart.delete('all')
            width, height = max(chart.winfo_width(), 220), max(chart.winfo_height(), 200)
            step = max(1, (height - 22) / 13)
            maximum = max([1, *d.losses.values()])
            for stage in range(13):
                y = 8 + stage * step
                count = d.losses[stage]
                label = '12+' if stage == 12 else str(stage)
                chart.create_text(6, y + step / 2, text=label, anchor='w', fill='#52606d')
                bar_width = max(1, width - 125) * count / maximum
                if count:
                    chart.create_rectangle(36, y + 2, 36 + bar_width, y + max(3, step - 2), fill='#c47754', outline='')
                ratio = f'{count / d.total * 100:.1f}%' if d.total else '—'
                chart.create_text(width - 4, y + step / 2, text=f'{count} 局  {ratio}', anchor='e', fill='#374957')
        chart.bind('<Configure>', redraw)
        if live:
            self.death_popup = window
            self.death_popup_refresh = redraw
            def close():
                self.death_popup = None
                window.destroy()
            window.protocol('WM_DELETE_WINDOW', close)
        redraw()

    def refresh_live_trend(self):
        if not self.current_dir or self.trend_warning:
            return
        path = self.current_dir / 'training_games.jsonl'
        try:
            with path.open(encoding='utf-8') as source:
                source.seek(self.live_offset)
                while True:
                    previous = source.tell()
                    line = source.readline()
                    if not line or not line.endswith('\n'):
                        self.live_offset = previous
                        break
                    self.trend.add(json.loads(line))
                    self.live_offset = source.tell()
            self.draw_training_charts()
            if self.notebook.select() == str(self.outcomes_tab):self.outcomes.refresh()
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            self.trend_warning = f'运行中的训练走势无法读取：{exc}'
            self.trend_caption.set(self.trend_warning)

    def chart_click(self, metric, x, canvas=None):
        key = metric if canvas is None or canvas is self.canvases[metric] else (metric, str(canvas))
        points = self.chart_points.get(key, [])
        if points:
            _, index = min(points, key=lambda point: abs(point[0] - x))
            self.history_table.selection_set(str(index))
            self.history_table.see(str(index))

    def evaluation_chart_hover(self, canvas, metric, x):
        canvas.delete('hover')
        key = metric if canvas is self.canvases[metric] else (metric, str(canvas))
        points = self.chart_points.get(key, [])
        if not points:
            return
        xpos, index = min(points, key=lambda item: abs(item[0] - x))
        if abs(xpos - x) > 10:
            return
        row = self.display_history[index]
        label = f"累计 {row['total']} 局：评估胜率 {row['rate']:.2f}%，平均交租 {row['rents']:.3f}"
        item = canvas.create_text(max(50, canvas.winfo_width() // 2), 2, text=label,
                                  anchor='n', fill='#243747', tags='hover')
        bbox = canvas.bbox(item)
        if bbox:
            background = canvas.create_rectangle(*bbox, fill='white', outline='#b5c4cf', tags='hover')
            canvas.tag_lower(background, item)

    def open_chart_zoom(self):
        self.notebook.select(self.charts_tab)

    def show_decision_stats(self):
        points = self.trend.points[-10:]
        games = sum(p.get('action_games', 0) for p in points)
        if not games:
            messagebox.showinfo('决策统计', '最近完整训练记录中没有可用的决策统计。', parent=self.root)
            return
        counts = Counter()
        for point in points:
            counts.update(point.get('actions', {}))
        earlier = self.trend.points[-20:-10]
        earlier_games = sum(p.get('action_games', 0) for p in earlier)
        earlier_counts = Counter()
        for point in earlier:
            earlier_counts.update(point.get('actions', {}))
        window = tk.Toplevel(self.root)
        window.title('最近训练的决策统计')
        window.geometry('700x350')
        remember_popup(self, window, 'decision_statistics')
        start = points[0]['total'] - TREND_WINDOW + 1
        end = points[-1]['total']
        ttk.Label(window, text=f'第 {start}～{end} 局训练：有动作记录 {games} 局', font=('Microsoft YaHei UI', 12, 'bold')).pack(anchor='w', padx=15, pady=(15, 4))
        ttk.Label(window, text=f'每 100 局使用次数：蓝色为最近一段，灰色为前一段（有动作记录 {earlier_games} 局）；训练包含探索。', foreground='#52606d').pack(anchor='w', padx=15)
        chart = tk.Canvas(window, background='white', highlightthickness=0)
        chart.pack(fill='both', expand=True, padx=15, pady=10)
        kinds = [('reroll', '重掷'), ('remove', '删除符号'), ('toggle_item', '开关物品'), ('destroy_item', '消除物品')]
        def draw(event=None):
            chart.delete('all')
            width = max(chart.winfo_width(), 200)
            maximum = max(1, *(counts[key] * 100 / games for key, _ in kinds),
                          *(earlier_counts[key] * 100 / earlier_games for key, _ in kinds) if earlier_games else (0,))
            for index, (key, label) in enumerate(kinds):
                y = 15 + index * 45
                value = counts[key] * 100 / games
                previous = earlier_counts[key] * 100 / earlier_games if earlier_games else None
                chart.create_text(4, y + 12, text=label, anchor='w')
                chart.create_rectangle(85, y, 85 + max(1, width - 240) * value / maximum, y + 10, fill='#548fbb', outline='')
                if previous is not None:
                    chart.create_rectangle(85, y + 13, 85 + max(1, width - 240) * previous / maximum, y + 23, fill='#a8b4bb', outline='')
                previous_text = f'{previous:.1f}' if previous is not None else '无记录'
                chart.create_text(width - 4, y + 12, text=f'{value:.1f} / {previous_text}', anchor='e')
        chart.bind('<Configure>', draw)

    def open_history_report(self):
        index = self.selected_history_index()
        report = self.display_history[index]['directory'] / '报告.html' if index is not None and 0 <= index < len(self.display_history) else None
        if report and report.is_file():
            webbrowser.open(report.resolve().as_uri())
        else:
            messagebox.showinfo('报告尚不可用', '所选轮次没有报告。', parent=self.root)

    def open_periodic_report(self):
        index = self.selected_history_index()
        row = self.display_history[index] if index is not None and 0 <= index < len(self.display_history) else None
        if row and row.get('group_checkpoint_total', row['total']) == row['total']:
            registered = latest_evaluations(row['directory']).get('after_continuation')
            if registered and Path(registered['report_path']).is_file():
                webbrowser.open(Path(registered['report_path']).resolve().as_uri())
                return
            report = row['directory'] / '定期评估报告.html'
            if report.is_file():
                webbrowser.open(report.resolve().as_uri())
                return
        messagebox.showinfo('报告尚不可用', '所选轮次还没有评估报告。', parent=self.root)

    def restore_progress_log(self):
        self.progress_history = ProgressLog(PROGRESS_DIRECTORY, self.route, self.runs_dir)
        self.progress_history_root = self.runs_dir.resolve()
        self.log.configure(state='normal')
        self.log.delete('1.0', 'end')
        if self.progress_history.lines:
            self.log.insert('end', '\n'.join(self.progress_history.lines) + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')

    def append(self, text: str):
        if self.started_at is not None:
            end = self.finished_at if self.finished_at is not None else time.monotonic()
            text = f'[{format_elapsed(end - self.started_at)}] {text}'
            if '最近 ' not in text:
                text += '  |  ' + self.rolling_summary.get()
        if getattr(self, 'progress_history_root', None) != self.runs_dir.resolve():
            self.restore_progress_log()
        self.progress_history.append(text)
        self.log.configure(state='normal')
        self.log.insert('end', text.rstrip() + '\n')
        # Text has a trailing newline; remove everything older than the last 500 logical lines.
        count = int(self.log.index('end-1c').split('.')[0]) - 1
        if count > 500:
            self.log.delete('1.0', f'{count - 500 + 1}.0')
        self.log.see('end')
        self.log.configure(state='disabled')

    def toggle_permission_changed(self, *_):
        if not self.allow_item_toggle.get() and self.limited_item_toggles.get():
            self.limited_item_toggles.set(False)

    def limited_toggle_changed(self, *_):
        if self.limited_item_toggles.get() and not self.allow_item_toggle.get():
            self.allow_item_toggle.set(True)

    def set_toggle_policy(self, allowed, limited):
        self.limited_item_toggles.set(False)
        self.allow_item_toggle.set(bool(allowed))
        self.limited_item_toggles.set(bool(allowed and limited))

    def show_toggle_list(self):
        existing = getattr(self, 'toggle_list_window', None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            return
        window = self.toggle_list_window = tk.Toplevel(self.root)
        window.title('模型可以主动开关的指定清单')
        window.geometry('650x640')
        remember_popup(self, window, 'toggle_list')
        window.minsize(580, 400)
        window.transient(self.root)
        outer = ttk.Frame(window, padding=14)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='勾选“仅限指定清单”会同时开启主动开关权限。取消此项后，恢复允许开关所有符合游戏规则的物品和精华；取消总开关则全部禁止。',
                  wraplength=590).pack(fill='x', pady=(0, 12))
        lists = ttk.Frame(outer)
        lists.pack(fill='both', expand=True)
        lists.rowconfigure(0, weight=1)
        for column, (title, entries) in enumerate((('物品', ITEM_TOGGLES), ('精华', ESSENCE_TOGGLES))):
            lists.columnconfigure(column, weight=1, uniform='toggle_list')
            frame = ttk.LabelFrame(lists, text=f'{title}（{len(entries)}）', padding=6)
            frame.grid(row=0, column=column, sticky='nsew', padx=(0, 8) if column == 0 else 0)
            box = tk.Listbox(frame, activestyle='none', exportselection=False, height=15, borderwidth=0)
            scroll = ttk.Scrollbar(frame, orient='vertical', command=box.yview)
            box.configure(yscrollcommand=scroll.set)
            scroll.pack(side='right', fill='y')
            box.pack(side='left', fill='both', expand=True)
            box.insert('end', *(name for _, name in entries))
        ttk.Label(outer, text='训练与评估均按此限制执行；使用或消除物品、交租救急、枕头选择及自动效果不受限制。每次开关奖赏沿用上方设置，同一目标每次旋转前最多开关一次。“格子棋精华”对应图鉴中的“格子旗精华”。',
                  wraplength=590, foreground='#52606d').pack(fill='x', pady=12)
        ttk.Button(outer, text='关闭', command=window.destroy).pack(anchor='e')

    def show_training_help(self):
        initial = ('基线初始模型：模仿手工策略的模型。' if self.route == 'baseline' else
                   '分组初始模型：已有的从零模型，每个种子尝试多局。' if self.route == 'grouped' else
                   '随机初始模型：本实验还没有训练过的模型。')
        messagebox.showinfo('训练与评估说明',
            '训练：模型探索选择，并每积累一批动作自动更新参数。持续训练每 1000 局保存，点击停止后完成当前保存阶段。\n\n'
            '评估：让已经保存的模型使用固定测试种子游玩，不会更新模型。本实验累计每 10000 局自动评估，跨多次启动累计。\n\n'
            + initial + '\n续训前模型：所选保存阶段开始时的模型。\n续训后模型：该阶段训练并保存后的模型。单独评估可选择这三类模型；所选模型的评估会更新对应的评估曲线。\n\n'
            '滚动指标：默认统计最近 1000 局，不是从本次点击开始一直累计的平均值。训练探索和固定种子评估分别显示。\n\n'
            '浏览器分析：可以查看最近训练、最近评估或自定义累计局数范围的候选排名。死亡与牌组页可按来源和最近窗口查看失败阶段与符号数量。旧记录缺失的字段不会补成零。',parent=self.root)

    def show_candidate_rules_help(self):
        messagebox.showinfo('新版候选表达',
            '每个符号、物品、精华使用独立身份，并提供游戏规则中的基础收益、效果参数、类别、描述中的关联对象和成长条件。'
            '这些是规则信息，不包含手工抓位、社区评分或最佳选择答案。\n\n'
            '所有候选共用评分网络，候选换位置时其评分一起移动。不会添加库存数量上限或超量惩罚。\n\n'
            '当前版本开放信用卡的 20 个、信用卡精华的 30 个候选，以及叠加效果。只计算实际存在的候选规则。'
            '从上一版规则模型继续时会自动扩容，完整保留已学参数和优化器，原模型文件保留。\n\n'
            '当前还提供物品与精华的存款、可取出金额、触发计数、目标次数、剩余进度和储存符号数量。'
            '剩余进度按移除次数、添加次数或旋转次数等各自规则计算，不等于一定还需几次旋转。'
            '已有规则模型首次接入这些状态时，完整保留原权重和优化器；新增输入初始影响为零，后续训练学习使用，奖励不变。\n\n'
            '旧模型首次使用时，保留已有局面与价值网络权重，改造候选评分并重建优化器，另存升级模型；之后继续正常保存优化器。'
            '这会改变模型行为，不保证升级后立即提高胜率，建议保留旧分支作对照。\n\n'
            '升级后的模型始终使用新版输入。首次升级的“续训前评估”评估升级完成但尚未继续训练的模型。', parent=self.root)

    def show_current_rules_help(self):
        messagebox.showinfo('当前规则与全新训练',
            f'当前 Rust 规则：{self.rules_version}\n\n'
            '训练和文字游玩使用同一套已修正的 Rust 游戏引擎。包括消除与移除、成长与连锁结算、'
            '符号生成落位、候选顺序和房东战等已核对的修正；仍不代表所有组合已与原版完全一致。\n\n'
            '正常训练使用进阶 20、正常开局金币。自动对照测试额外赠送的 1000 金币不会加入训练。\n\n'
            '这个规则版本从全新模型与全新优化器开始，不加载旧权重，累计训练从 0 局计算。'
            '候选身份、规则信息及物品状态已启用；奖励、线程和开关权限仍按界面设置。\n\n'
            '每个规则版本使用自己的保存目录，同版本的不同起点也各有独立实验编号。'
            '本实验内部可以继续训练；旧实验文件仍保留，不混入本页曲线与评估。\n\n'
            f'当前保存位置：{self.runs_dir}\n'
            f'此前新规则实验：{LEGACY_FRESH_RUNS}', parent=self.root)

    def selected_reward_mode(self):
        return next((mode for mode, label in REWARD_NAMES.items() if label == self.reward_choice.get()), None)

    def update_reward_caption(self):
        captions = {
            'fixed': '方案 1：前 12 次交租各 +1；击败房东 +20。',
            'progressive': '方案 2：交租 +0.5 至 +1.5、失败 −3、通关 +20；所有分数包括开关奖赏除以 20。',
            'win_potential': '方案 3：交租给阶段分，结束按胜负结算；开关为 0 时完整一局总分只取决于胜负。',
            'progress_potential': '方案 4：交租和房东伤害预支进度分，结束时收回；通关 +1。',
        }
        mode = self.selected_reward_mode()
        if mode == 'progressive':
            try:
                rents = parse_progressive_rents(self.progressive_rents_json.get())
                loss, win = validate_progressive_outcomes(float(self.progressive_loss.get()), float(self.progressive_win.get()))
                captions['progressive'] = (f'方案 2：第 6 次交租 +{rents[5]:.3f}；失败 {loss:+g}、通关 {win:+g}；'
                                           '所有分数包括开关奖赏除以 20。')
            except ValueError:
                captions['progressive'] = '方案二设置无效，请重新调整。'
        self.reward_caption.set(captions.get(mode, '请选择奖励方案。'))
        if mode in ('win_potential', 'progress_potential'):
            self.toggle_hint.set('建议先用 0；非 0 会改变整局总奖赏。')
        else:
            self.toggle_hint.set('建议先用 0；小幅试验 −0.05 至 0，输入原始分数。')

    def show_reward_help(self):
        mode = self.selected_reward_mode()
        if mode in REWARD_EXPLANATIONS:
            messagebox.showinfo('奖励方案说明', REWARD_EXPLANATIONS[mode] +
                                '\n\n切换方案后仍从所选模型继续训练。由于奖励的数值和目标可能变化，'
                                '模型需要时间适应，短期表现可能波动；建议用评估结果比较。', parent=self.root)

    def open_progressive_settings(self):
        if self.training_active:
            messagebox.showinfo('训练正在运行', '请在本轮训练结束后修改方案二。', parent=self.root)
            return
        ProgressiveSettingsDialog(self)

    def show_history_rent_rewards(self):
        index = self.selected_history_index()
        if index is None or not 0 <= index < len(self.display_history):
            messagebox.showinfo('暂无数据', '请先在表格中选择一轮训练。', parent=self.root)
            return
        row = self.display_history[index]
        parts = row.get('group_parts') or [(row['directory'], 0, row['round_games'])]
        lines = []
        try:
            for directory, start, stop in parts:
                data = json.loads((Path(directory) / 'summary.json').read_text(encoding='utf-8'))
                meta = data['manifest']
                first = int(meta.get('previous_training_games', 0)) + start + 1
                last = int(meta.get('previous_training_games', 0)) + stop
                mode = meta.get('reward_mode')
                lines.append(f'第 {first}～{last} 局 · {Path(directory).name} · {REWARD_NAMES.get(mode, "未知方案")}')
                if mode == 'progressive':
                    rents = validate_progressive_rents(meta.get('progressive_rents', DEFAULT_PROGRESSIVE_RENTS))
                    loss, win = validate_progressive_outcomes(meta.get('progressive_loss', DEFAULT_PROGRESSIVE_LOSS),
                                                              meta.get('progressive_win', DEFAULT_PROGRESSIVE_WIN))
                    lines.append('  ' + '　'.join(f'第 {k} 次：{v:.3f}' for k, v in enumerate(rents, 1)))
                    lines.append(f'  失败：{loss:+g}；通关：{win:+g}。')
                    lines.append('  原始分数；实际训练奖赏统一除以 20。')
                lines.append('')
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            messagebox.showerror('无法读取奖励设置', str(exc), parent=self.root)
            return
        window = tk.Toplevel(self.root)
        window.title('所选训练的奖励设置')
        window.geometry('850x450')
        remember_popup(self, window, 'saved_rewards')
        outer = ttk.Frame(window, padding=12)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='所选行包含的各保存阶段', font=('Microsoft YaHei UI', 12, 'bold')).pack(anchor='w')
        ttk.Label(outer, text='历史记录未单独保存方案二数值时，按当时的默认数值显示。', foreground='#52606d').pack(anchor='w', pady=(4, 8))
        text_box = tk.Text(outer, wrap='word')
        scroll = ttk.Scrollbar(outer, command=text_box.yview)
        text_box.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        text_box.pack(side='left', fill='both', expand=True)
        text_box.insert('1.0', '\n'.join(lines))
        text_box.configure(state='disabled')

    def update_elapsed(self):
        if self.started_at is not None and self.finished_at is None:
            elapsed = max(time.monotonic() - self.started_at, 0.001)
            self.elapsed_display.set(f'本轮已用时间：{format_elapsed(elapsed)}（含训练、评估和报告）')
            speed = f'{self.training_finished * 3600 / elapsed:.0f} 局/小时（含评估及保存）'
        else:
            speed = '—'
        last = self.trend.points[-1] if self.trend.points else None
        rate = f"{last['rate']:.2f}%" if last and last['rate'] is not None else '—'
        rents = f"{last['rents']:.3f}" if last else '—'
        selected = self.selected_run()
        try:
            saved = dt.datetime.fromtimestamp((selected / 'summary.json').stat().st_mtime).strftime('%m-%d %H:%M:%S') if selected else '—'
        except OSError:
            saved = '—'
        self.overview.set(f'累计记录 {self.trend.total} 局  |  最近完整 1000 局：胜率 {rate}，平均交租 {rents}  |  速度 {speed}  |  最近保存 {saved}')
        self.root.after(1000, self.update_elapsed)

    def finish_elapsed(self):
        if self.started_at is not None and self.finished_at is None:
            self.finished_at = time.monotonic()
            self.elapsed_display.set(f'本轮总耗时：{format_elapsed(self.finished_at - self.started_at)}（含训练、评估和报告）')

    def save_panel_timing(self, directory: Path):
        if self.started_at is None or self.finished_at is None or not (directory / 'summary.json').is_file():
            return
        try:
            with (directory / 'panel_timing.json').open('x', encoding='utf-8') as output:
                json.dump({'elapsed_seconds': self.finished_at - self.started_at,
                           'includes': 'training, evaluation, and report generation'}, output, ensure_ascii=False)
        except FileExistsError:
            pass
        except OSError:
            self.append('本轮总耗时未能保存到历史记录。')

    def completion_sound(self):
        try:
            if os.name == 'nt':
                import winsound
                winsound.MessageBeep(winsound.MB_OK)
            else:
                self.root.bell()
        except (OSError, RuntimeError, tk.TclError):
            pass  # A sound device failure must not hide a completed training run.

    def refresh_runs(self, select: Path | None = None):
        if self.route == 'baseline' and select and select.resolve() == (self.runs_dir/'_initial').resolve():
            select = self.runs_dir
        if (self.route in ('fresh_rules','baseline') and select
                and not select.resolve().is_relative_to(self.runs_dir.resolve())):
            messagebox.showerror('无法使用该模型',
                '这里是当前规则的独立实验区。旧模型不会载入；请选择本规则保存目录中的模型，或点击“全新起点”。',
                parent=self.root)
            return
        self.run_options = completed_runs(self.runs_dir, self.route)
        if self.route == 'baseline':
            self.run_options.append((0, self.runs_dir))  # explicit initial-model choice
        if self.route == 'grouped' and (GROUPED_SOURCE/'model_final.zip').is_file():
            self.run_options.append((0, self.runs_dir))
        if self.route == 'fresh_rules':
            self.run_options.append((0, self.runs_dir))
        if select and select.is_dir() and not any(p == select for _, p in self.run_options):
            try:
                data = json.loads((select/'summary.json').read_text(encoding='utf-8'))
                meta = data['manifest']
                compatible = matches_route(meta, self.route)
                if not compatible or meta.get('reward_mode') not in REWARD_NAMES or not (select/'model_final.zip').is_file():
                    raise ValueError('请选择当前训练路线的完整结果文件夹。')
                count = int(meta.get('previous_training_games', 0)) + int(data['training_games'])
                self.run_options.insert(0, (count, select))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                messagebox.showerror('无法使用该模型', str(exc), parent=self.root)
                return
        labels = [('最新规则 · 基线初始模型（重新模仿手工）' if self.route == 'baseline' and path == self.runs_dir
                   else '分组起点（从零训练 19000 局）' if self.route == 'grouped' and path == self.runs_dir
                   else '共同起点（未开始本分支训练）' if self.route == 'fresh_rules' and path == self.runs_dir and self.experiments and self.experiments.branch
                   else '全新开始（0 局，不加载已有模型）' if self.route == 'fresh_rules' and path == self.runs_dir
                   else self.saved_model_label(path, count) if self.route in ('fresh_rules','baseline')
                   else f'累计训练 {count} 局  ·  {path.name}') for count, path in self.run_options]
        self.run_select['values'] = labels
        if self.run_options:
            index = next((i for i, (_, path) in enumerate(self.run_options) if select and path == select), 0)
            self.run_select.current(index)
        else:
            self.choice.set('未找到可续训的完整模型')
        self.load_selected_reward()
        self.refresh_comparison()
        if self.experiments and hasattr(self.experiments, 'note_selection'):
            self.experiments.note_selection()

    def saved_model_label(self, path, count):
        try:
            meta = json.loads((path/'summary.json').read_text(encoding='utf8'))['manifest']
            meta.setdefault('model_id', save_id(path))
            return meta.get('model_label') or model_label(meta, count)
        except (OSError, ValueError, TypeError, KeyError):
            return f'累计训练 {count} 局 · {path.name}'

    def load_selected_reward(self):
        directory = self.selected_run()
        if directory and not (self.route in ('baseline', 'grouped', 'fresh_rules') and directory == self.runs_dir):
            try:
                data = json.loads((directory / 'summary.json').read_text(encoding='utf-8'))
                meta = data['manifest']
                value = meta.get('toggle_reward', 0)
                self.toggle_reward.set(f"{float(value):g}")
                self.set_toggle_policy(meta.get('allow_item_toggle', True), meta.get('limited_item_toggles', False))
                if meta.get('candidate_rules_version'):
                    self.candidate_rules.set(True)
                self.reward_choice.set(REWARD_NAMES.get(meta.get('reward_mode'), REWARD_NAMES['fixed']))
                self.update_reward_caption()
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                self.toggle_reward.set('0')
                self.set_toggle_policy(True, False)
                self.reward_choice.set(REWARD_NAMES['fixed'])
                self.update_reward_caption()

    def model_changed(self, event=None):
        self.load_selected_reward()
        self.refresh_comparison()
        if self.experiments and hasattr(self.experiments, 'note_selection'):
            self.experiments.note_selection()

    def select_fresh_start(self):
        if self.route == 'fresh_rules' and not self.training_active:
            self.refresh_runs(select=self.runs_dir)
            self.status.set('已选择全新起点，开始时创建新模型')

    def import_release_checkpoint(self):
        if self.route != 'fresh_rules' or self.training_active:
            return
        bundle = filedialog.askopenfilename(parent=self.root, title='选择从 GitHub Release 下载的模型包',
                                            filetypes=[('模型包', '*.zip')])
        if not bundle:
            return
        try:
            result = subprocess.run([python_executable(), '-X', 'utf8', str(ROOT/'python/published_checkpoint.py'),
                                     bundle, '--runs-dir', str(self.runs_dir)], capture_output=True,
                                    text=True, encoding='utf8', timeout=45,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode:
                raise ValueError(result.stderr.strip().splitlines()[-1] if result.stderr.strip() else '导入失败')
            directory = Path(result.stdout.strip())
            meta = json.loads((directory/'summary.json').read_text(encoding='utf8'))['manifest']
            self.refresh_runs(select=directory)
            self.reward_choice.set(REWARD_NAMES[meta['reward_mode']])
            self.toggle_reward.set(str(meta['toggle_reward']))
            self.allow_item_toggle.set(meta['allow_item_toggle'])
            self.limited_item_toggles.set(meta['limited_item_toggles'])
            if meta['reward_mode'] == 'progressive':
                self.progressive_rents_json.set(json.dumps(meta['progressive_rents']))
                self.progressive_loss.set(str(meta['progressive_loss']))
                self.progressive_win.set(str(meta['progressive_win']))
            self.update_reward_caption()
            self.experiments.note_selection()
            self.status.set('发布模型已导入。可直接评估或继续训练。')
            self.append(f'已导入累计 {meta["origin_training_games"]:,} 局的发布模型；本地训练记录从续训开始。')
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
            messagebox.showerror('无法导入模型', str(exc), parent=self.root)

    def select_run(self):
        directory = filedialog.askdirectory(parent=self.root, initialdir=str(self.runs_dir if self.runs_dir.exists() else RUNS), title='选择已有训练结果文件夹')
        if directory:
            self.refresh_runs(Path(directory).resolve())

    def selected_run(self) -> Path | None:
        index = self.run_select.current()
        return self.run_options[index][1] if 0 <= index < len(self.run_options) else None

    def restore_preferences(self):
        stored = read_preferences(PREFERENCE_SETTINGS)['routes'].get(self.route, {})
        if not isinstance(stored, dict):
            return
        for name in PREFERENCE_FIELDS:
            value = valid_preference(name, stored.get(name))
            if value is not None:
                getattr(self, name).set(value)
        if not self.allow_item_toggle.get():
            self.limited_item_toggles.set(False)
        self.update_reward_caption()

    def schedule_preference_save(self, *_):
        self.preferences_dirty = True
        if self.preference_job is not None:
            self.root.after_cancel(self.preference_job)
        self.preference_job = self.root.after(400, self.flush_preferences)

    def flush_preferences(self):
        if self.preference_job is not None:
            self.root.after_cancel(self.preference_job)
            self.preference_job = None
        if not self.preferences_dirty:
            return
        try:
            values = {name: getattr(self, name).get() for name in PREFERENCE_FIELDS}
            if self.experiments and self.experiments.branch:
                locked = {'reward_choice', 'toggle_reward', 'allow_item_toggle', 'limited_item_toggles',
                          'progressive_rents_json', 'progressive_loss', 'progressive_win'}
                values = {key:value for key,value in values.items() if key not in locked}
            save_preferences(PREFERENCE_SETTINGS, self.route, values)
            self.preferences_dirty = False
        except (OSError, tk.TclError) as exc:
            self.append(f'面板偏好未能保存：{exc}')

    def thread_setting_changed(self, *_):
        if not self.training_active or not self.continuous_mode or self.stop_requested.is_set():
            return
        try:
            value = int(self.compute_threads.get().strip())
            if not 1 <= value <= 64:
                raise ValueError
        except ValueError:
            self.thread_input_invalid = True
        else:
            self.thread_input_invalid = False
            with self.thread_config_lock:
                self.requested_threads = value
        self.update_thread_hint()

    def update_thread_hint(self):
        if not self.training_active or not self.continuous_mode:
            self.thread_hint.set(self.base_thread_hint)
            return
        with self.thread_config_lock:
            pending = self.requested_threads
        if self.stop_requested.is_set():
            text = f'正在停止；当前阶段使用 {self.current_stage_threads} 个线程。'
        elif self.thread_input_invalid:
            text = f'线程数请输入 1–64 的整数；当前阶段 {self.current_stage_threads} 个，下阶段仍使用 {pending} 个。'
        elif pending != self.current_stage_threads:
            text = f'当前阶段使用 {self.current_stage_threads} 个线程；保存后下一阶段自动切换为 {pending} 个。'
        else:
            text = f'当前阶段使用 {self.current_stage_threads} 个线程；修改后将在保存后的下一阶段生效。'
        self.thread_hint.set(text)

    def set_busy(self, busy: bool):
        self.training_active = busy
        normal = 'disabled' if busy else 'normal'
        self.start_button.configure(state=normal)
        self.manual_eval_button.configure(state=normal)
        if self.candidate_rules_check:
            self.candidate_rules_check.configure(state=normal)
        if self.fresh_button:
            self.fresh_button.configure(state=normal)
        if getattr(self, 'import_button', None):
            self.import_button.configure(state=normal)
        self.stop_button.configure(state='normal' if busy else 'disabled')
        for widget in (self.refresh_button, self.choose_button, self.games_entry, self.eval_entry, self.threads_entry, self.toggle_entry, self.toggle_permission, self.limited_toggle_permission, self.progressive_button):
            widget.configure(state=normal)
        if self.route != 'grouped':
            self.periodic_check.configure(state=normal)
            self.periodic_entry.configure(state=normal)
        if busy and self.continuous_mode and not self.stop_requested.is_set():
            self.threads_entry.configure(state='normal')
        self.run_select.configure(state='disabled' if busy else 'readonly')
        self.reward_select.configure(state='disabled' if busy or (self.experiments and self.experiments.branch) else 'readonly')
        if self.continuous_button:
            self.continuous_button.configure(state='normal' if not busy or self.continuous_mode else 'disabled',
                                             text=('取消停止，继续训练' if self.stop_requested.is_set() else '停止并保存')
                                             if busy and self.continuous_mode else '开始持续训练')
        self.update_continuous_controls()
        if busy:
            self.progress.start(12)
        else:
            self.progress.stop()
        self.update_thread_hint()

        if self.experiments and hasattr(self.experiments, 'set_busy'):
            self.experiments.set_busy(busy)
        elif self.experiments:
            self.experiments.select.configure(state='disabled' if busy else 'readonly')
            self.experiments.create_button.configure(state=normal)
            if self.experiments.branch:self.progressive_button.configure(state='disabled')

    def update_continuous_controls(self, *_):
        if not hasattr(self, 'limit_check'):
            return
        busy = self.training_active
        limited = self.continuous_limit_enabled.get()
        self.limit_check.configure(state='disabled' if busy else 'normal')
        self.limit_entry.configure(state='normal' if limited and not busy else 'disabled')
        self.end_eval_check.configure(state='normal' if limited and not busy else 'disabled')

    def toggle_continuous(self):
        if self.worker and self.worker.is_alive() and self.continuous_mode:
            if self.stop_requested.is_set():
                self.cancel_stop()
            else:
                self.stop()
        else:
            self.start(continuous=True)

    def cancel_stop(self):
        """Undo a manual request while its saved boundary is still pending."""
        if not self.worker or not self.worker.is_alive() or not self.continuous_mode:
            return False
        if getattr(self.root, '_closing_after_training', False):
            return False
        with self.stop_control_lock:
            if self.stop_committed.is_set() or not self.stop_requested.is_set():
                return False
            self.stop_requested.clear()
        self.continuous_button.configure(state='normal', text='停止并保存')
        self.stop_button.configure(state='normal')
        self.threads_entry.configure(state='normal')
        self.thread_setting_changed()
        self.update_thread_hint()
        self.status.set('已取消停止，持续训练继续运行')
        self.append('已取消手动停止请求，将继续训练；原先设置的目标局数和定期评估安排保持有效。')
        return True

    def open_manual_evaluation(self):
        if any(app.worker and app.worker.is_alive() for app in getattr(self.root, '_training_apps', [self])):
            messagebox.showinfo('正在运行', '请先停止训练并等待模型保存，再单独评估已保存的模型。', parent=self.root)
            return
        directory = self.selected_run()
        if self.route == 'baseline' and directory == self.runs_dir:
            directory = self.runs_dir / '_initial'
        try:
            if directory is None or not (directory / 'summary.json').is_file():
                raise ValueError('请选择已经保存的训练模型。')
            summary = json.loads((directory / 'summary.json').read_text(encoding='utf-8'))
            checkpoints = {item['tag']: item for item in summary['checkpoints']}
            if not {'random_initial', 'before_continuation'} <= checkpoints.keys():
                raise ValueError('该模型缺少初始或训练前模型记录。')
            games = int(self.eval_games.get().strip())
            threads = int(self.compute_threads.get().strip())
            if not 1 <= games <= MAX_EVAL_GAMES or not 1 <= threads <= 64:
                raise ValueError('评估局数请输入有效范围，计算线程请输入 1 到 64。')
            choose_seeds(2, games)
            if not MANUAL_EVALUATOR.is_file():
                raise ValueError('缺少单独评估程序。')
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            messagebox.showerror('无法评估', str(exc), parent=self.root)
            return
        window = tk.Toplevel(self.root)
        window.title('单独评估模型')
        remember_popup(self, window, 'evaluation')
        window.transient(self.root)
        window.resizable(True, True)
        frame = ttk.Frame(window, padding=18)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='选择要评估的模型', font=('Microsoft YaHei UI', 13, 'bold')).pack(anchor='w')
        ttk.Label(frame, text=f'每个模型使用相同的 {games} 个固定种子；不进行训练。', foreground='#52606d').pack(anchor='w', pady=(6, 4))
        labels = [('random_initial', '初始模型'), ('before_continuation', '所选阶段训练前模型'),
                  ('after_continuation', '所选模型（当前保存点）')]
        if summary['manifest'].get('initialization') == 'behavior_cloned_initial':
            labels = [('after_continuation', '手工模仿起点（尚未 PPO 续训）')]
        elif is_published_origin(summary['manifest']):
            labels = [('after_continuation', '所选模型（发布检查点）')]
        flags = {}
        for tag, label in labels:
            flags[tag] = tk.BooleanVar(value=True)
            ttk.Checkbutton(frame, text=label, variable=flags[tag]).pack(anchor='w', pady=2)
        ttk.Label(frame, text='持续训练每 1000 局保存一次，“阶段训练前”通常是上一个保存点。\n评估使用所选模型保存时的游戏设置，报告独立保存。',
                  foreground='#52606d', justify='left').pack(anchor='w', pady=8)

        def launch():
            selected = [tag for tag, _ in labels if flags[tag].get()]
            if not selected:
                messagebox.showinfo('请选择模型', '请至少选择一个模型。', parent=window)
                return
            window.destroy()
            self.begin_manual_evaluation(directory, games, threads, selected)

        ttk.Button(frame, text='开始评估', command=launch).pack(anchor='e', pady=(4, 0))

    def begin_manual_evaluation(self, directory, games, threads, selected):
        if any(app.worker and app.worker.is_alive() for app in getattr(self.root, '_training_apps', [self])):
            messagebox.showinfo('正在运行', '请先等待当前任务结束。', parent=self.root)
            return
        out = ROOT / 'evaluations' / f'manual_{self.route}' / (dt.datetime.now().strftime('%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:6])
        command = [python_executable(), '-u', str(MANUAL_EVALUATOR), str(directory), '--out', str(out),
                   '--games', str(games), '--threads', str(threads), '--models', *selected]
        self.cancelled = False
        self.continuous_mode = False
        self.started_at, self.finished_at = time.monotonic(), None
        self.training_finished, self.training_average = 0, None
        self.set_busy(True)
        self.status.set('正在单独评估…')
        self.latest_summary.set(f'评估 {len(selected)} 个模型，每个 {games} 局；不进行训练。')
        self.append(f'开始单独评估 {directory.name}：每个模型 {games} 局，计算线程 {threads}。')
        self.worker = threading.Thread(target=self._run_manual_evaluation, args=(command, out), daemon=True)
        self.worker.start()

    def _run_manual_evaluation(self, command, out):
        log_path = out.parent / (out.name + '.log')
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open('w', encoding='utf-8') as log:
                code = self._run_command(command, log, 'evaluation')
            if self.cancelled:
                self.events.put(('stopped', str(log_path)))
            elif code != 0 or not (out / '报告.html').is_file():
                self.events.put(('error', f'单独评估未完成（代码 {code}）。\n日志：{log_path}'))
            else:
                self.events.put(('manual_done', str(out)))
        except Exception:
            self.events.put(('error', traceback.format_exc() + f'\n日志：{log_path}'))

    def open_manual_report(self):
        directory = self.selected_run()
        parent = ROOT / 'evaluations' / f'manual_{self.route}'
        if directory and parent.is_dir():
            for summary_path in sorted(parent.glob('*/summary.json'), key=lambda p: p.stat().st_mtime, reverse=True):
                try:
                    summary = json.loads(summary_path.read_text(encoding='utf-8'))
                    if Path(summary['source_run']).resolve() == directory.resolve() and (summary_path.parent / '报告.html').is_file():
                        webbrowser.open((summary_path.parent / '报告.html').resolve().as_uri())
                        return
                except (OSError, ValueError, KeyError, TypeError):
                    continue
        messagebox.showinfo('报告尚不可用', '所选模型还没有单独评估报告。', parent=self.root)

    def start(self, continuous=False):
        if self.worker and self.worker.is_alive():
            return
        if any(app is not self and app.worker and app.worker.is_alive()
               for app in getattr(self.root, '_training_apps', [])):
            messagebox.showinfo('另一条路线正在训练', '请等待另一条路线完成或先停止，再开始本轮。', parent=self.root)
            return
        previous = self.selected_run()
        training_options = None
        try:
            if self.experiments and hasattr(self.experiments, 'options'):
                training_options = self.experiments.options(freeze=True)
                self.experiments.note_selection()
            if self.experiments and self.experiments.branch:
                self.experiments.apply_settings()
                saved = completed_runs(self.runs_dir, self.route)
                if saved and previous != saved[0][1]:
                    raise ValueError('本实验分支请从最新保存点续训；要从旧模型或共同起点重新尝试，请新建对照实验。')
            session_limit = None
            if continuous and self.continuous_limit_enabled.get():
                value = valid_preference('continuous_limit_games', self.continuous_limit_games.get())
                if value is None:
                    raise ValueError('本次持续训练局数请输入 1000 的倍数，例如 1000、5000 或 10000。')
                session_limit = int(value)
            games = CONTINUOUS_CHUNK if continuous else int(self.train_games.get().strip())
            eval_games = int(self.eval_games.get().strip())
            periodic_config = None
            if continuous:
                interval = '10000'
                if interval is None:
                    raise ValueError('定期评估间隔请输入 1000 到 1000000 之间、能被 1000 整除的局数。')
                if not PERIODIC_EVALUATOR.is_file():
                    raise ValueError('缺少定期评估程序。')
                periodic_config = {'interval': int(interval), 'games': eval_games}
                if session_limit and self.continuous_end_eval_enabled.get() and not MANUAL_EVALUATOR.is_file():
                    raise ValueError('缺少训练结束评估程序。')
            try:
                threads = int(self.compute_threads.get().strip())
            except ValueError:
                raise ValueError('计算线程请输入 1 到 64 之间的整数。') from None
            if not 1 <= threads <= 64:
                raise ValueError('计算线程请输入 1 到 64 之间的整数。')
            reward_mode = self.selected_reward_mode()
            if reward_mode is None:
                raise ValueError('请选择奖励方案。')
            progressive_rents = parse_progressive_rents(self.progressive_rents_json.get())
            progressive_loss, progressive_win = validate_progressive_outcomes(
                float(self.progressive_loss.get()), float(self.progressive_win.get()))
            try:
                toggle_reward = float(self.toggle_reward.get().strip())
            except ValueError:
                raise ValueError('每次开关奖赏请输入数字，例如 -0.001、0 或 0.001。') from None
            if not math.isfinite(toggle_reward) or abs(toggle_reward) > 1:
                raise ValueError('每次开关奖赏请输入 −1 到 1 之间的数字。')
            if games < 2 or games > 1_000_000 or eval_games < 1 or eval_games > 100_000:
                raise ValueError('训练局数请输入 2–1000000；每阶段评估种子数请输入 1–100000。')
            if self.route == 'grouped' and (games < 4 or games % 4):
                raise ValueError('同种子分组每组 4 局，训练局数请填写 4 的倍数，例如 500。')
            if previous is None:
                raise ValueError('没有可续训的模型，请选择已有的训练结果文件夹。')
            if self.route in ('fresh_rules','baseline') and installed_rules_version() != self.rules_version:
                raise ValueError('游戏规则在界面打开后发生了更新，请重新打开训练界面，以使用新规则的独立保存目录。')
            if self.route == 'scratch' and not self.candidate_rules.get():
                meta = json.loads((previous / 'summary.json').read_text(encoding='utf-8'))['manifest']
                if meta.get('candidate_rules_version'):
                    raise ValueError('所选模型已经使用新版候选表达，请保留勾选，或选择升级前的旧模型。')
            if continuous and (self.route == 'grouped' or not CONTINUOUS_REPORTER.is_file()):
                raise ValueError('此路线暂不支持持续训练，或缺少训练报告程序。')
            if continuous and previous != self.runs_dir:
                total = saved_total(previous)
                games = min(CONTINUOUS_CHUNK, max(2, next_evaluation_total(total)-total))
            elif continuous and self.experiments and self.experiments.branch:
                total = self.experiments.branch['origin_training_games']
                games = min(CONTINUOUS_CHUNK, max(2, next_evaluation_total(total)-total))
            train_seed, eval_seed = choose_seeds(games, eval_games)
            trainer = (BASELINE_TRAINER if self.route == 'baseline' else GROUPED_TRAINER if self.route == 'grouped'
                       else FRESH_TRAINER if self.route == 'fresh_rules' else TRAINER)
            if not trainer.is_file() or not REPORTER.is_file():
                raise ValueError('缺少训练或报告程序，请保持项目文件完整。')
        except (OSError, ValueError) as exc:
            messagebox.showerror('无法开始', str(exc), parent=self.root)
            return
        stamp = dt.datetime.now().strftime('%Y%m%d_%H%M%S')
        suffix = save_suffix(training_options, saved_total(previous) if previous and previous != self.runs_dir else 0) if training_options is not None else ''
        self.current_dir = self.runs_dir / f'ui_training_{stamp}_{uuid.uuid4().hex[:6]}{suffix}'
        # The trainer creates its own output directory; do not create it here.
        self.cancelled = False
        self.continuous_mode = bool(continuous)
        self.continuous_limit = session_limit if continuous else None
        self.periodic_config = periodic_config
        self.continuous_end_eval_config = ({'games': eval_games}
            if session_limit and self.continuous_end_eval_enabled.get() else None)
        with self.stop_control_lock:
            self.stop_requested.clear()
            self.stop_committed.clear()
        with self.thread_config_lock:
            self.requested_threads = threads
        self.current_stage_threads = threads
        self.thread_input_invalid = False
        self.continuous_games = self.continuous_wins = 0
        self.continuous_rents = 0.0
        self.live_offset = 0
        self.session_deaths = DeathDistribution()
        self.death_offsets = {}
        self.death_warning = ''
        self.render_live_deaths()
        self.stop_file = self.runs_dir / f'.continuous_stop_{uuid.uuid4().hex}' if continuous else None
        if continuous:
            self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.training_finished = 0
        self.training_average = None
        self.started_at = time.monotonic()
        self.finished_at = None
        self.elapsed_display.set('本轮已用时间：0 秒（含训练、评估和报告）')
        self.set_busy(True)
        self.status.set('正在启动训练…')
        self.latest_summary.set(('持续训练中，每 1000 局保存一次；点击停止后完成当前阶段再保存。' if continuous else
                                 f'本轮训练 {games} 局；每个模型阶段评估 {eval_games} 个种子；{REWARD_NAMES[reward_mode]}；开关奖赏 {toggle_reward:+g}；计算线程 {threads}。'))
        initial_baseline = self.route == 'baseline' and previous == self.runs_dir
        initial_grouped = self.route == 'grouped' and previous == self.runs_dir
        initial_fresh = self.route == 'fresh_rules' and previous == self.runs_dir
        source_label = ('基线初始模型' if initial_baseline else '从零训练 19000 局模型' if initial_grouped
                        else '全新模型（0 局）' if initial_fresh else previous.name)
        evaluation_note = (f"每 {periodic_config['interval']} 局进行 {eval_games} 局定期评估" if periodic_config else '不进行定期评估')
        toggle_scope = toggle_scope_label(self.allow_item_toggle.get(), self.limited_item_toggles.get())
        self.append(f'从 {source_label} 开始{"持续训练（每 1000 局保存，" + evaluation_note + "）" if continuous else f"训练 {games} 局"}；{REWARD_NAMES[reward_mode]}；开关奖赏 {toggle_reward:+g}；主动开关：{toggle_scope}；计算线程 {threads}。')
        if self.continuous_limit:
            ending = (f'随后只评估刚训练好的模型，固定种子 {eval_games:,} 局；若与定期评估重合只运行一次。'
                      if self.continuous_end_eval_config else '到达定期评估点时仍完成该评估。')
            self.append(f'本次持续训练达到 {self.continuous_limit:,} 局后自动保存并停止；{ending}')
        command = [python_executable(), '-u', str(trainer), '--out', str(self.current_dir),
                   '--games', str(games), '--eval-games', str(eval_games), '--train-seed-start', str(train_seed),
                   '--eval-seed-start', str(eval_seed), '--sampling-seed', str(secrets.randbelow(2**30)),
                   '--toggle-reward', str(toggle_reward), '--reward-mode', reward_mode, '--torch-threads', str(threads)]
        if not (initial_baseline or initial_grouped or initial_fresh):
            command.extend(['--previous', str(previous)])
        if not self.allow_item_toggle.get():
            command.append('--no-toggle-actions')
        if self.limited_item_toggles.get():
            command.append('--limited-item-toggles')
        if self.route in ('fresh_rules','baseline') or (self.route == 'scratch' and self.candidate_rules.get()):
            command.append('--candidate-rules')
        if self.route in ('fresh_rules','baseline'):
            command.extend(['--expected-rules-version', self.rules_version])
            if self.experiments and self.experiments.branch:
                command.extend(['--branch-config', str(self.runs_dir/'branch.json')])
        if training_options is not None:
            command.extend(['--training-options', json.dumps(training_options, ensure_ascii=False)])
        if reward_mode == 'progressive':
            command.extend(['--progressive-rents', json.dumps(progressive_rents),
                            '--progressive-loss', str(progressive_loss), '--progressive-win', str(progressive_win)])
        if continuous:
            command.extend(['--no-eval', '--stop-file', str(self.stop_file)])
        self.worker = threading.Thread(target=self._run_continuous if continuous else self._run,
                                       args=(command, self.current_dir), daemon=True)
        self.worker.start()

    def _run_command(self, command: list[str], logfile, phase: str) -> int:
        if self.cancelled:
            return -1
        env = os.environ.copy()
        env['PYTHONUTF8'] = '1'
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
        self.process = subprocess.Popen(command, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, encoding='utf-8', errors='replace', bufsize=1,
                                        env=env, creationflags=flags)
        if self.cancelled:
            self.process.terminate()
        assert self.process.stdout is not None
        with self.process.stdout as stream:
            for line in stream:
                logfile.write(line)
                logfile.flush()
                stripped = line.strip()
                if stripped.startswith(('training ', 'eval ', 'continuing checkpoint', 'Traceback', 'Error', 'RuntimeError', 'ValueError')):
                    self.events.put(('line', stripped))
        code = self.process.wait()
        self.process = None
        return code

    def _run(self, command: list[str], out: Path):
        # A separate log directory avoids pre-creating the trainer's output path.
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.runs_dir / (out.name + '.log')
        try:
            with log_path.open('w', encoding='utf-8') as log:
                code = self._run_command(command, log, 'training')
                if self.cancelled:
                    self.events.put(('stopped', str(log_path)))
                    return
                if code != 0:
                    self.events.put(('error', f'训练进程退出（代码 {code}）。\n日志：{log_path}'))
                    return
                self.events.put(('reporting', None))
                code = self._run_command([python_executable(), '-u', str(REPORTER), str(out)], log, 'report')
                if self.cancelled:
                    self.events.put(('stopped', str(log_path)))
                elif code != 0 or not (out/'报告.html').is_file():
                    self.events.put(('error', f'模型已保存，但报告生成失败（代码 {code}）。\n日志：{log_path}'))
                else:
                    self.events.put(('done', str(out)))
        except Exception:
            self.events.put(('error', traceback.format_exc() + f'\n日志：{log_path}'))
        finally:
            self.process = None

    def _run_continuous(self, command: list[str], out: Path):
        """Save every 1000 games; optional evaluation runs between stages."""
        log_path = self.runs_dir / (out.name + '.log')
        previous = Path(command[command.index('--previous')+1]) if '--previous' in command else None
        controls = getattr(self, 'experiments', None)
        initial_total = saved_total(previous) if previous else (controls.branch['origin_training_games']
            if controls and controls.branch else 0)
        next_eval = next_evaluation_total(initial_total, self.periodic_config['interval'] if self.periodic_config else 10000)
        last_saved = None
        session_games = 0
        session_limit = getattr(self, 'continuous_limit', None)
        end_eval_config = getattr(self, 'continuous_end_eval_config', None)
        try:
            with log_path.open('w', encoding='utf-8') as log:
                if previous and self.periodic_config and initial_total and initial_total % self.periodic_config['interval'] == 0:
                    existing = latest_evaluations(previous).get('after_continuation', {}).get('result')
                    if existing is None and (previous/'periodic_eval.json').is_file():
                        existing = json.loads((previous/'periodic_eval.json').read_text(encoding='utf8'))
                    if not existing or existing.get('games', 0) < self.periodic_config['games']:
                        self.events.put(('periodic_started', self.periodic_config['games']))
                        eval_command = [python_executable(), '-u', str(PERIODIC_EVALUATOR), str(previous),
                            '--games', str(self.periodic_config['games']), '--threads', str(self.requested_threads)]
                        if self._run_command(eval_command,log,'evaluation') != 0:
                            raise RuntimeError('累计保存点的补充评估失败，训练尚未开始。')
                        self.events.put(('periodic_done',str(previous)))
                        if commit_requested_stop(self, previous):
                            return
                while True:
                    if last_saved is not None and commit_requested_stop(self, last_saved):
                        return
                    code = self._run_command(command, log, 'training')
                    if code != 0:
                        self.events.put(('error', f'持续训练进程退出（代码 {code}）。已有保存点未被覆盖。\n日志：{log_path}'))
                        return
                    if self._run_command([python_executable(), '-u', str(CONTINUOUS_REPORTER), str(out)], log, 'report') != 0:
                        self.events.put(('error', f'模型已保存，但训练报告生成失败。\n日志：{log_path}'))
                        return
                    summary = json.loads((out / 'summary.json').read_text(encoding='utf-8'))
                    stats = training_stats(out, int(summary['training_games']))
                    if stats is None:
                        raise ValueError('已保存阶段的逐局记录不完整')
                    updated = (int(summary['checkpoints'][-1]['updated_decisions']) -
                               int(next(c['updated_decisions'] for c in summary['checkpoints'] if c['tag'] == 'before_continuation')))
                    self.events.put(('continuous_saved', {'directory': str(out), 'stats': stats, 'updated': updated}))
                    last_saved = out
                    session_games += int(summary['training_games'])
                    total = int(summary['manifest'].get('previous_training_games', 0)) + int(summary['training_games'])
                    evaluated_here = False
                    if self.periodic_config and total >= next_eval:
                        with self.thread_config_lock:
                            eval_threads = self.requested_threads
                        self.events.put(('periodic_started', self.periodic_config['games']))
                        eval_cmd = [python_executable(), '-u', str(PERIODIC_EVALUATOR), str(out),
                                    '--games', str(self.periodic_config['games']), '--threads', str(eval_threads)]
                        if self._run_command(eval_cmd, log, 'evaluation') != 0:
                            self.events.put(('error', f'阶段模型已保存，但定期评估失败；持续训练已停止。\n日志：{log_path}'))
                            return
                        self.events.put(('periodic_done', str(out)))
                        evaluated_here = True
                        next_eval = next_evaluation_total(total, self.periodic_config['interval'])
                        if commit_requested_stop(self, out):
                            return
                    if session_limit and session_games >= session_limit:
                        if end_eval_config and not evaluated_here:
                            with self.thread_config_lock:
                                eval_threads = self.requested_threads
                            eval_out = ROOT / 'evaluations' / 'training_end' / (dt.datetime.now().strftime('%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:6])
                            self.events.put(('end_eval_started', end_eval_config['games']))
                            eval_cmd = [python_executable(), '-u', str(MANUAL_EVALUATOR), str(out),
                                        '--out', str(eval_out), '--games', str(end_eval_config['games']),
                                        '--threads', str(eval_threads), '--source', '训练结束评估',
                                        '--models', 'after_continuation']
                            if self._run_command(eval_cmd, log, 'evaluation') != 0:
                                self.events.put(('error', f'目标局数已完成且模型已保存，但训练结束评估失败。\n日志：{log_path}'))
                                return
                            self.events.put(('end_eval_done', str(out)))
                        self.events.put(('line', f'本次已完成预设的 {session_games:,} 局，模型已保存，自动停止。'))
                        self.events.put(('continuous_done', str(out)))
                        return
                    train_seed, eval_seed = choose_seeds(CONTINUOUS_CHUNK, 1000)
                    if commit_requested_stop(self, out):
                        return
                    stamp = dt.datetime.now().strftime('%Y%m%d_%H%M%S')
                    options = json.loads(command[command.index('--training-options')+1]) if '--training-options' in command else None
                    suffix = save_suffix(options, total) if options is not None else ''
                    next_out = self.runs_dir / f'ui_training_{stamp}_{uuid.uuid4().hex[:6]}{suffix}'
                    def set_arg(name, value):
                        if name in command:
                            command[command.index(name) + 1] = str(value)
                        else:
                            command.extend([name, str(value)])
                    set_arg('--previous', out)
                    next_games = min(CONTINUOUS_CHUNK, max(2, next_eval-total)) if self.periodic_config else CONTINUOUS_CHUNK
                    if session_limit: next_games = min(next_games, session_limit-session_games)
                    set_arg('--games', next_games)
                    set_arg('--out', next_out)
                    set_arg('--train-seed-start', train_seed)
                    set_arg('--eval-seed-start', eval_seed)
                    set_arg('--sampling-seed', secrets.randbelow(2**30))
                    with self.thread_config_lock:
                        next_threads = self.requested_threads
                    set_arg('--torch-threads', next_threads)
                    out = next_out
                    self.events.put(('continuous_started', {'directory': str(out), 'threads': next_threads}))
        except Exception:
            self.events.put(('error', traceback.format_exc() + f'\n日志：{log_path}'))
        finally:
            self.process = None
            if self.stop_file:
                try:
                    self.stop_file.unlink(missing_ok=True)
                except OSError:
                    pass

    def consume_events(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'line':
                    self._progress_line(value)
                elif kind == 'reporting':
                    self.refresh_live_deaths()
                    self.status.set('训练和评估完成，正在生成逐局报告…')
                    self.append('正在生成报告…')
                elif kind == 'done':
                    self.finish_elapsed()
                    self.set_busy(False)
                    self.status.set('训练完成，模型和报告已保存')
                    self.current_dir = Path(value)
                    self.refresh_live_deaths()
                    self.save_panel_timing(self.current_dir)
                    self.refresh_runs(self.current_dir)
                    self.show_result(self.current_dir)
                    if self.started_at is not None and self.finished_at is not None:
                        self.append(f'本轮全部完成，总耗时 {format_elapsed(self.finished_at - self.started_at)}。')
                    self.completion_sound()
                elif kind == 'continuous_saved':
                    self.current_dir = Path(value['directory'])
                    self.refresh_live_deaths()
                    stats = value['stats']
                    self.continuous_games += stats['games']
                    self.continuous_wins += stats['wins']
                    self.continuous_rents += stats['average_rents'] * stats['games']
                    self.training_finished = self.continuous_games
                    self.training_average = self.continuous_rents / self.continuous_games
                    self.refresh_runs(self.current_dir)
                    self.append(f'已保存阶段模型与逐局记录；本次持续训练累计 {self.continuous_games} 局。')
                    if value['updated'] == 0:
                        self.append('本阶段动作不足一个更新批次，模型参数尚未变化。')
                elif kind == 'continuous_started':
                    self.refresh_live_deaths()
                    self.current_dir = Path(value['directory'])
                    self.current_stage_threads = value['threads']
                    self.live_offset = 0
                    self.status.set('持续训练中…')
                    self.update_thread_hint()
                    self.append(f'下一阶段已启动，计算线程 {self.current_stage_threads}。')
                elif kind == 'periodic_started':
                    self.status.set('正在定期评估已保存的模型…')
                    self.append(f'开始定期评估：固定种子 {value} 局，期间暂停训练。')
                elif kind == 'periodic_done':
                    self.refresh_runs(Path(value))
                    self.append('定期评估完成，结果已保存。')
                elif kind == 'end_eval_started':
                    self.status.set('训练已完成，正在评估刚保存的模型…')
                    self.append(f'开始训练结束评估：只评估刚训练好的模型，固定种子 {value:,} 局，正常开局且关闭辅助。')
                elif kind == 'end_eval_done':
                    self.refresh_runs(Path(value))
                    self.append('训练结束评估完成，已计入该模型的续训后评估及浏览器分析。')
                elif kind == 'manual_done':
                    self.finish_elapsed()
                    self.set_busy(False)
                    result = json.loads((Path(value) / 'summary.json').read_text(encoding='utf-8'))
                    titles = {'random_initial': '初始模型', 'before_continuation': '阶段训练前', 'after_continuation': '所选模型'}
                    lines = []
                    for entry in result['evaluations']:
                        rate = '—' if entry['win_rate'] is None else f"{entry['win_rate'] * 100:.2f}%"
                        lines.append(f"{titles[entry['label']]}：平均交租 {entry['avg_rents']:.3f}，通关率 {rate}（{entry['wins']}/{entry['valid_games']}）")
                    self.status.set('单独评估完成，模型未改变')
                    self.latest_summary.set('；'.join(lines))
                    self.refresh_comparison()
                    for line in lines:
                        self.append(line)
                    self.append('单独评估报告已保存，可点击“打开单独评估报告”。')
                    self.completion_sound()
                elif kind == 'analysis_done':
                    self.analysis_running = False
                    for button in self.analysis_buttons:button.configure(state='normal')
                    webbrowser.open(Path(value).resolve().as_uri())
                elif kind == 'analysis_error':
                    self.analysis_running = False
                    for button in self.analysis_buttons:button.configure(state='normal')
                    messagebox.showerror('无法生成分析',value,parent=self.root)
                elif kind == 'experiment_created':
                    self.experiments.created(value)
                elif kind == 'experiment_report':
                    webbrowser.open(Path(value).resolve().as_uri())
                elif kind == 'experiment_error':
                    window = getattr(self.experiments, 'pending_window', None)
                    if window and window.winfo_exists():window.destroy()
                    messagebox.showerror('实验操作未完成', value, parent=self.root)
                elif kind == 'continuous_done':
                    self.finish_elapsed()
                    self.set_busy(False)
                    self.status.set(f'本次 {self.continuous_games:,} 局目标已完成，模型已保存' if self.continuous_limit and self.continuous_games >= self.continuous_limit else '持续训练已停止，模型和记录已保存')
                    self.latest_summary.set(f'本次已保存 {self.continuous_games} 局；' + self.rolling_summary.get())
                    if self.continuous_games:self.save_panel_timing(Path(value))
                    self.append('持续训练已停止，模型和逐局训练报告已保存。')
                    self.completion_sound()
                elif kind == 'stopped':
                    self.refresh_live_deaths()
                    self.finish_elapsed()
                    self.set_busy(False)
                    self.status.set('本轮已停止')
                    self.append(f'本轮已停止，日志保存在 {value}。已有模型未被覆盖。')
                elif kind == 'error':
                    self.refresh_live_deaths()
                    self.finish_elapsed()
                    self.set_busy(False)
                    self.status.set('运行未完成')
                    self.append(value)
                    messagebox.showerror('运行未完成', value, parent=self.root)
        except queue.Empty:
            pass
        self.root.after(150, self.consume_events)

    def _progress_line(self, line: str):
        if line.startswith('training options: '):
            line = line.replace('training options: ', '本次训练设置：', 1).replace('; evaluations=normal_unassisted', '；评估正常开局且关闭辅助')
        if line.startswith('training shaping: '):
            status = re.search(r'status=(\w+)', line)
            count = re.search(r'reference rows=(\d+)', line)
            if status and status[1] == 'waiting_for_evaluation':
                line = '奖励塑形：暂未找到本分支可用评估；差值引导暂不计分，评估后自动启用。'
            elif status and status[1] == 'ready':
                line = f'奖励塑形：已固定最新评估差值，{count[1] if count else "—"} 个候选阶段达到样本要求；评估不加入塑形分数。'
            else:
                line = '奖励塑形：已启用储物间超额扣分；评估不加入塑形分数。'
        if self.experiments and self.experiments.branch and 'exploration alpha=' in line:
            match = re.search(r'alpha=([\d.]+); added=(\d+)', line)
            if match:
                alpha = float(match[1])
                title = ('A 纯 PPO' if self.experiments.branch['branch_id']=='control' else
                         'B 辅助已撤除' if alpha==0 else f'B 统计辅助强度 {alpha:.3f}')
                self.experiments.caption.set(f'{title} · 本分支新增约 {int(match[2]):,} 局 · 正式评估关闭辅助')
        train = re.search(r'training (\d+)/(\d+); wins=(\d+)', line)
        evaluation = re.search(r'eval (\w+): (\d+)/(\d+)', line)
        average = re.search(r'\bavg_rents=(\d+(?:\.\d+)?)', line)
        speed = re.search(r'\bgames_per_min=([\d.]+)', line)
        if train:
            self.refresh_live_deaths()
            if average:
                chunk_games = int(train[1])
                self.training_finished = self.continuous_games + chunk_games if self.continuous_mode else chunk_games
                self.training_average = ((self.continuous_rents + float(average[1]) * chunk_games) / self.training_finished
                                         if self.continuous_mode else float(average[1]))
                self.refresh_live_trend()
            msg = (f'持续训练：本次已完成 {self.continuous_games + int(train[1])} 局，通关 {self.continuous_wins + int(train[3])} 局'
                   if self.continuous_mode else f'训练：{train[1]} / {train[2]} 局，通关 {train[3]} 局')
        elif evaluation:
            titles = {'random_initial':('基线初始模型' if self.route == 'baseline' else '分组起点模型' if self.route == 'grouped' else '随机初始模型'), 'before_continuation':'续训前模型',
                      'middle':'训练中途模型', 'after_continuation':'续训后模型', 'periodic_eval':'续训后模型（定期）'}
            msg = f'评估{titles.get(evaluation[1], evaluation[1])}：{evaluation[2]} / {evaluation[3]} 局'
            if average:
                msg += f'，本阶段评估平均交租 {float(average[1]):.3f} 次'
            wins = re.search(r'\bwins=(\d+)', line)
            valid = re.search(r'\bvalid_games=(\d+)', line)
            if wins and valid:
                n, w = int(valid[1]), int(wins[1])
                msg += f'，实时通关率 {w / n * 100:.2f}%（{w}/{n}）' if n else '，暂无有效评估对局'
        else:
            msg = line
        if train:
            self.draw_training_charts()
            msg += ' · ' + self.rolling_summary.get()
        if (train or evaluation) and speed:
            msg += f' · {float(speed[1]):.1f} 局/分钟'
        self.status.set(msg)
        self.append(msg)

    def show_result(self, directory: Path):
        try:
            result = json.loads((directory/'summary.json').read_text(encoding='utf-8'))
            before = next(e for e in result['evaluations'] if e['label'] == 'before_continuation')
            after = next(e for e in result['evaluations'] if e['label'] == 'after_continuation')
            prior = result['manifest']['previous_training_games']
            added = result['training_games']
            checkpoints = result['checkpoints']
            initial_updated = next(c['updated_decisions'] for c in checkpoints if c['tag'] == 'before_continuation')
            final_updated = next(c['updated_decisions'] for c in checkpoints if c['tag'] == 'after_continuation')
            updated = max(0, int(final_updated) - int(initial_updated))
            self.latest_summary.set(
                f'累计训练 {prior + added} 局。续训前：胜 {before["wins"]}/{before["games"]}，平均交租 {before["avg_rents"]:.3f}；'
                f'续训后：胜 {after["wins"]}/{after["games"]}，平均交租 {after["avg_rents"]:.3f}。'
                + (' 本轮样本不足一个训练批次，模型尚未更新。' if updated == 0 else ''))
            if updated == 0:
                self.append('注意：本轮样本不足一个训练批次，模型参数尚未更新；可增加训练局数。')
            self.append('模型和逐局报告已保存。可点击“打开报告”查看详情。')
            if result['manifest'].get('candidate_policy_upgraded'):
                self.append('本轮首次升级了候选表达，续训前评估使用升级后的起点；升级前原模型仍保留在原文件夹。')
            elif result['manifest'].get('candidate_capacity_upgraded'):
                self.append('已开放信用卡等效果的完整候选，已有权重和优化器完整保留，原模型文件保留。')
            if result['manifest'].get('item_state_upgraded'):
                self.append('已补齐物品与精华状态；原权重和优化器完整保留，升级起点另存 model_upgraded.zip。')
            if result['manifest'].get('rules_upgraded'):
                self.append('已修正重掷规则：只允许符号候选重掷；历史模型和统计保留。')
        except (OSError, ValueError, KeyError, StopIteration):
            self.append('模型已保存，详细结果请打开报告查看。')

    def report_directory(self) -> Path | None:
        return self.selected_run()

    def open_report(self):
        directory = self.report_directory()
        if self.route == 'baseline' and directory == self.runs_dir:
            registered = latest_evaluations(self.runs_dir/'_initial').get('after_continuation')
            if registered:
                webbrowser.open(Path(registered['report_path']).resolve().as_uri())
                return
        if not directory or not (directory/'报告.html').is_file():
            messagebox.showinfo('报告尚不可用', '所选模型没有报告。训练完成后会自动生成。', parent=self.root)
            return
        webbrowser.open((directory/'报告.html').resolve().as_uri())

    def show_model_settings(self):
        directory = self.selected_run()
        if self.route == 'baseline' and directory == self.runs_dir:
            directory = self.runs_dir/'_initial'
        if not directory or not (directory/'summary.json').is_file():
            messagebox.showinfo('尚无保存模型', '请先选择一个已保存的模型。', parent=self.root)
            return
        data = json.loads((directory/'summary.json').read_text(encoding='utf8'))
        meta = data['manifest'];total = int(meta.get('previous_training_games', 0)) + int(data['training_games'])
        source_name = meta.get('previous_run') or meta.get('origin_run')
        if not source_name:
            source_name = f'最新规则手工示范 {meta["teacher_demo_games"]} 局' if meta.get('teacher_demo_games') else '全新随机模型'
        lines = [meta.get('model_label') or model_label(meta, total), '',
                 '来源保存点：'+str(source_name),
                 '本保存阶段训练：'+str(data['training_games'])+' 局',
                 '奖励方案：'+REWARD_NAMES.get(meta.get('reward_mode'), str(meta.get('reward_mode')))]
        if meta.get('progressive_rents'):
            lines += ['各次交租奖励：'+', '.join(f'{v:.4g}' for v in meta['progressive_rents']),
                      f'失败 {meta.get("progressive_loss")}；通关 {meta.get("progressive_win")}；统一除以 20']
        from training_shaping import label as shaping_label
        shaping = shaping_label(meta.get('training_shaping') or (meta.get('training_options') or {}).get('shaping'))
        lines += [f'训练奖励塑形：{shaping or "未启用"}',
                  f'塑形差值状态：{meta.get("training_shaping_status", "未启用")}',
                  f'每次开关奖励：{meta.get("toggle_reward", 0)}',
                  '正式评估：正常开局，关闭统计辅助、控牌扣分与差值奖励，不额外添加指定内容。', '',
                  '保存位置：'+str(directory),
                  '完整设置与来源校验信息见保存文件夹里的“模型说明.json”或“summary.json”。']
        window = tk.Toplevel(self.root);window.title('所选模型的保存设置');window.geometry('820x450')
        remember_popup(self, window, 'model_settings')
        outer = ttk.Frame(window, padding=12);outer.pack(fill='both', expand=True)
        text = tk.Text(outer, wrap='word', font=('Microsoft YaHei UI', 10))
        scroll = ttk.Scrollbar(outer, command=text.yview);text.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y');text.pack(fill='both', expand=True)
        text.insert('1.0', '\n'.join(lines));text.configure(state='disabled')

    def open_folder(self):
        directory = (self.current_dir if self.worker and self.worker.is_alive() and self.current_dir and self.current_dir.exists() else self.selected_run())
        if directory and directory.exists():
            if os.name == 'nt':
                os.startfile(str(directory))
            else:
                webbrowser.open(directory.resolve().as_uri())
        else:
            messagebox.showinfo('结果尚不可用', '还没有可打开的结果文件夹。', parent=self.root)

    def stop(self):
        if self.worker and self.worker.is_alive():
            if self.continuous_mode:
                with self.stop_control_lock:
                    self.stop_requested.set()
                self.continuous_button.configure(state='normal', text='取消停止，继续训练')
                self.stop_button.configure(state='disabled')
                self.threads_entry.configure(state='disabled')
                self.update_thread_hint()
                self.status.set('正在完成当前 1000 局阶段并保存；可点击按钮取消停止…')
                self.append('收到停止请求，将完成当前 1000 局阶段并保存模型；保存前可点击“取消停止，继续训练”。')
                return True
            self.cancelled = True
            self.stop_button.configure(state='disabled')
            self.status.set('正在停止…')
            if self.process:
                self.process.terminate()
        return True

    def close(self):
        close_training_apps(self.root, [self])


def close_training_apps(root, apps):
    """Confirm first, then wait for continuous training to save before closing."""
    if getattr(root, '_closing_after_training', False):
        return
    active = [app for app in apps if app.worker and app.worker.is_alive()]
    if active and not messagebox.askyesno(
            '训练正在运行',
            '关闭窗口会停止正在进行的训练。持续训练会先完成当前 1000 局阶段并保存；普通训练会中止当前轮次。确定关闭吗？',
            parent=root):
        return
    for app in active:
        if app.stop() is False:
            return
    root._closing_after_training = True

    def finish_when_stopped():
        if any(app.worker and app.worker.is_alive() for app in apps):
            root.after(200, finish_when_stopped)
            return
        for app in apps:
            app.consume_events()
            app.flush_preferences()
        root.destroy()

    finish_when_stopped()


class ProgressiveSettingsDialog:
    """Edit a per-route preference; only a saved snapshot reaches a trainer."""
    def __init__(self, app: TrainingApp):
        self.app = app
        self.window = tk.Toplevel(app.root)
        self.window.title('调整方案二：交租与胜负奖励')
        self.window.geometry('920x570')
        remember_popup(app, self.window, 'progressive_rewards')
        self.window.minsize(720, 500)
        self.window.transient(app.root)
        outer = ttk.Frame(self.window, padding=16)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='方案二 · 奖励设置', font=('Microsoft YaHei UI', 14, 'bold')).pack(anchor='w')
        ttk.Label(outer, text='拖动滑块调整原始分数（0～5），点击“保存方案”后用于本路线的下次训练。',
                  foreground='#52606d', wraplength=850).pack(anchor='w', pady=(4, 2))
        ttk.Label(outer, text='失败分数范围 −20～0，通关奖励范围 0～50；所有分数仍统一除以 20。默认值保持原精度。',
                  foreground='#52606d', wraplength=850).pack(anchor='w', pady=(0, 8))
        try:
            values = parse_progressive_rents(app.progressive_rents_json.get())
        except ValueError:
            values = DEFAULT_PROGRESSIVE_RENTS
        self.variables = [tk.DoubleVar(value=value) for value in values]
        self.labels = [tk.StringVar() for _ in values]
        self.loss_variable = tk.DoubleVar(value=float(app.progressive_loss.get()))
        self.win_variable = tk.DoubleVar(value=float(app.progressive_win.get()))
        self.loss_label = tk.StringVar()
        self.win_label = tk.StringVar()
        self.summary = tk.StringVar()
        grid = ttk.Frame(outer)
        grid.pack(fill='both', expand=True)
        grid.columnconfigure(0, weight=1, uniform='rent_column')
        grid.columnconfigure(1, weight=1, uniform='rent_column')
        for index, var in enumerate(self.variables):
            row, col = index % 6, index // 6
            cell = ttk.Frame(grid, padding=(3, 4))
            cell.grid(row=row, column=col, sticky='ew', padx=(0, 15) if col == 0 else (15, 0))
            cell.columnconfigure(1, weight=1)
            ttk.Label(cell, text=f'第 {index + 1} 次', width=8).grid(row=0, column=0, sticky='w')
            ttk.Scale(cell, from_=0, to=5, variable=var, orient='horizontal').grid(row=0, column=1, sticky='ew', padx=6)
            ttk.Label(cell, textvariable=self.labels[index], width=6, anchor='e').grid(row=0, column=2)
            var.trace_add('write', lambda *_args, i=index: self.update_display(i))
            self.update_display(index)
        for col, (title, variable, low, high, label) in enumerate((
                ('失败扣分', self.loss_variable, -20, 0, self.loss_label),
                ('通关奖励', self.win_variable, 0, 50, self.win_label))):
            cell = ttk.Frame(grid, padding=(3, 6))
            cell.grid(row=6, column=col, sticky='ew', padx=(0, 15) if col == 0 else (15, 0))
            cell.columnconfigure(1, weight=1)
            ttk.Label(cell, text=title, width=8).grid(row=0, column=0, sticky='w')
            ttk.Scale(cell, from_=low, to=high, variable=variable, orient='horizontal').grid(row=0, column=1, sticky='ew', padx=6)
            ttk.Label(cell, textvariable=label, width=7, anchor='e').grid(row=0, column=2)
            variable.trace_add('write', lambda *_: self.update_outcomes())
        self.update_outcomes()
        ttk.Label(outer, textvariable=self.summary, foreground='#29516b').pack(anchor='w', pady=(4, 8))
        buttons = ttk.Frame(outer)
        buttons.pack(fill='x')
        ttk.Button(buttons, text='保存方案', command=self.save).pack(side='left')
        ttk.Button(buttons, text='恢复默认', command=self.reset).pack(side='left', padx=8)
        ttk.Button(buttons, text='读取所选模型的方案', command=self.load_model).pack(side='left', padx=8)
        ttk.Button(buttons, text='取消', command=self.window.destroy).pack(side='right')
        ttk.Label(outer, text='保存的方案按训练路线独立保留，不会随切换模型自动更改；可点击“读取所选模型的方案”。恢复或读取后仍需保存。',
                  foreground='#52606d', wraplength=850).pack(anchor='w', pady=(8, 0))

    def update_display(self, index):
        self.labels[index].set(f'{self.variables[index].get():.3f}')
        self.summary.set(f'第 6 次：{self.variables[5].get():.3f} 分  |  12 次合计：{sum(v.get() for v in self.variables):.3f} 分')

    def update_outcomes(self):
        self.loss_label.set(f'{self.loss_variable.get():+.2f}')
        self.win_label.set(f'{self.win_variable.get():+.2f}')

    def reset(self):
        for variable, value in zip(self.variables, DEFAULT_PROGRESSIVE_RENTS):
            variable.set(value)
        self.loss_variable.set(DEFAULT_PROGRESSIVE_LOSS)
        self.win_variable.set(DEFAULT_PROGRESSIVE_WIN)

    def load_model(self):
        directory = self.app.selected_run()
        try:
            data = json.loads((directory / 'summary.json').read_text(encoding='utf-8'))
            meta = data['manifest']
            if meta.get('reward_mode') != 'progressive':
                raise ValueError('所选模型上一轮使用的不是方案二。')
            values = validate_progressive_rents(meta.get('progressive_rents', DEFAULT_PROGRESSIVE_RENTS))
            loss, win = validate_progressive_outcomes(meta.get('progressive_loss', DEFAULT_PROGRESSIVE_LOSS),
                                                      meta.get('progressive_win', DEFAULT_PROGRESSIVE_WIN))
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            messagebox.showinfo('无法读取模型设置', str(exc), parent=self.window)
            return
        for variable, value in zip(self.variables, values):
            variable.set(value)
        self.loss_variable.set(loss)
        self.win_variable.set(win)

    def save(self):
        try:
            values = validate_progressive_rents([variable.get() for variable in self.variables])
            # Outcome sliders show two decimals; save exactly what the user sees.
            loss, win = validate_progressive_outcomes(round(self.loss_variable.get(), 2), round(self.win_variable.get(), 2))
            encoded = json.dumps(values)
            preferences = {name: getattr(self.app, name).get() for name in PREFERENCE_FIELDS}
            preferences['progressive_rents_json'] = encoded
            preferences['progressive_loss'] = str(loss)
            preferences['progressive_win'] = str(win)
            save_preferences(PREFERENCE_SETTINGS, self.app.route, preferences)
        except (OSError, ValueError, TypeError, tk.TclError) as exc:
            messagebox.showerror('无法保存方案', str(exc), parent=self.window)
            return
        self.app.progressive_rents_json.set(encoded)
        self.app.progressive_loss.set(str(loss))
        self.app.progressive_win.set(str(win))
        self.window.destroy()


class ColumnSettingsDialog:
    """Edit visibility, display names and ordering without touching result data."""
    def __init__(self, app: TrainingApp):
        self.app = app
        self.defaults = {name: label for name, label, _ in HISTORY_COLUMNS}
        current = normalize_column_settings(app.column_settings)
        self.hidden = set(current['hidden'])
        self.names = dict(current['names'])
        self.editing = None
        self.window = tk.Toplevel(app.root)
        self.window.title('自定义结果表头')
        self.window.geometry('690x550')
        remember_popup(app, self.window, 'columns')
        self.window.minsize(570, 430)
        self.window.transient(app.root)
        outer = ttk.Frame(self.window, padding=16)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='选择要显示的信息并调整顺序', font=('Microsoft YaHei UI', 13, 'bold')).pack(anchor='w')
        ttk.Label(outer, text='选中一行后，可以显示或隐藏、上移或下移，也可以修改它的显示名称。',
                  foreground='#52606d', wraplength=620).pack(anchor='w', pady=(4, 8))
        frame = ttk.Frame(outer)
        frame.pack(fill='both', expand=True)
        self.table = ttk.Treeview(frame, columns=('visible', 'original', 'display'), show='headings', selectmode='browse', height=10)
        for key, label, width in (('visible', '显示状态', 80), ('original', '原字段', 200), ('display', '显示名称', 230)):
            self.table.heading(key, text=label)
            self.table.column(key, width=width, minwidth=60, anchor='w', stretch=key != 'visible')
        bar = ttk.Scrollbar(frame, command=self.table.yview)
        self.table.configure(yscrollcommand=bar.set)
        bar.pack(side='right', fill='y')
        self.table.pack(side='left', fill='both', expand=True)
        for name in current['order']:
            self.table.insert('', 'end', iid=name, values=self.row_values(name))
        self.table.bind('<<TreeviewSelect>>', self.select_row)
        self.table.bind('<Double-1>', self.double_click)
        actions = ttk.Frame(outer)
        actions.pack(fill='x', pady=(8, 4))
        ttk.Button(actions, text='显示 / 隐藏', command=self.toggle).pack(side='left')
        ttk.Button(actions, text='上移', command=lambda: self.move(-1)).pack(side='left', padx=6)
        ttk.Button(actions, text='下移', command=lambda: self.move(1)).pack(side='left')
        ttk.Button(actions, text='恢复默认', command=self.reset).pack(side='right')
        edit = ttk.Frame(outer)
        edit.pack(fill='x', pady=(6, 8))
        ttk.Label(edit, text='显示名称').pack(side='left')
        self.name_value = tk.StringVar()
        self.name_entry = ttk.Entry(edit, textvariable=self.name_value)
        self.name_entry.pack(side='left', fill='x', expand=True, padx=8)
        ttk.Label(edit, text='留空则使用原名称', foreground='#52606d').pack(side='left')
        self.name_value.trace_add('write', self.name_changed)
        bottom = ttk.Frame(outer)
        bottom.pack(fill='x')
        ttk.Button(bottom, text='保存', command=self.save).pack(side='right')
        ttk.Button(bottom, text='取消', command=self.window.destroy).pack(side='right', padx=8)
        if current['order']:
            self.table.selection_set(current['order'][0])
            self.select_row()
        self.window.grab_set()

    def row_values(self, name):
        return ('隐藏' if name in self.hidden else '显示', self.defaults[name], self.names.get(name, self.defaults[name]))

    def selected(self):
        selected = self.table.selection()
        return selected[0] if selected else None

    def select_row(self, event=None):
        self.editing = None
        name = self.selected()
        self.name_value.set(self.names.get(name, '') if name else '')
        self.name_entry.configure(state='normal' if name else 'disabled')
        self.editing = name

    def name_changed(self, *_):
        if self.editing:
            value = self.name_value.get().strip()
            if value:
                self.names[self.editing] = value
            else:
                self.names.pop(self.editing, None)
            self.table.item(self.editing, values=self.row_values(self.editing))

    def double_click(self, event):
        if self.table.identify_column(event.x) == '#1':
            name = self.table.identify_row(event.y)
            if name:
                self.table.selection_set(name)
                self.toggle()

    def toggle(self):
        name = self.selected()
        if name:
            if name in self.hidden:
                self.hidden.remove(name)
            else:
                self.hidden.add(name)
            self.table.item(name, values=self.row_values(name))

    def move(self, direction):
        name = self.selected()
        if name:
            position = self.table.index(name)
            target = max(0, min(len(self.table.get_children()) - 1, position + direction))
            self.table.move(name, '', target)
            self.table.see(name)

    def reset(self):
        self.hidden.clear()
        self.names.clear()
        for index, (name, _, _) in enumerate(HISTORY_COLUMNS):
            self.table.move(name, '', index)
            self.table.item(name, values=self.row_values(name))
        self.select_row()

    def save(self):
        order = list(self.table.get_children())
        if all(name in self.hidden for name in order):
            messagebox.showerror('无法保存', '请至少显示一列。', parent=self.window)
            return
        settings = {'order': order, 'hidden': list(self.hidden), 'names': self.names}
        try:
            save_column_settings(COLUMN_SETTINGS, settings)
        except OSError as exc:
            messagebox.showerror('无法保存', f'表头设置没有保存：{exc}', parent=self.window)
            return
        self.app.apply_column_settings(settings)
        self.window.destroy()


def show_route(notebook, apps, route):
    """Show a single route's two tabs without mixing models or histories."""
    selected = notebook.select()
    comparison = any(selected == str(app.comparison_tab) for app in apps)
    charts = any(selected == str(app.charts_tab) for app in apps)
    outcomes = any(selected == str(app.outcomes_tab) for app in apps)
    target = next(app for app in apps if app.route == route)
    for app in apps:
        for tab in (app.training_tab, app.comparison_tab, app.charts_tab, app.outcomes_tab):
            if app is target:
                notebook.add(tab)
            else:
                notebook.hide(tab)
    notebook.select(target.outcomes_tab if outcomes else target.charts_tab if charts else target.comparison_tab if comparison else target.training_tab)


def main():
    root = tk.Tk()
    header = ttk.Frame(root, padding=(10, 6))
    header.pack(fill='x')
    ttk.Label(header, text='训练方案').pack(side='left', padx=(0, 6))
    route_name = tk.StringVar(value='当前规则 · PPO 训练')
    routes = {'当前规则 · PPO 训练': 'fresh_rules'}
    if BASELINE_TRAINER.is_file():
        routes['当前规则 · 模仿手工'] = 'baseline'
    if completed_runs(RUNS, 'scratch'):
        routes['旧实验 · 原从零模型'] = 'scratch'
    if GROUPED_TRAINER.is_file() and GROUPED_SOURCE.is_dir():
        routes['旧实验 · 同种子分组'] = 'grouped'
    selector = ttk.Combobox(header, textvariable=route_name, values=tuple(routes), state='readonly', width=22)
    selector.pack(side='left')
    ttk.Label(header, text=f'当前规则：{installed_rules_version()}', foreground='#52606d').pack(side='left', padx=10)
    notebook = ttk.Notebook(root)
    notebook.pack(fill='both', expand=True)
    apps = [TrainingApp(root, notebook, route) for route in routes.values()]
    root._training_apps = apps
    show_route(notebook, apps, 'fresh_rules')
    selector.bind('<<ComboboxSelected>>', lambda event: show_route(notebook, apps, routes[route_name.get()]))
    root.protocol('WM_DELETE_WINDOW', lambda: close_training_apps(root, apps))
    if '--self-check' in sys.argv:
        root.withdraw()
        root.update()
        root.destroy()
        return
    root.mainloop()


if __name__ == '__main__':
    main()
