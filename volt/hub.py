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

import asyncio
import json
import math
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from .config import DATA, Settings
from .gripper_serial import GripperSerial
from .journal import Journal

POSES_FILE = DATA / "poses.json"
UNHOMED_MAX_SPEED = 10   # % cap for joint jogs before the arm has been homed
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
        self.journal = Journal()          # long-term memory, synced to the Dell Brain
        self._last_refusal = ("", 0.0)
        self.state = "disconnected"       # disconnected|connecting|connected|error
        self.last_error = ""
        self._grip_pos = None
        self._connect_thread = None
        # STOP gets its own thread: the parol6 RobotClient refuses to run on the
        # web server's event loop, and STOP must never queue behind other work.
        self._stopper = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stop")
        self._homed = False               # open-loop arm: angles mean nothing until homed
        # parol6 refuses MSG moves until the "calibrate" tool action has run, and
        # it forgets calibration on every connect (the simulator toggle resets it).
        self._grip_cal = False
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

    def _kill_stale_controller(self):
        """Stop any PAROL6 controller process left over from an earlier connection.

        parol6's Robot.start() *attaches* to a controller that's already running.
        If that one was started in simulator mode it has no serial port, so
        switching it to "real" leaves the arm unconnected (all joints 0.0°, homing
        never starts). Always start a fresh controller with the right port instead.
        """
        if self.is_mock:
            return
        try:
            if subprocess.run(["pgrep", "-f", "parol6.server.cli"], capture_output=True).returncode != 0:
                return
            self.log("Stopping leftover PAROL6 controller", "warn")
            subprocess.run(["pkill", "-f", "parol6.server.cli"], capture_output=True)
            for _ in range(50):                      # wait up to 5 s for it to exit
                time.sleep(0.1)
                if subprocess.run(["pgrep", "-f", "parol6.server.cli"], capture_output=True).returncode != 0:
                    return
            subprocess.run(["pkill", "-9", "-f", "parol6.server.cli"], capture_output=True)
            time.sleep(0.5)
        except FileNotFoundError:
            pass                                     # no pgrep (not Linux): nothing to do

    def connect_background(self):
        """Connect (retrying) without blocking the web server. Never enables motors
        unless robot.auto_enable is on."""
        if self._connect_thread and self._connect_thread.is_alive():
            return

        def _worker():
            self.state = "connecting"
            attempt = 0
            while True:
                attempt += 1
                try:
                    cfg = self.settings.get("robot")      # re-read: Settings may have changed
                    self._kill_stale_controller()
                    self.robot.simulate = cfg["simulate"]
                    self.robot.connect(com_port=cfg["com_port"] or None)
                    self._grip_cal = False
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
        self._homed = False
        self._grip_cal = False
        if self.is_mock:
            self.robot.connect()
            self.state = "connected"
            return
        # swap sim/real cleanly by building a fresh Robot
        self.robot = make_robot(self.settings.get("robot")["simulate"])
        self.connect_background()

    # ── stop / enable ──────────────────────────────────────────────────────
    def stop(self, reason: str = "user"):
        """Halt immediately. Deliberately does not take the motion lock.

        Safe to call from anywhere. From async code (the web server) the halt runs
        on the dedicated stop thread; elsewhere it runs inline and blocks until sent.
        """
        self._enabled = False
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return self._do_stop(reason)
        self._stopper.submit(self._do_stop, reason)
        return True

    def _do_stop(self, reason: str):
        try:
            self._enabled = False
            if self.robot.connected:
                self.robot.stop()
            self.log(f"STOP ({reason})", "warn")
            if reason not in ("reconnect", "server shutdown"):
                self.journal.add("stop", f"Stopped by {reason}", reason=reason,
                                 joints=self._angles_or_none(), busy=self._busy)
        except Exception as e:
            self.log(f"STOP failed: {e}", "error")
        return True

    def enable(self):
        self._need_connection()
        self.robot.resume()
        self._enabled = True
        self.log("Motors enabled")
        self.journal.add("enabled", "Motors enabled", joints=self._angles_or_none())

    def is_enabled(self) -> bool:
        """Enabled = we enabled it, no STOP since, and the E-stop isn't pressed."""
        try:
            return self._enabled and self.robot.connected and bool(self.robot.is_enabled())
        except Exception:
            return False

    # ── guards ─────────────────────────────────────────────────────────────
    def _angles_or_none(self):
        try:
            return [round(a, 2) for a in self.robot.get_angles()] if self.robot.connected else None
        except Exception:
            return None

    def _refuse(self, msg: str):
        """Record a refused motion (throttled so a held jog button doesn't spam) and raise."""
        last, t = self._last_refusal
        if msg != last or time.time() - t > 10:
            self.journal.add("refused", f"Refused: {msg}", reason=msg, joints=self._angles_or_none())
            self._last_refusal = (msg, time.time())
        raise MotionRefused(msg)

    @property
    def homed(self) -> bool:
        """The simulator always knows where it is; the real open-loop arm only after Home."""
        return self.simulate or self._homed

    def _need_homed(self, what: str):
        if not self.homed:
            self._refuse(f"Home the arm first ({what} needs a known position)")

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
                self._refuse(f"J{i} target {a:.1f}° is outside limits [{lo}, {hi}]")

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
        if self.homed:
            cur = self.robot.get_angles()
            lo, hi = self._limits()[joint - 1]
            a = cur[joint - 1]
            if (direction > 0 and a >= hi) or (direction < 0 and a <= lo):
                self._refuse(f"J{joint} at soft limit ({a:.1f}°)")
        else:
            # Before homing the reported angles are meaningless, so soft limits
            # can't be checked. Allow slow joint jogs so you can free the arm.
            speed = min(speed, UNHOMED_MAX_SPEED)
        pulse = self.settings.get("motion")["jog_pulse_s"]
        return self._run_motion(f"jog J{joint}",
                                lambda: self.robot.jog_joint(joint, direction * speed, pulse))

    def jog_cartesian(self, axis: str, direction: int, speed_pct: float):
        axis = axis.upper()
        if axis not in CART_AXES or direction not in (-1, 1):
            raise MotionRefused("Bad jog request")
        self._need_homed("Cartesian jog")
        speed = self._clamp_speed(speed_pct)
        pulse = self.settings.get("motion")["jog_pulse_s"]
        return self._run_motion(f"jog {axis}",
                                lambda: self.robot.jog_cartesian(axis, direction * speed, pulse))

    def move_joints(self, angles, speed_pct: float, ai: bool = False):
        self._need_homed("moving to a position")
        self._check_target(angles)
        speed = self._clamp_speed(speed_pct, ai=ai) / 100.0
        return self._run_motion("move", lambda: self.robot.move_joints(
            [float(a) for a in angles], speed=speed, wait=True))

    def home(self):
        self.log("Homing")
        t = time.time()
        try:
            r = self._run_motion("home", lambda: self.robot.home(wait=True, timeout=90))
        except Exception as e:
            self._homed = False
            self.journal.add("homed", f"Homing FAILED: {e}", ok=False, error=str(e),
                             joints=self._angles_or_none(), sim=self.simulate)
            raise
        self._homed = True
        self.journal.add("homed", f"Homed in {time.time()-t:.1f}s", ok=True, seconds=round(time.time()-t, 1),
                         joints=self._angles_or_none(), sim=self.simulate)
        return r

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

        if link == "can" and action != "calibrate" and not self._grip_cal and not self.is_mock:
            raise MotionRefused("GRIPPER_NOT_CALIBRATED: the gripper needs calibrating after "
                                "each connect (jaws open and close fully once)")

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
        if action == "calibrate":
            self._grip_cal = True
            self.journal.add("gripper_calibrated", f"Gripper calibrated over {link.upper()}",
                             link=link, response=str(resp))
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
        old = self.poses().get(name)
        POSES_FILE.write_text(json.dumps(p, indent=2))
        self.log(f"Saved pose '{name}'")
        self.journal.add("pose_saved",
                         f"Pose '{name}' {'updated' if old else 'saved'}",
                         name=name, joints=p[name], previous=old, pose=self.robot.get_pose())
        return p[name]

    def delete_pose(self, name: str):
        p = self.poses()
        old = p.pop(name, None)
        POSES_FILE.write_text(json.dumps(p, indent=2))
        if old:
            self.journal.add("pose_deleted", f"Pose '{name}' deleted", name=name, joints=old)

    def goto_pose(self, name: str, speed_pct: float, ai: bool = False):
        p = self.poses()
        if name not in p:
            raise MotionRefused(f"No pose named '{name}'")
        self.log(f"Go to pose '{name}'" + (" (AI)" if ai else ""))
        try:
            r = self.move_joints(p[name], speed_pct, ai=ai)
            self.journal.add("pose_used", f"Moved to pose '{name}'" + (" (AI)" if ai else ""),
                             name=name, ok=True, ai=ai, speed=speed_pct)
            return r
        except Exception as e:
            self.journal.add("pose_used", f"Failed to reach pose '{name}': {e}",
                             name=name, ok=False, ai=ai, error=str(e))
            raise

    # ── telemetry ──────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        snap = {
            "state": self.state, "sim": self.simulate, "mock": self.is_mock,
            "enabled": False, "estop": None, "busy": self._busy, "homed": self.homed,
            "joints": None, "pose": None, "gripper": self._grip_pos,
            "gripper_calibrated": self._grip_cal or self.is_mock,
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
                    self.journal.add("estop", "Physical E-stop pressed", joints=snap["joints"], busy=self._busy)
                snap["enabled"] = self.is_enabled()
            except Exception as e:
                snap["error"] = str(e)
        return snap
