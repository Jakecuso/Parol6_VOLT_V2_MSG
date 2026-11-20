"""volt/hub.py — the safety layer. Every motion request goes through RobotHub.

Rules enforced here (the phone, the Dell Brain and the AI all hit these):
  1. STOP always works, never waits on a lock.
  2. One motion at a time. A move/home in progress blocks jogs and other moves.
  3. Every speed is clamped to the Settings speed cap (AI has a lower cap).
  4. Joint targets and jogs are checked against soft joint limits.
  5. Jogs are short pulses: if the phone stops sending (finger lifted, Wi-Fi
     dropped, app closed) the arm stops within one pulse. That's the deadman.
  6. Motors are never auto-enabled unless Settings says so.
"""
from __future__ import annotations

import json
import math
import threading
import time
from collections import deque

from .config import DATA, Settings
from .gripper_serial import GripperSerial

POSES_FILE = DATA / "poses.json"
CART_AXES = ("X", "Y", "Z", "RX", "RY", "RZ")


def make_robot(simulate: bool):
    """Real PAROL6 Robot if the API is installed, otherwise the pure-Python mock."""
    try:
        import parol6  # noqa: F401
        from .robot import Robot
        return Robot(simulate=simulate)
    except Exception as e:
        print(f"[hub] parol6 API not available ({e.__class__.__name__}); using MockRobot")
        from .mock_robot import MockRobot
        return MockRobot()


class MotionRefused(Exception):
    pass


class RobotHub:
    def __init__(self, settings: Settings, simulate: bool | None = None):
        self.settings = settings
        if simulate is not None and settings.data["robot"]["simulate"] != simulate:
            settings.update({"robot": {"simulate": simulate}})
        self.robot = make_robot(settings.get("robot")["simulate"])
        self._motion = threading.Lock()
        self._busy = None                 # name of running motion, or None
        self._uart = None                 # GripperSerial, created on demand
        self.events = deque(maxlen=200)   # recent log lines for the UI
        self.state = "disconnected"       # disconnected|connecting|connected|error
        self.last_error = ""
        self._grip_pos = None
        self._connect_thread = None
        self._enabled = False             # our own flag: halt() disables the controller,
                                          # but the API has no "enabled" readout

    # ── log ────────────────────────────────────────────────────────────────
    def log(self, msg: str, level: str = "info"):
        self.events.append({"t": time.time(), "level": level, "msg": msg})
        print(f"[hub] {level}: {msg}")

    # ── connection ─────────────────────────────────────────────────────────
    @property
    def is_mock(self) -> bool:
        return getattr(self.robot, "is_mock", False)

    @property
    def simulate(self) -> bool:
        return self.is_mock or bool(self.settings.get("robot")["simulate"])

    def connect_background(self):
        """Connect (retrying) without blocking the web server. Never enables motors
        unless robot.auto_enable is on."""
        if self._connect_thread and self._connect_thread.is_alive():
            return
        cfg = self.settings.get("robot")

        def _worker():
            self.state = "connecting"
            attempt = 0
            while True:
                attempt += 1
                try:
                    self.robot.simulate = cfg["simulate"]
                    self.robot.connect(com_port=cfg["com_port"] or None)
                    self.state = "connected"
                    self.log(f"Connected ({'simulator' if self.simulate else 'real arm'})")
                    if cfg.get("auto_enable"):
                        self.enable()
                    return
                except Exception as e:
                    self.last_error = str(e)
                    self.state = "connecting"
                    if attempt == 1 or attempt % 5 == 0:
                        self.log(f"Connect attempt {attempt} failed: {e}", "warn")
                    try:
                        self.robot.close()
                    except Exception:
                        pass
                    time.sleep(4)

        self._connect_thread = threading.Thread(target=_worker, daemon=True, name="connect")
        self._connect_thread.start()

    def reconnect(self):
        self.stop("reconnect")
        try:
            self.robot.close()
        except Exception:
            pass
        self.state = "disconnected"
        self._enabled = False
        if self.is_mock:
            self.robot.connect()
            self.state = "connected"
            return
        # swap sim/real cleanly by building a fresh Robot
        self.robot = make_robot(self.settings.get("robot")["simulate"])
        self.connect_background()

    # ── stop / enable ──────────────────────────────────────────────────────
    def stop(self, reason: str = "user"):
        """Halt immediately. Deliberately does not take the motion lock."""
        try:
            self._enabled = False
            if self.robot.connected:
                self.robot.stop()
            self.log(f"STOP ({reason})", "warn")
        except Exception as e:
            self.log(f"STOP failed: {e}", "error")
        return True

    def enable(self):
        self._need_connection()
        self.robot.resume()
        self._enabled = True
        self.log("Motors enabled")

    def is_enabled(self) -> bool:
        """Enabled = we enabled it, no STOP since, and the E-stop isn't pressed."""
        try:
            return self._enabled and self.robot.connected and bool(self.robot.is_enabled())
        except Exception:
            return False

    # ── guards ─────────────────────────────────────────────────────────────
    def _need_connection(self):
        if not self.robot.connected:
            raise MotionRefused("Arm not connected")

    def _clamp_speed(self, pct: float, ai: bool = False) -> float:
        cap = self.settings.get("motion")["speed_cap_pct"]
        if ai:
            cap = min(cap, self.settings.get("ai")["max_ai_speed_pct"])
        return max(1.0, min(float(cap), abs(float(pct))))

    def _limits(self):
        return self.settings.get("motion")["joint_limits"]

    def _check_target(self, angles):
        if len(angles) != 6 or not all(math.isfinite(float(a)) for a in angles):
            raise MotionRefused("Target must be 6 numbers")
        for i, (a, (lo, hi)) in enumerate(zip(angles, self._limits()), start=1):
            if not lo <= float(a) <= hi:
                raise MotionRefused(f"J{i} target {a:.1f}° is outside limits [{lo}, {hi}]")

    def _run_motion(self, name: str, fn):
        self._need_connection()
        if not self.is_enabled():
            raise MotionRefused("Motors are disabled (or E-stop pressed). Tap Enable")
        if not self._motion.acquire(blocking=False):
            raise MotionRefused(f"Busy: {self._busy}")
        self._busy = name
        try:
            return fn()
        finally:
            self._busy = None
            self._motion.release()

    # ── motion ─────────────────────────────────────────────────────────────
    def jog_joint(self, joint: int, direction: int, speed_pct: float):
        if joint not in range(1, 7) or direction not in (-1, 1):
            raise MotionRefused("Bad jog request")
        speed = self._clamp_speed(speed_pct)
        cur = self.robot.get_angles()
        lo, hi = self._limits()[joint - 1]
        a = cur[joint - 1]
        if (direction > 0 and a >= hi) or (direction < 0 and a <= lo):
            raise MotionRefused(f"J{joint} at soft limit ({a:.1f}°)")
        pulse = self.settings.get("motion")["jog_pulse_s"]
        return self._run_motion(f"jog J{joint}",
                                lambda: self.robot.jog_joint(joint, direction * speed, pulse))

    def jog_cartesian(self, axis: str, direction: int, speed_pct: float):
        axis = axis.upper()
        if axis not in CART_AXES or direction not in (-1, 1):
            raise MotionRefused("Bad jog request")
        speed = self._clamp_speed(speed_pct)
        pulse = self.settings.get("motion")["jog_pulse_s"]
        return self._run_motion(f"jog {axis}",
                                lambda: self.robot.jog_cartesian(axis, direction * speed, pulse))

    def move_joints(self, angles, speed_pct: float, ai: bool = False):
        self._check_target(angles)
        speed = self._clamp_speed(speed_pct, ai=ai) / 100.0
        return self._run_motion("move", lambda: self.robot.move_joints(
            [float(a) for a in angles], speed=speed, wait=True))

    def home(self):
        self.log("Homing")
        return self._run_motion("home", lambda: self.robot.home(wait=True, timeout=90))

    # ── gripper ────────────────────────────────────────────────────────────
    def _gripper_uart(self) -> GripperSerial:
        port = self.settings.get("gripper")["uart_port"] or None
        if self._uart is None or self._uart._port != port:
            if self._uart:
                self._uart.close()
            self._uart = GripperSerial(port=port)
        return self._uart

    def gripper(self, action: str, position: float | None = None):
        """action: open | close | set | calibrate. position 0.0 (open) - 1.0 (closed)."""
        g = self.settings.get("gripper")
        link = g["link"]
        if link == "none":
            raise MotionRefused("No gripper configured (Settings > Gripper)")
        if action == "open":
            position = 0.0
        elif action == "close":
            position = 1.0
        elif action == "set":
            position = max(0.0, min(1.0, float(position or 0)))
        elif action != "calibrate":
            raise MotionRefused("Bad gripper action")

        if link == "uart":
            u = self._gripper_uart()
            resp = u.calibrate() if action == "calibrate" else u.set_position(round(position * 255))
        else:
            self._need_connection()
            if action == "calibrate":
                resp = self.robot.gripper_calibrate()
            else:
                resp = self.robot.gripper_move(position, speed=g["speed"],
                                               current=int(g["current_ma"]))
        if position is not None:
            self._grip_pos = position
        self.log(f"Gripper {action}" + (f" {position:.2f}" if action == "set" else ""))
        return resp

    # ── poses ──────────────────────────────────────────────────────────────
    def poses(self) -> dict:
        try:
            raw = json.loads(POSES_FILE.read_text())
        except Exception:
            return {}
        # accept v1 formats: {"name": [6 angles]} or {"name": {"joints": [...]}}
        out = {}
        for k, v in raw.items():
            if isinstance(v, dict):
                v = v.get("joints") or v.get("angles")
            if isinstance(v, list) and len(v) == 6:
                out[k] = [float(x) for x in v]
        return out

    def save_pose(self, name: str):
        name = name.strip()[:40]
        if not name:
            raise MotionRefused("Pose needs a name")
        self._need_connection()
        p = self.poses()
        p[name] = [round(a, 3) for a in self.robot.get_angles()]
        POSES_FILE.parent.mkdir(parents=True, exist_ok=True)
        POSES_FILE.write_text(json.dumps(p, indent=2))
        self.log(f"Saved pose '{name}'")
        return p[name]

    def delete_pose(self, name: str):
        p = self.poses()
        p.pop(name, None)
        POSES_FILE.write_text(json.dumps(p, indent=2))

    def goto_pose(self, name: str, speed_pct: float, ai: bool = False):
        p = self.poses()
        if name not in p:
            raise MotionRefused(f"No pose named '{name}'")
        self.log(f"Go to pose '{name}'" + (" (AI)" if ai else ""))
        return self.move_joints(p[name], speed_pct, ai=ai)

    # ── telemetry ──────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        snap = {
            "state": self.state, "sim": self.simulate, "mock": self.is_mock,
            "enabled": False, "estop": None, "busy": self._busy,
            "joints": None, "pose": None, "gripper": self._grip_pos,
            "error": self.last_error if self.state != "connected" else "",
        }
        if self.robot.connected:
            try:
                snap["joints"] = [round(a, 2) for a in self.robot.get_angles()]
                snap["pose"] = [round(a, 2) for a in self.robot.get_pose()]
                io = list(getattr(self.robot.get_status(), "io", []) or [])
                snap["estop"] = (io[4] == 0) if len(io) > 4 else None
                if snap["estop"] and self._enabled:
                    self._enabled = False         # E-stop drops enable; re-Enable after release
                    self.log("E-stop pressed", "warn")
                snap["enabled"] = self.is_enabled()
            except Exception as e:
                snap["error"] = str(e)
        return snap
