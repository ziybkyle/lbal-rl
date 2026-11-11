"""A small, bounded progress log for each independent training area."""
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import uuid

MAX_LINES = 500


class ProgressLog:
    def __init__(self, directory, route, runs_dir):
        identity = str(Path(runs_dir).resolve()).casefold()
        key = hashlib.sha256(identity.encode('utf8')).hexdigest()[:16]
        self.path = Path(directory) / route / f'{key}.json'
        try:
            data = json.loads(self.path.read_text(encoding='utf8'))
            lines = data.get('lines', [])
            if not isinstance(lines, list):
                lines = []
        except (OSError, ValueError, AttributeError):
            lines = []
        self.lines = deque((line for line in lines if isinstance(line, str)), maxlen=MAX_LINES)

    def append(self, text):
        self.lines.extend(str(text).rstrip().splitlines())
        temporary = self.path.with_name(f'.{self.path.name}.{uuid.uuid4().hex}.tmp')
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps({'version': 1, 'lines': list(self.lines)}, ensure_ascii=False), encoding='utf8')
            os.replace(temporary, self.path)
        except OSError:
            pass
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
