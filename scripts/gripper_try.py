#!/usr/bin/env python3
"""Try every reasonable way of driving the MSG gripper over CAN, one step at a time.

Needs a PAROL6 controller already running against the arm, e.g. on the Dell:
    terminal 1:  ~/p6latest/bin/parol6-server --serial=/dev/ttyACM0
    terminal 2:  ~/p6latest/bin/python gripper_try.py

After each step it shows the gripper's feedback and waits for Enter.
Write down which step numbers made the jaws move. Ctrl+C stops at any time.
Only the gripper is commanded. No joint motion is sent.
"""
import inspect
import time

from parol6 import RobotClient


def frac_current() -> bool:
    try:
        import parol6.tools as t
        return "* (hi - lo)" in inspect.getsource(t)
    except Exception:
        return False


FRAC = frac_current()


def cur(ma: int):
    """Grip current in whatever unit this parol6 expects."""
    return round((ma - 100) / 2700, 3) if FRAC else ma


def show(c, label=""):
    try:
        ts = c._tool_status()
        f = {k: getattr(ts, k) for k in ("key", "variant_key", "state", "engaged",
             "fault_code", "positions", "channels") if hasattr(ts, k)}
        print(f"      feedback {label}: {f}")
    except Exception as e:
        print(f"      feedback {label}: error {e}")


STEP = [0]


def step(c, desc, fn, settle=2.5):
    STEP[0] += 1
    print(f"\n[{STEP[0]}] {desc}")
    try:
        t = time.time()
        r = fn()
        print(f"      sent ok -> {r}  ({time.time() - t:.1f}s)")
    except Exception as e:
        print(f"      FAILED: {e}")
    for i in range(int(settle / 0.5)):
        time.sleep(0.5)
        if i in (0, int(settle / 0.5) - 1):
            show(c, "now" if i == 0 else "after")
    input("      Did the jaws move? (note it) Press Enter for the next step… ")


def moves(c, tag):
    step(c, f"{tag}: CALIBRATE (jaws should sweep)", lambda: c.tool_action("MSG", "calibrate", wait=True, timeout=20), 4)
    step(c, f"{tag}: CLOSE, slow, low force (~300 mA)", lambda: c.tool_action("MSG", "move", [1.0, 0.3, cur(300)], wait=True, timeout=15))
    step(c, f"{tag}: OPEN, slow, low force", lambda: c.tool_action("MSG", "move", [0.0, 0.3, cur(300)], wait=True, timeout=15))
    step(c, f"{tag}: HALF, medium force (~800 mA)", lambda: c.tool_action("MSG", "move", [0.5, 0.5, cur(800)], wait=True, timeout=15))


def main():
    try:
        import importlib.metadata as md
        ver = md.version("parol6")
    except Exception:
        ver = "?"
    print(f"parol6 {ver}  |  grip current sent as {'fraction' if FRAC else 'mA'}")
    print("Hands clear of the jaws. 24V on. Ctrl+C to stop.\n")

    c = RobotClient(host="127.0.0.1", port=5001)
    c.wait_ready(timeout=10)
    show(c, "start")

    step(c, "Real hardware mode (simulator off)", lambda: c.simulator(False), 2)
    step(c, "Enable controller (resume)", lambda: c.resume(), 1)
    step(c, "Gripper 'idle' action (clears/activates on some firmware)", lambda: c.tool_action("MSG", "idle"), 1.5)

    step(c, "Select tool MSG (default variant)", lambda: c.select_tool("MSG"), 1)
    moves(c, "MSG default")

    for v in ("100mm", "150mm"):
        step(c, f"Select tool MSG variant {v}", lambda v=v: c.select_tool("MSG", v), 1)
        moves(c, f"MSG {v}")

    step(c, "Gripper 'stop' action", lambda: c.tool_action("MSG", "stop"), 1)
    c.close()
    print("\nDone. Tell Claude which step numbers moved the jaws, and paste this output.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
