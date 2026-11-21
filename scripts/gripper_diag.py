#!/usr/bin/env python3
"""Gripper (MSG over CAN) diagnostic. Talks to the PAROL6 controller VOLT is
already running (UDP 127.0.0.1:5001) and prints what the controller hears back
from the gripper before, during and after calibrate + close + open.

Run on the Pi, with the arm's 24V on, hands clear of the jaws:
    cd ~/volt-v2 && .venv/bin/python scripts/gripper_diag.py [variant]

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


def tool_line(c, label):
    try:
        st = c.status()
        ts = getattr(st, "tool_status", None)
        if ts is None:
            print(f"  {label:<18} (no tool status)")
            return
        pos = list(getattr(ts, "positions", []) or [])
        ch = list(getattr(ts, "channels", []) or [])
        print(f"  {label:<18} tool={getattr(ts, 'tool_key', '?')!s:<6} "
              f"state={getattr(ts, 'state', '?')!s:<14} pos={[round(p, 3) for p in pos]} "
              f"current={ch} engaged={getattr(ts, 'engaged', '?')} "
              f"part={getattr(ts, 'part_detected', '?')} fault={getattr(ts, 'fault_code', '?')}")
    except Exception as e:
        print(f"  {label:<18} status error: {e}")


def watch(c, label, seconds):
    end = time.time() + seconds
    while time.time() < end:
        tool_line(c, label)
        time.sleep(0.5)


def main():
    c = RobotClient(host="127.0.0.1", port=5001)
    c.wait_ready(timeout=10)
    print("Connected to controller.")
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
                                 ("CLOSE", "move", [1.0, 0.5, 400]),
                                 ("OPEN", "move", [0.0, 0.5, 400])):
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
