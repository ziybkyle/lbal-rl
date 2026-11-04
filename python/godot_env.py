"""GodotEnv — Python interface to the native Godot game kernel (server mode).

Spawns the patched headless game (optionally inside WSL) and talks to its
RLDriver TCP server. Every command pumps the game synchronously to the next
decision point and returns the state snapshot dict.

Usage:
    from godot_env import GodotEnv
    env = GodotEnv(seed="ABCD123456")      # spawns engine, waits for server
    st = env.start()                       # new run (uses seed)
    st = env.spin()
    st = env.choose(reply="skip")          # reply = card type for card emails
    ...
    env.close()
"""
from __future__ import annotations

import json
import os
import platform
import queue
import random
import socket
import subprocess
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
GAME_DIR = (HERE.parent / "game").resolve()
WINDOWS_ENGINE = (HERE.parent / "runtime" / "godot.exe").resolve()

def _wsl_path(path: Path) -> str:
    if os.name != "nt":
        return str(path)
    drive = path.drive[:1].lower()
    rest = str(path)[2:].replace("\\", "/")
    return f"/mnt/{drive}{rest}" if drive else str(path).replace("\\", "/")


# Inside WSL the engine path; override with LBAL_WSL_ENGINE if different.
WSL_ENGINE = os.environ.get("LBAL_WSL_ENGINE", "~/godot-server-344")
WSL_GAME = os.environ.get("LBAL_WSL_GAME", _wsl_path(GAME_DIR))
WSL_RELAY = os.environ.get("LBAL_WSL_RELAY", _wsl_path(HERE / "pipe_relay.py"))


class GodotError(RuntimeError):
    pass


class GodotEnv:
    def __init__(
        self,
        seed: str = "",
        floor: int = 1,
        port: int | None = None,
        spawn: bool = True,
        host: str = "127.0.0.1",
        bind_wildcard: bool = True,
        timeout: float = 120.0,
        connect_timeout: float = 90.0,
        transport: str = "auto",
    ):
        """Use native TCP by default; the legacy WSL pipe remains available."""
        self.seed = seed
        self.floor = floor
        self.port = port or random.randint(7400, 7899)
        self.host = host
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.transport = transport
        if transport == "auto":
            self.transport = "tcp"
        self.proc: subprocess.Popen | None = None
        self.sock: socket.socket | None = None
        self._buf = b""
        self._q: "queue.Queue[bytes | None]" = queue.Queue()
        self._reader: threading.Thread | None = None
        self.state: dict = {}
        if spawn:
            if self.transport == "pipe":
                self._spawn_pipe()
            else:
                self._spawn_tcp(bind_wildcard)
                self._wait_server_tcp()

    # ---------------------------------------------------------------- process

    def _spawn_tcp(self, bind_wildcard: bool):
        bind = "0" if bind_wildcard else "1"
        if platform.system() == "Windows":
            if not WINDOWS_ENGINE.is_file():
                raise GodotError(f"Godot engine not found: {WINDOWS_ENGINE}")
            env = dict(os.environ)
            env.update({"RL_MODE": "server", "RL_PORT": str(self.port), "RL_BIND": "1", "RL_HEADLESS": "1"})
            user_data = HERE.parent / "runtime" / "user_data"
            user_data.mkdir(parents=True, exist_ok=True)
            env["APPDATA"] = str(user_data)
            env["LOCALAPPDATA"] = str(user_data)
            env.pop("RL_PROTO", None)
            full = [str(WINDOWS_ENGINE), "--no-window", "--audio-driver", "Dummy", "--path", str(GAME_DIR)]
            self.proc = subprocess.Popen(
                full, env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            env = dict(os.environ)
            env.update({"RL_MODE": "server", "RL_PORT": str(self.port), "RL_BIND": bind})
            full = [os.path.expanduser(WSL_ENGINE), "--path", WSL_GAME]
            self.proc = subprocess.Popen(
                full, env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

    def _spawn_pipe(self):
        if platform.system() != "Windows":
            raise GodotError("pipe transport only supported on Windows")
        cmd = f"python3 -u '{WSL_RELAY}' --port {self.port}"
        relay_err = open(HERE / "relay_err.log", "w")
        self.proc = subprocess.Popen(
            ["wsl", "-e", "bash", "-lc", cmd],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=relay_err,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        assert self.proc.stdin is not None and self.proc.stdout is not None

        def reader():
            try:
                for line in self.proc.stdout:
                    self._q.put(line)
            except Exception:
                pass
            finally:
                self._q.put(None)

        self._reader = threading.Thread(target=reader, daemon=True)
        self._reader.start()
        deadline = time.time() + self.connect_timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise GodotError("engine relay exited during startup")
            try:
                hello = self.recv()
                if hello.get("type") != "hello":
                    raise GodotError(f"bad hello: {hello}")
                return
            except queue.Empty:
                continue
            except GodotError:
                raise
        raise GodotError("engine did not come up (pipe)")

    def _wait_server_tcp(self):
        deadline = time.time() + self.connect_timeout
        while time.time() < deadline:
            try:
                s = socket.create_connection((self.host, self.port), timeout=2.0)
                self.sock = s
                self.sock.settimeout(self.timeout)
                hello = self.recv()
                if hello.get("type") != "hello":
                    raise GodotError(f"bad hello: {hello}")
                return
            except (ConnectionRefusedError, socket.timeout, OSError):
                if self.proc and self.proc.poll() is not None:
                    raise GodotError("engine process exited during startup")
                time.sleep(0.3)
        raise GodotError(f"engine did not come up on {self.host}:{self.port}")

    # ---------------------------------------------------------------- protocol

    def recv(self) -> dict:
        if self.transport == "pipe":
            if self._reader is None:
                raise GodotError("not connected")
            try:
                item = self._q.get(timeout=self.timeout)
            except queue.Empty:
                raise GodotError(f"timeout after {self.timeout}s waiting for engine")
            if item is None:
                raise ConnectionError("engine relay closed the pipe")
            self._buf += item
        else:
            assert self.sock is not None
            while b"\n" not in self._buf:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise ConnectionError("server closed connection")
                self._buf += chunk
        if b"\n" not in self._buf:
            raise GodotError("partial message without newline")
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line.decode("utf-8"))

    def send(self, obj: dict):
        if self.transport == "pipe":
            if self.proc is None or self.proc.stdin is None:
                raise GodotError("not connected")
            self.proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        else:
            assert self.sock is not None
            self.sock.sendall((json.dumps(obj) + "\n").encode("utf-8"))

    def _cmd(self, obj: dict) -> dict:
        self.send(obj)
        msg = self.recv()
        if msg.get("type") == "error":
            # every error is followed by a state snapshot
            st = self.recv()
            self.state = st
            raise GodotError(msg.get("msg", "unknown server error"))
        if "phase" in msg:
            self.state = msg
        return msg

    # ---------------------------------------------------------------- api

    def start(self, seed: str | None = None, floor: int | None = None) -> dict:
        if seed is not None:
            self.seed = seed
        if floor is not None:
            self.floor = floor
        return self._cmd({"cmd": "start", "seed": self.seed, "floor": self.floor})

    def spin(self) -> dict:
        return self._cmd({"cmd": "spin"})

    def choose(self, reply: str) -> dict:
        return self._cmd({"cmd": "choose", "reply": reply})

    def item_action(self, index: int, item: str, action: str) -> dict:
        return self._cmd({"cmd": "item_action", "index": index, "item": item, "action": action})

    def get_state(self) -> dict:
        return self._cmd({"cmd": "state"})

    def pump(self, n: int = 60) -> dict:
        return self._cmd({"cmd": "pump", "n": n})

    def close(self):
        try:
            if self.sock is not None:
                self.send({"cmd": "quit"})
                try:
                    self.recv()
                except Exception:
                    pass
                self.sock.close()
        except Exception:
            pass
        finally:
            self.sock = None
        if self.proc is not None:
            try:
                self.proc.terminate()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


if __name__ == "__main__":
    # minimal self-test
    env = GodotEnv(seed="PYENV00001")
    try:
        st = env.start()
        print("phase:", st["phase"], "coins:", st["coins"])
        for _ in range(30):
            if st["phase"] == "email":
                e = st["email"]
                if e["cards"]:
                    st = env.choose(e["cards"][0]["type"])
                else:
                    st = env.choose(e["replies"][0] if e["replies"] else "skip")
            elif st["phase"] in ("game_over", "out_of_money"):
                st = env.choose("retry")
            elif st["phase"] in ("ending", "win"):
                print("run won at spins", st["spins"])
                break
            elif st["can_spin"]:
                st = env.spin()
            else:
                st = env.get_state()
        print("spins:", st["spins"], "coins:", st["coins"], "deck:", sorted(st["deck"]))
        print("PYENV OK")
    finally:
        env.close()
