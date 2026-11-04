#!/usr/bin/env python3
"""WSL-side relay: spawn the engine (TCP 127.0.0.1, WSL-internal) and bridge
its line-JSON protocol to this process's stdin/stdout.

Spawned by GodotEnv via `wsl -e bash -lc "python3 -u pipe_relay.py --port N"`,
so the Windows side talks through wsl.exe pipes — immune to WSL2 localhost
forwarding breakage (proxy TUN, mirrored/NAT modes).

Protocol passthrough only: no parsing except newline framing.
Engine is terminated when the client closes stdin or the socket dies.
Run with -u (unbuffered).
"""
import argparse
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0,
                    help="0 = pick a free port inside WSL (the Windows side never "
                         "needs to know it when the transport is a stdio pipe)")
    ap.add_argument(
        "--game",
        default=os.environ.get(
            "LBAL_WSL_GAME", str(Path(__file__).resolve().parent.parent / "game")
        ),
    )
    ap.add_argument("--engine", default=os.path.expanduser("~/godot-server-344"))
    ap.add_argument("--floor", type=int, default=1)
    ap.add_argument("--proto", default="", help="RL_PROTO value (3 = v3 driver)")
    args = ap.parse_args()

    if args.port == 0:
        # Bind-then-close is racy in theory; in practice this relay immediately
        # occupies the port with the engine, and every relay runs in its own
        # process inside the same WSL network namespace, so collisions here mean
        # a stale engine from an earlier run — which the engine's own exit-on-
        # disconnect watchdog now cleans up.
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        args.port = probe.getsockname()[1]
        probe.close()

    env = dict(os.environ)
    env.update({"RL_MODE": "server", "RL_PORT": str(args.port), "RL_BIND": "1"})
    if args.proto:
        env.update({"RL_PROTO": str(args.proto)})
    if os.environ.get("RL_TRACE") == "1":
        env.update({"RL_TRACE": "1"})
    if os.environ.get("RL_NODEPROF") == "1":
        env.update({"RL_NODEPROF": "1"})
    # Keep the engine's own output: a GDScript runtime error aborts the frame and
    # would otherwise look like an eternal client timeout with nothing to read.
    eng_log_path = "/tmp/rl_engine_%d.log" % args.port
    eng_log = open(eng_log_path, "wb")
    proc = subprocess.Popen(
        [args.engine, "--path", args.game], env=env,
        stdout=eng_log, stderr=eng_log,
        start_new_session=True,
    )
    sock = None
    try:
        deadline = time.time() + 120
        while time.time() < deadline and proc.poll() is None:
            try:
                sock = socket.create_connection(("127.0.0.1", args.port), timeout=2)
                break
            except OSError:
                time.sleep(0.3)
        if sock is None:
            sys.stderr.write("relay: engine did not come up on port %d\n" % args.port)
            sys.exit(1)
        sock.settimeout(None)  # blocking mode — the connect probe used timeout=2

        rf = sock.makefile("rb")

        def pump_sock():
            trace = os.environ.get("RELAY_TRACE") == "1"
            trace_out = open("/tmp/relay_trace_%d.log" % os.getpid(), "a", buffering=1) if trace else None
            buf = b""
            try:
                while True:
                    data = sock.recv(65536)
                    if not data:
                        if trace_out:
                            trace_out.write("EOF from engine\n")
                        break
                    if trace_out:
                        trace_out.write("RECV %d bytes %r\n" % (len(data), data[:60]))
                    buf += data
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        sys.stdout.buffer.write(line + b"\n")
                        sys.stdout.buffer.flush()
            except OSError as e:
                if trace_out:
                    trace_out.write("sock err %r\n" % e)
            finally:
                try:
                    os.close(sys.stdout.fileno())
                except OSError:
                    pass

        t = threading.Thread(target=pump_sock, daemon=True)
        t.start()

        trace = os.environ.get("RELAY_TRACE") == "1"
        trace_out = open("/tmp/relay_trace_%d.log" % os.getpid(), "a", buffering=1) if trace else None
        for line in sys.stdin.buffer:
            if trace_out:
                trace_out.write("IN  %r\n" % line)
            try:
                sock.sendall(line if line.endswith(b"\n") else line + b"\n")
            except OSError:
                break
        # stdin EOF: client gone
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


if __name__ == "__main__":
    main()
