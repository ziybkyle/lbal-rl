"""Bare pipe client spawned on its own console: start -> choose(intro) -> spin.
No curses. If this blocks, the issue is pipe/relay in spawned-console context;
if it works, the issue is specific to the curses TUI process."""
import sys
import time

sys.path.insert(0, ".")
from godot_env import GodotEnv


def log(msg):
    with open("pipe_probe.log", "a") as f:
        f.write("%.1f %s\n" % (time.time(), msg))


def main():
    log("spawned")
    env = GodotEnv(seed="PIPEPROBE1")
    try:
        st = env.start()
        log("start ok phase=%s coins=%s" % (st["phase"], st["coins"]))
        for i in range(6):
            if st["phase"] == "email":
                e = st["email"]
                if e["cards"]:
                    reply = e["cards"][0]["type"]
                else:
                    reply = e["replies"][0] if e["replies"] else "skip"
                log("choose %s" % reply)
                st = env.choose(reply)
                log("  -> phase=%s" % st["phase"])
            elif st["can_spin"]:
                log("spin")
                st = env.spin()
                log("  -> phase=%s spins=%s coins=%s" % (st["phase"], st["spins"], st["coins"]))
            else:
                log("idle-stuck")
                st = env.get_state()
        log("PROBE OK")
    finally:
        env.close()


if __name__ == "__main__":
    main()
