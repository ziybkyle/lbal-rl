"""Drive tui_play.py through a pty with scripted keys; capture rendered screens.

Run INSIDE WSL:  python3 tui_pty_test.py [seed]
Prints PASS/FAIL + saves screen dumps to /tmp/tui_screens.txt
"""
import os
import pty
import re
import select
import struct
import sys
import termios
import time
import fcntl


def set_winsize(fd, rows, cols):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def read_for(fd, seconds, sink):
    end = time.time() + seconds
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], max(0, end - time.time()))
        if fd in r:
            try:
                data = os.read(fd, 65536)
            except OSError:
                return False
            if not data:
                return False
            sink.write(data.decode("utf-8", "replace"))
    return True


def strip_ansi(text):
    text = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", text)
    text = re.sub(r"\x1b[()][B0]", "", text)
    return text


def main():
    seed = sys.argv[1] if len(sys.argv) > 1 else "TUITEST001"
    sink = open("/tmp/tui_raw.log", "w", encoding="utf-8", errors="replace")
    pid, fd = pty.fork()
    if pid == 0:
        os.environ["TERM"] = "xterm"
        os.chdir(os.path.dirname(os.path.abspath(__file__)))
        os.execvp("python3", ["python3", "tui_play.py", seed])
    set_winsize(fd, 40, 120)

    marks = []
    ok = read_for(fd, 40, sink)  # engine boot + first render
    marks.append(("boot", 40))

    script = []
    for _ in range(8):          # intro + starting card picks
        script.append(("1", 1.5))
    for _ in range(4):          # spins
        script.append((" ", 3.0))
        for _ in range(3):      # handle emails that pop up mid-script
            script.append(("1", 1.5))
    script.append(("q", 2.0))

    sent = 0
    for key, wait in script:
        if not ok:
            break
        try:
            os.write(fd, key.encode())
        except OSError:
            break
        sent += 1
        ok = read_for(fd, wait, sink)
        if key == "q":
            break

    time.sleep(1.0)
    try:
        read_for(fd, 2, sink)
    except Exception:
        pass
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass
    sink.close()

    raw = open("/tmp/tui_raw.log", "r", encoding="utf-8", errors="replace").read()
    txt = strip_ansi(raw)
    open("/tmp/tui_screens.txt", "w").write(txt)
    checks = {
        "board rendered": "BOARD" in txt,
        "seed shown": seed in txt,
        "email/currency panel": ("EMAIL:" in txt) or ("No email pending" in txt),
        "rent line": "rent" in txt,
        "coins line": "$" in txt,
        "keys help": "SPACE=spin" in txt,
    }
    for k, v in checks.items():
        print(("PASS " if v else "FAIL ") + k)
    print(f"keys sent: {sent}; raw bytes: {len(raw)}")
    print("screen tail:")
    tail = "\n".join(l for l in txt.split("\n") if l.strip())[-800:]
    print(tail)
    sys.exit(0 if all(checks.values()) else 1)


if __name__ == "__main__":
    main()
