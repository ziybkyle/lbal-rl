"""Local client for the Godot v3 decision protocol.

This module has no training-library dependency. Each client owns one engine
process and a private user-data directory; the game project is never modified.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import time
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
GAME = ROOT / "game"
ENGINE = ROOT / "runtime" / "godot.exe"


class EngineError(RuntimeError):
    """The engine, wire protocol, or a requested action failed."""


@dataclass(frozen=True)
class Candidate:
    index: int
    kind: int
    reference: int | None
    type_id: int
    aux: tuple[float, ...]


@dataclass(frozen=True)
class Frame:
    frame_id: int
    episode: int
    kind: int
    version: int
    vocab_version: int
    phi: float
    reward: float
    terminated: bool
    truncated: bool
    end_reason: int
    spins_elapsed: int
    auto_resolved: int
    scalars: tuple[float, ...]
    destroyed_history: tuple[float, ...]
    removed_history: tuple[float, ...]
    board: tuple[int, ...]
    board_neighbors: tuple[tuple[int, ...], ...]
    deck_types: tuple[int, ...]
    deck_features: tuple[tuple[float, ...], ...]
    deck_mask: tuple[bool, ...]
    deck_uids: tuple[int, ...]
    item_types: tuple[int, ...]
    item_features: tuple[tuple[float, ...], ...]
    item_mask: tuple[bool, ...]
    item_uids: tuple[int, ...]
    offer_types: tuple[int, ...]
    offer_features: tuple[tuple[float, ...], ...]
    candidates: tuple[Candidate, ...]
    info: dict[str, Any]
    raw: bytes

    @property
    def outcome(self) -> str:
        """Separate normal wins/losses from incomplete or invalid episodes."""
        if self.truncated or self.end_reason == 6 or self.info.get("unresolved", 0):
            return "invalid"
        if not self.terminated:
            return "running"
        return {1: "loss", 2: "win"}.get(self.end_reason, "invalid")

    @classmethod
    def decode(cls, message: dict[str, Any], shapes: dict[str, int]) -> "Frame":
        if message.get("type") != "frame":
            raise EngineError(f"Expected frame, received: {message.get('type')!r}")
        try:
            blob = base64.b64decode(message["blob"], validate=True)
            pos = 0

            def unpack(fmt: str):
                nonlocal pos
                fmt = "<" + fmt
                value = struct.unpack_from(fmt, blob, pos)
                pos += struct.calcsize(fmt)
                return value

            def floats(n: int) -> tuple[float, ...]:
                return unpack(f"{n}f")

            def uint16s(n: int) -> tuple[int, ...]:
                return unpack(f"{n}H")

            def bytes_(n: int) -> tuple[int, ...]:
                return unpack(f"{n}B")

            (fid, episode, kind, count, version, vocab_version, phi, reward,
             terminated, truncated, reason, spins, auto) = unpack("IIBHHHffBBBHB")
            if version != 4:
                raise EngineError(f"Unsupported frame version {version}; expected 4")
            k, nboard, ndeck, nitems, noffers, nscalar, nsym, ndf, nif, naux = (
                shapes[x] for x in ("K", "N_BOARD", "N_DECK", "N_ITEMS", "N_OFFERS",
                                    "N_SCALARS", "N_SYM", "N_DECK_FEAT", "N_ITEM_FEAT", "CAND_AUX")
            )
            if count > k:
                raise EngineError(f"Candidate count {count} exceeds capacity {k}")
            scalars = floats(nscalar)
            destroyed_history = floats(nsym)
            removed_history = floats(nsym)
            board = uint16s(nboard)
            neighbors = uint16s(nboard * 8)
            board_neighbors = tuple(neighbors[i * 8:(i + 1) * 8] for i in range(nboard))
            deck_types = uint16s(ndeck)
            df = floats(ndeck * ndf)
            deck_features = tuple(df[i * ndf:(i + 1) * ndf] for i in range(ndeck))
            deck_mask = tuple(bool(x) for x in bytes_(ndeck))
            deck_uids = unpack(f"{ndeck}I")
            item_types = uint16s(nitems)
            itf = floats(nitems * nif)
            item_features = tuple(itf[i * nif:(i + 1) * nif] for i in range(nitems))
            item_mask = tuple(bool(x) for x in bytes_(nitems))
            item_uids = unpack(f"{nitems}I")
            offer_types = uint16s(noffers)
            of = floats(noffers * 4)
            offer_features = tuple(of[i * 4:(i + 1) * 4] for i in range(noffers))
            kinds = bytes_(k)
            refs = uint16s(k)
            types = uint16s(k)
            aux = floats(k * naux)
            mask = bytes_(k)
            if pos != len(blob):
                raise EngineError(f"Unexpected frame size: consumed {pos} of {len(blob)} bytes")
            candidates = tuple(
                Candidate(i, kinds[i], None if refs[i] == 65535 else refs[i], types[i],
                          aux[i * naux:(i + 1) * naux])
                for i in range(count) if mask[i]
            )
            return cls(fid, episode, kind, version, vocab_version, phi, reward,
                       bool(terminated), bool(truncated), reason, spins, auto, scalars,
                       destroyed_history, removed_history, board, board_neighbors,
                       deck_types, deck_features, deck_mask, deck_uids, item_types,
                       item_features, item_mask, item_uids, offer_types, offer_features,
                       candidates, message.get("info", {}), blob)
        except (KeyError, ValueError, struct.error) as exc:
            raise EngineError(f"Malformed v3 frame: {exc}") from exc


class V3Client:
    def __init__(self, *, game: Path = GAME, engine: Path = ENGINE,
                 timeout: float = 30.0, startup_timeout: float = 40.0, startup_attempts: int = 3):
        self.game = Path(game).resolve()
        self.engine = Path(engine).resolve()
        self.timeout = timeout
        self.startup_timeout = startup_timeout
        self.hello: dict[str, Any] = {}
        self.frame: Frame | None = None
        self.startup_warnings: list[str] = []
        if startup_attempts < 1:
            raise ValueError("startup_attempts must be positive")
        for attempt in range(startup_attempts):
            self._temp = tempfile.TemporaryDirectory(prefix="xyfd_v3_")
            self._root = Path(self._temp.name)
            self._log = None
            self.proc = None
            self.sock = None
            self._reader = None
            try:
                self._start()
                break
            except Exception as exc:
                if self._log is not None:
                    self._log.flush()
                log_path = self._root / "engine.log"
                details = log_path.read_text(encoding="utf-8", errors="replace")[-4000:] if log_path.exists() else ""
                for diagnostic in self._root.rglob("rlv3.log"):
                    details += "\nDriver log:\n" + diagnostic.read_text(encoding="utf-8", errors="replace")[-4000:]
                self.close()
                failure = f"{exc}\nEngine log:\n{details}"
                # A local handshake may occasionally be reset on startup. Retry
                # only before any game begins, using a new process and profile.
                transient = isinstance(exc.__cause__, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError))
                transient = transient or str(exc) == "Engine closed its connection"
                if not transient or attempt + 1 == startup_attempts:
                    raise EngineError(failure) from exc
                self.startup_warnings.append(failure)

    def _start(self) -> None:
        if not self.engine.is_file() or not (self.game / "project.godot").is_file():
            raise EngineError(f"Missing engine or project: {self.engine}, {self.game}")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        env = dict(os.environ)
        env.update({"RL_MODE": "server", "RL_PROTO": "3", "RL_PORT": str(port),
                    "RL_BIND": "1", "RL_HEADLESS": "1",
                    "APPDATA": str(self._root), "LOCALAPPDATA": str(self._root)})
        self._log = (self._root / "engine.log").open("wb")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(
            [str(self.engine), "--no-window", "--audio-driver", "Dummy", "--path", str(self.game)],
            env=env, stdout=self._log, stderr=subprocess.STDOUT, creationflags=flags,
        )
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise EngineError(f"Engine exited during startup ({self.proc.returncode})")
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), timeout=0.5)
                break
            except (ConnectionRefusedError, TimeoutError, OSError):
                time.sleep(0.1)
        if self.sock is None:
            raise EngineError("Engine startup timed out")
        self.sock.settimeout(self.timeout)
        self._reader = self.sock.makefile("rb")
        self.hello = self._recv()
        if self.hello.get("type") != "hello" or self.hello.get("v") != 3:
            raise EngineError(f"Unexpected engine handshake: {self.hello!r}")

    def _recv(self) -> dict[str, Any]:
        try:
            line = self._reader.readline()
            if not line:
                raise EngineError("Engine closed its connection")
            return json.loads(line)
        except (OSError, ValueError) as exc:
            raise EngineError(f"Engine response failed: {exc}") from exc

    def _send(self, command: dict[str, Any]) -> None:
        try:
            self.sock.sendall((json.dumps(command, ensure_ascii=True) + "\n").encode("utf-8"))
        except OSError as exc:
            raise EngineError(f"Engine command failed: {exc}") from exc

    def command(self, command: dict[str, Any]) -> Frame:
        self._send(command)
        message = self._recv()
        if message.get("type") == "error":
            # The protocol sends the last valid frame after a rejected action.
            self.frame = Frame.decode(self._recv(), self.hello["shapes"])
            raise EngineError(f"{message.get('code', 'error')}: {message.get('msg', '')}")
        self.frame = Frame.decode(message, self.hello["shapes"])
        return self.frame

    def reset(self, seed: str, floor: int = 1) -> Frame:
        return self.command({"cmd": "reset", "seed": seed, "floor": floor})

    def step(self, action: int) -> Frame:
        if self.frame is None or not any(c.index == action for c in self.frame.candidates):
            raise EngineError(f"Action {action} is not a legal current candidate")
        return self.command({"cmd": "step", "a": action})

    def type_name(self, type_id: int) -> str:
        vocab = self.hello["vocab"]
        if 0 <= type_id < 168 and type_id < len(vocab["symbols"]):
            return vocab["symbols"][type_id]
        if 168 <= type_id < 282 and type_id - 168 < len(vocab["items"]):
            return vocab["items"][type_id - 168]
        if 282 <= type_id < 410 and type_id - 282 < len(vocab["essences"]):
            return vocab["essences"][type_id - 282]
        return {410: "empty", 411: "confirm", 412: "deny", 413: "skip",
                414: "reroll", 415: "pay", 416: "item_button", 417: "toggle_item",
                418: "destroy_item", 424: "column_0", 425: "column_1", 426: "column_2",
                427: "column_3", 428: "column_4", 429: "other_button",
                430: "spin", 431: "remove"}.get(type_id, str(type_id))

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.sendall(b'{"cmd":"quit"}\n')
            except OSError:
                pass
        if self._reader is not None:
            self._reader.close()
            self._reader = None
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        if self.proc is not None:
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=3)
            self.proc = None
        if self._log is not None:
            self._log.close()
            self._log = None
        if self._temp is not None:
            self._temp.cleanup()
            self._temp = None

    def __enter__(self) -> "V3Client":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
