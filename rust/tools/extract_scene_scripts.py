from pathlib import Path


def decode_string(data: str, start: int) -> tuple[str, int]:
    i = start
    out: list[str] = []
    while i < len(data):
        ch = data[i]
        if ch == "\\":
            nxt = data[i + 1]
            if nxt == "n":
                out.append("\n")
            elif nxt == "t":
                out.append("\t")
            elif nxt == "r":
                out.append("\r")
            elif nxt == '"':
                out.append('"')
            elif nxt == "\\":
                out.append("\\")
            else:
                out.append(nxt)
            i += 2
            continue
        if ch == '"':
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    raise ValueError("unterminated string")


def extract_all(path: Path) -> list[tuple[str, str]]:
    data = path.read_text(encoding="utf-8")
    result: list[tuple[str, str]] = []
    pos = 0
    while True:
        block = data.find('[sub_resource type="GDScript"', pos)
        if block < 0:
            break
        header_end = data.find("\n", block)
        header = data[block:header_end]
        source_marker = data.find('script/source = "', header_end)
        if source_marker < 0:
            break
        # The next sub_resource or node marks the end of this block.
        next_block = data.find("\n[", source_marker)
        if next_block < 0:
            next_block = len(data)
        source, _ = decode_string(data, source_marker + len('script/source = "'))
        result.append((header, source))
        pos = source_marker + len(source)
    return result


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    outdir = root / "rust" / "reference"
    outdir.mkdir(parents=True, exist_ok=True)
    for path in sorted((root / "game").glob("*.tscn")):
        sources = extract_all(path)
        for index, (header, source) in enumerate(sources):
            target = outdir / f"{path.stem}.{index}.gd"
            target.write_text(source, encoding="utf-8", newline="\n")
            print(f"{path.name} [{header}] -> {target} ({len(source)} chars, {source.count(chr(10)) + 1} lines)")


if __name__ == "__main__":
    main()
