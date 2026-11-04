
import curses, sys
log = open("minkey_child.log", "w", buffering=1)
def run(scr):
    curses.curs_set(0)
    scr.nodelay(False)
    scr.addstr(0, 0, "waiting for keys; q quits")
    scr.refresh()
    while True:
        k = scr.getch()
        log.write(f"key={k}\n")
        if k == ord("q"):
            break
curses.wrapper(run)
log.write("done\n")
