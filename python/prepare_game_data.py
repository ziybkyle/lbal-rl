"""Copy the four simulator inputs from an owned Steam PCK (Godot 3, pack v1)."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct

from published_checkpoint import REGISTRY, ROOT, sha256


def read_inputs(pack, expected):
    files = {}
    with pack.open('rb') as source:
        file_size = pack.stat().st_size
        header = source.read(88)
        if len(header) != 88 or header[:8] != b'GDPC\x01\x00\x00\x00':
            raise ValueError('需要 Steam 安装目录中的 Luck be a Landlord.pck（Godot 3 PCK v1）。')
        count, = struct.unpack_from('<I', header, 84)
        if count > 100_000:
            raise ValueError('PCK 文件索引不完整。')
        for _ in range(count):
            length, = struct.unpack('<I', source.read(4))
            if not 0 < length <= 8192:
                raise ValueError('PCK 文件索引不完整。')
            name = source.read(length).rstrip(b'\x00').decode('utf-8').removeprefix('res://')
            offset, size, md5 = struct.unpack('<QQ16s', source.read(32))
            if name in expected:
                if name in files or size > 4_194_304 or offset + size > file_size:
                    raise ValueError(f'PCK 中的 {name} 记录无效。')
                files[name] = (offset, size, md5)
        data = {}
        for name, digest in expected.items():
            if name not in files:
                raise ValueError(f'PCK 中缺少 {name}。')
            offset, size, md5 = files[name]
            source.seek(offset)
            contents = source.read(size)
            if hashlib.md5(contents, usedforsecurity=False).digest() != md5:
                raise ValueError(f'PCK 中的 {name} 校验失败。')
            if hashlib.sha256(contents).hexdigest() != digest:
                raise ValueError(f'{name} 与发布版本不匹配，请检查 Steam 游戏版本。')
            data[name] = contents
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pack', type=Path, nargs='?')
    args = parser.parse_args()
    try:
        expected = json.loads(REGISTRY.read_text(encoding='utf-8'))['game_files_sha256']
        if args.pack:
            data = read_inputs(args.pack, expected)
            for name, digest in expected.items():
                dest = ROOT / 'game' / name
                if dest.exists() and sha256(dest) != digest:
                    raise ValueError(f'请先备份并移开不匹配的 game/{name}。')
            for name, contents in data.items():
                dest = ROOT / 'game' / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                if not dest.exists():
                    with dest.open('xb') as target:
                        target.write(contents)
        for name, digest in expected.items():
            dest = ROOT / 'game' / name
            if not dest.is_file() or sha256(dest) != digest:
                raise ValueError(f'缺少或版本不匹配：game/{name}')
        print('游戏数据校验通过。 Game data verified.', flush=True)
    except (OSError, ValueError, struct.error) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
