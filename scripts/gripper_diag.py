#!/usr/bin/env python3
"""Gripper (MSG over CAN) diagnostic. Talks to the PAROL6 controller VOLT is
already running (UDP 127.0.0.1:5001) and prints what the controller hears back
from the gripper before, during and after calibrate + close + open.

Run on the Pi, with the arm's 24V on, hands clear of the jaws:
    cd ~/volt-v2 && .venv/bin/python scripts/gripper_diag.py [variant]

Or on any PC with the latest parol6 and the arm's USB plugged in:
    parol6-server --serial=/dev/ttyACM0      (terminal 1)
    python gripper_diag.py                   (terminal 2)

variant is optional, e.g. 100mm or 150mm.

Reading the output:
  * position / current change during calibrate -> CAN works, gripper answers
  * everything stays 0 -> the controller hears nothing from the gripper:
    CAN wiring, H/L swapped, termination, or the STEPFOC not set to CAN mode
  * fault != 0 -> the gripper is reporting an error
"""
import sys
import time

from parol6 import RobotClient

VARIANT = sys.argv[1] if len(sys.argv) > 1 else ""


def grip_current():
    """Older parol6 (June 2026) takes grip current in mA; newer versions (Sep 2026+)
    take a 0-1 fraction of the gripper's current range. Send the right one."""
    try:
        import inspect
        import parol6.tools as t
        if "* (hi - lo)" in inspect.getsource(t):
            return 0.15          # ~500 mA on the MSG's 100-2800 mA range
    except Exception:
        pass
    return 400


def tool_line(c, label):
    """Print the raw tool status two ways: status().tool_status and _tool_status()."""
    for src, fn in (("status", lambda: getattr(c.status(), "tool_status", None)),
                    ("query", lambda: c._tool_status())):
        try:
            ts = fn()
            if ts is None:
                print(f"  {label:<14} [{src}] None")
                continue
            fields = {k: getattr(ts, k) for k in ("key", "variant_key", "state", "engaged",
                      "part_detected", "fault_code", "positions", "channels") if hasattr(ts, k)}
            print(f"  {label:<14} [{src}] {fields or repr(ts)[:300]}")
        except Exception as e:
            print(f"  {label:<14} [{src}] error: {e}")


def watch(c, label, seconds):
    end = time.time() + seconds
    while time.time() < end:
        tool_line(c, label)
        time.sleep(0.5)


def main():
    c = RobotClient(host="127.0.0.1", port=5001)
    c.wait_ready(timeout=10)
    try:
        import importlib.metadata as md
        ver = md.version("parol6")
    except Exception:
        ver = "?"
    cur = grip_current()
    print(f"Connected to controller. parol6 {ver}, grip current param = {cur}")
    tool_line(c, "start")

    print("\nEnabling controller (resume)…")
    try:
        c.resume()
    except Exception as e:
        print("  resume:", e)

    print(f"\nSelecting tool MSG {VARIANT or '(default variant)'}…")
    try:
        print("  select_tool ->", c.select_tool("MSG", VARIANT) if VARIANT else c.select_tool("MSG"))
    except Exception as e:
        print("  select_tool FAILED:", e)
    time.sleep(0.5)
    tool_line(c, "after select")

    for name, action, params in (("CALIBRATE", "calibrate", None),
                                 ("CLOSE", "move", [1.0, 0.5, cur]),
                                 ("OPEN", "move", [0.0, 0.5, cur])):
        print(f"\n>>> {name}")
        try:
            t = time.time()
            rc = c.tool_action("MSG", action, params, wait=True, timeout=20) if params \
                else c.tool_action("MSG", action, wait=True, timeout=20)
            print(f"  returned {rc} after {time.time() - t:.1f}s")
        except Exception as e:
            print(f"  FAILED: {e}")
        watch(c, name.lower(), 2.0)

    c.close()
    print("\nDone. Did the jaws move? Did pos/current change at any point?")


if __name__ == "__main__":
    main()
