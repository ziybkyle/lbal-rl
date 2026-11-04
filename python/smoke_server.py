"""Smoke test: connect to the game's RL TCP server and play a few turns.

Run INSIDE WSL next to the engine process:
    python3 smoke_server.py [port]
"""
import json
import socket
import sys
import time


class Client:
    def __init__(self, port=7350, host="127.0.0.1", timeout=60.0):
        self.sock = socket.create_connection((host, port), timeout=10.0)
        self.sock.settimeout(timeout)
        self.buf = b""

    def recv_msg(self):
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("server closed")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line.decode("utf-8"))

    def send(self, obj):
        self.sock.sendall((json.dumps(obj) + "\n").encode("utf-8"))


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7350
    c = Client(port)
    hello = c.recv_msg()
    print("hello:", hello)
    c.send({"cmd": "start", "seed": "SMOKE00001", "floor": 1})
    st = c.recv_msg()
    assert st.get("type") != "error", st
    print(f"start: phase={st['phase']} coins={st['coins']} rent={st['rent']} "
          f"spins_left={st['spins_left']} board_cols={len(st['board'])} "
          f"rows={[len(r) for r in st['board']]}")
    print(f"  deck({len(st['deck'])}): {st['deck'][:8]}")
    spins_done = 0
    for turn in range(40):
        if st["phase"] == "email":
            e = st["email"]
            if e["cards"]:
                pick = e["cards"][0]["type"]
                print(f"  email {e['type']}: pick card {pick}")
                c.send({"cmd": "choose", "reply": pick})
            else:
                reply = e["replies"][0] if e["replies"] else "skip"
                print(f"  email {e['type']}: reply {reply}")
                c.send({"cmd": "choose", "reply": reply})
        elif st["phase"] in ("game_over", "out_of_money", "ending", "win"):
            print(f"  run ended: {st['phase']} after {st['spins']} spins")
            c.send({"cmd": "choose", "reply": "retry"} if st["phase"] != "ending" else {"cmd": "state"})
            if st["phase"] == "ending":
                break
        elif st["can_spin"]:
            c.send({"cmd": "spin"})
            spins_done += 1
        else:
            print(f"  stuck: phase={st['phase']} can_spin={st['can_spin']} coins={st['coins']}")
            c.send({"cmd": "state"})
        st = c.recv_msg()
        if st.get("type") == "error":
            print("  server error:", st["msg"])
            st = c.recv_msg()
        if st["spins"] >= 8 and st["phase"] != "busy":
            print(f"turn {turn}: phase={st['phase']} spins={st['spins']} coins={st['coins']} "
                  f"floor={st['floor']} rent={st['rent']} left={st['spins_left']}")
            if st["spins"] >= 8:
                break
    print("last state keys:", sorted(st.keys()))
    print(f"spins played: {spins_done}, final spins counter: {st['spins']}")
    c.send({"cmd": "quit"})
    try:
        print("bye:", c.recv_msg())
    except Exception:
        pass
    print("SMOKE OK")


if __name__ == "__main__":
    main()
