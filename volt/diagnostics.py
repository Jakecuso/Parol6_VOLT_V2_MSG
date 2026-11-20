"""volt/diagnostics.py — the hardware checks behind Settings > System check.

Passive checks never move anything. Motion checks (gripper cycle, joint wiggle)
only run when you tap them, the motors are enabled, and they move tiny amounts
at low speed.

Each check returns {"id", "name", "status": pass|warn|fail|skip, "detail", "fix"}.
"""
from __future__ import annotations

import glob
import math
import os
import shutil
import subprocess
import time

import httpx

from .camera import camera
from .hub import RobotHub

STM32_VID = "0483"


def _r(id, name, status, detail="", fix=""):
    return {"id": id, "name": name, "status": status, "detail": detail, "fix": fix}


def _sh(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout.strip()
    except Exception:
        return ""


# ── passive checks ─────────────────────────────────────────────────────────

def check_api(hub: RobotHub):
    if hub.is_mock:
        return _r("api", "PAROL6 software", "warn", "PAROL6 API not installed, running a fake arm",
                  "Run scripts/setup_pi.sh on the Pi")
    try:
        import importlib.metadata as md
        v = md.version("parol6")
    except Exception:
        v = "installed"
    return _r("api", "PAROL6 software", "pass", f"parol6 {v}")


def check_arm_usb(hub: RobotHub):
    if hub.simulate:
        return _r("arm_usb", "Arm USB cable", "skip", "Simulator mode")
    try:
        from serial.tools import list_ports
        ports = list(list_ports.comports())
    except Exception as e:
        return _r("arm_usb", "Arm USB cable", "fail", str(e))
    for p in ports:
        if p.vid and f"{p.vid:04x}" == STM32_VID:
            return _r("arm_usb", "Arm USB cable", "pass", f"PAROL6 board on {p.device}")
    acm = glob.glob("/dev/ttyACM*")
    if acm:
        return _r("arm_usb", "Arm USB cable", "warn", f"Serial port {acm[0]} found but not an STM32",
                  "Check Settings > Robot > Port")
    return _r("arm_usb", "Arm USB cable", "fail", "No arm control board on USB",
              "Plug the arm's USB into the Pi and power the control board")


def check_controller(hub: RobotHub):
    if hub.state != "connected" or not hub.robot.connected:
        return _r("controller", "Controller link", "fail", f"State: {hub.state}. {hub.last_error}",
                  "Tap Reconnect. On the real arm, check 24V power and the USB cable")
    try:
        t = time.time()
        hub.robot.ping()
        return _r("controller", "Controller link", "pass", f"Ping {1000*(time.time()-t):.0f} ms")
    except Exception as e:
        return _r("controller", "Controller link", "fail", str(e))


def check_estop(hub: RobotHub):
    if not hub.robot.connected:
        return _r("estop", "E-stop", "skip", "Not connected")
    s = hub.snapshot()
    if s["estop"] is None:
        return _r("estop", "E-stop", "warn", "Could not read E-stop input")
    if s["estop"]:
        return _r("estop", "E-stop", "warn", "E-stop is PRESSED", "Twist to release, then Enable")
    return _r("estop", "E-stop", "pass", "Released")


def check_telemetry(hub: RobotHub):
    if not hub.robot.connected:
        return _r("telemetry", "Joint readout", "skip", "Not connected")
    try:
        a = hub.robot.get_angles()
        if len(a) != 6 or not all(math.isfinite(x) for x in a):
            return _r("telemetry", "Joint readout", "fail", f"Bad angles: {a}")
        return _r("telemetry", "Joint readout", "pass", " ".join(f"{x:.0f}°" for x in a))
    except Exception as e:
        return _r("telemetry", "Joint readout", "fail", str(e))


def check_limits(hub: RobotHub):
    if not hub.robot.connected:
        return _r("limits", "Within soft limits", "skip", "Not connected")
    a = hub.robot.get_angles()
    bad = [f"J{i+1} {x:.0f}°" for i, (x, (lo, hi)) in
           enumerate(zip(a, hub.settings.get("motion")["joint_limits"])) if not lo <= x <= hi]
    if bad:
        return _r("limits", "Within soft limits", "warn", "Outside: " + ", ".join(bad),
                  "Home the arm, or widen limits in Settings > Motion")
    return _r("limits", "Within soft limits", "pass", "All joints inside limits")


def check_gripper(hub: RobotHub):
    g = hub.settings.get("gripper")
    if g["link"] == "none":
        return _r("gripper", "Gripper link", "skip", "No gripper configured")
    if g["link"] == "uart":
        try:
            resp = hub._gripper_uart().info()
            port = hub._uart.status().get("port")
            if resp:
                return _r("gripper", "Gripper link (UART)", "pass", f"{port}: {resp[:60]}")
            return _r("gripper", "Gripper link (UART)", "warn", f"{port} open but no reply",
                      "Check TX/RX wiring and baud 256000")
        except Exception as e:
            return _r("gripper", "Gripper link (UART)", "fail", str(e),
                      "Plug in the USB-UART adapter, or switch link to CAN")
    if not hub.robot.connected:
        return _r("gripper", "Gripper link (CAN)", "skip", "Arm not connected")
    try:
        hub.robot.set_tool("MSG")
        return _r("gripper", "Gripper link (CAN)", "pass", "MSG tool selected on controller",
                  "Run the gripper motion test to confirm the jaws move")
    except Exception as e:
        return _r("gripper", "Gripper link (CAN)", "fail", str(e), "Check CAN H/L wiring and termination")


def check_camera(hub: RobotHub):
    c = hub.settings.get("camera")
    if c["source"] == "none":
        return _r("camera", "Camera", "skip", "No camera configured")
    if c["source"] == "local":
        camera.configure(c)
        f = camera.grab(timeout=4)
        if f:
            return _r("camera", "Camera (on Pi)", "pass", f"/dev/video{c['device']}, {len(f)//1024} KB frame")
        return _r("camera", "Camera (on Pi)", "fail", camera.error or "No frame",
                  "Check the USB camera is plugged into the Pi")
    try:
        with httpx.stream("GET", c["remote_url"], timeout=4) as r:
            if r.status_code != 200:
                return _r("camera", "Camera (on Dell)", "fail", f"HTTP {r.status_code}")
            for chunk in r.iter_bytes():
                if chunk:
                    return _r("camera", "Camera (on Dell)", "pass", "Stream is live")
        return _r("camera", "Camera (on Dell)", "fail", "Stream sent no data")
    except Exception as e:
        return _r("camera", "Camera (on Dell)", "fail", f"Can't reach {c['remote_url']}",
                  "Start the Brain on the Dell, or set camera source to Local")


def check_brain(hub: RobotHub):
    url = hub.settings.get("brain")["url"].rstrip("/") + "/health"
    try:
        r = httpx.get(url, timeout=3)
        if r.status_code == 200:
            return _r("brain", "Dell Brain", "pass", r.text[:80])
        return _r("brain", "Dell Brain", "warn", f"HTTP {r.status_code}")
    except Exception:
        return _r("brain", "Dell Brain", "warn", "Offline (AI features hidden)",
                  "Fine for now. Start the Brain on the Dell to enable AI")


def check_pi(hub: RobotHub):
    bits, status = [], "pass"
    try:
        t = int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000
        bits.append(f"{t:.0f}°C")
        if t > 75:
            status = "warn"
    except Exception:
        pass
    thr = _sh(["vcgencmd", "get_throttled"])
    if thr:
        val = int(thr.split("=")[-1], 16)
        if val & 0x1:
            status = "fail"
            bits.append("UNDER-VOLTAGE now")
        elif val & 0x50000:
            status = "warn" if status == "pass" else status
            bits.append("under-voltage earlier")
    du = shutil.disk_usage("/")
    bits.append(f"{du.free // 2**30} GB free")
    try:
        mem = dict(l.split(":") for l in open("/proc/meminfo"))
        bits.append(f"{int(mem['MemAvailable'].split()[0])//1024} MB RAM free")
    except Exception:
        pass
    fix = "Use the official 27W USB-C supply" if status != "pass" else ""
    return _r("pi", "Pi health", status, ", ".join(bits), fix)


def check_network(hub: RobotHub):
    try:
        state = open("/sys/class/net/eth0/operstate").read().strip()
    except Exception:
        return _r("network", "Wired network", "skip", "No eth0 (not a Pi?)")
    if state == "up":
        return _r("network", "Wired network", "pass", "Ethernet connected")
    return _r("network", "Wired network", "warn", "On Wi-Fi only", "Plug the Pi into the switch for lower lag")


PASSIVE = [check_api, check_arm_usb, check_controller, check_estop, check_telemetry,
           check_limits, check_gripper, check_camera, check_brain, check_pi, check_network]


def run_all(hub: RobotHub) -> list[dict]:
    out = []
    for fn in PASSIVE:
        try:
            out.append(fn(hub))
        except Exception as e:
            out.append(_r(fn.__name__.replace("check_", ""), fn.__name__, "fail", f"Check crashed: {e}"))
    return out


# ── motion checks (explicit tap only) ──────────────────────────────────────

def motion_test(hub: RobotHub, which: str) -> dict:
    if which == "gripper":
        try:
            hub.gripper("open")
            time.sleep(1.5)
            hub.gripper("set", 0.5)
            time.sleep(1.5)
            hub.gripper("open")
            return _r("m_gripper", "Gripper cycle", "pass", "Sent open → half → open. Did the jaws move?")
        except Exception as e:
            return _r("m_gripper", "Gripper cycle", "fail", str(e))
    if which.startswith("joint"):
        j = int(which[-1])
        if not hub.is_enabled():
            return _r(f"m_{which}", f"J{j} wiggle", "fail", "Motors are disabled", "Tap Enable first")
        try:
            before = hub.robot.get_angles()[j - 1]
            for d in (1, 1, -1, -1):
                hub.jog_joint(j, d, 5)
                time.sleep(0.25)
            after = hub.robot.get_angles()[j - 1]
            return _r(f"m_{which}", f"J{j} wiggle", "pass",
                      f"Moved out and back at 5%. Start {before:.1f}°, end {after:.1f}°")
        except Exception as e:
            return _r(f"m_{which}", f"J{j} wiggle", "fail", str(e))
    return _r("m_unknown", which, "fail", "Unknown test")
