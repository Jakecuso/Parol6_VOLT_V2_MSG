"""volt/mock_robot.py — a pure-Python stand-in for volt.robot.Robot.

Used automatically when the PAROL6 API (`parol6`) isn't installed, e.g. on a
laptop or in CI. It has the same public methods as Robot, integrates jog pulses
into fake joint angles, and never touches hardware. On the Pi the real Robot
(with the PAROL6 simulator or real arm) is used instead.
"""
from __future__ import annotations

import threading
import time

HOME = [0.0, -90.0, 180.0, 0.0, 0.0, 180.0]  # PAROL6 standby
MAX_DPS = 60.0  # degrees/second at 100% speed


class _Status:
    def __init__(self, estop_ok=True):
        self.io = [0, 0, 0, 0, 1 if estop_ok else 0]

    def __str__(self):
        return "mock"


class MockRobot:
    is_mock = True

    def __init__(self, simulate: bool = True, **_):
        self.simulate = True
        self._connected = False
        self._enabled = False
        self._angles = list(HOME)
        self._grip = 0.0
        self._lock = threading.Lock()

    # lifecycle
    @property
    def connected(self):
        return self._connected

    def connect(self, com_port=None):
        self._connected = True
        return self

    def connect_async(self, com_port=None, retry_delay=4.0, on_connect=None):
        self.connect()
        if on_connect:
            on_connect(self)

    def reconfigure(self, simulate, com_port=None):
        self.connect()

    def close(self):
        self._connected = False
        self._enabled = False

    # status
    def ping(self):
        return True

    def get_angles(self):
        with self._lock:
            return list(self._angles)

    def get_pose(self, frame="WRF"):
        a = self.get_angles()
        return [200 + a[0], a[1] * 2, 300 + a[2] / 3, a[3], a[4], a[5]]

    def get_status(self):
        return _Status()

    def is_enabled(self):
        return self._enabled

    # control
    def stop(self):
        self._enabled = False

    def disable(self):
        self.stop()

    def resume(self):
        self._enabled = True

    def set_tool(self, name):
        return 0

    def home(self, wait=True, timeout=60.0):
        self._require()
        with self._lock:
            self._angles = list(HOME)

    def jog_joint(self, joint, speed_pct, duration=0.2):
        self._require()
        with self._lock:
            self._angles[joint - 1] += MAX_DPS * speed_pct / 100.0 * duration

    def jog_cartesian(self, axis, speed_pct, duration=0.2, frame="WRF"):
        self._require()
        idx = ["X", "Y", "Z", "RX", "RY", "RZ"].index(axis.upper())
        with self._lock:
            self._angles[idx] += MAX_DPS * speed_pct / 100.0 * duration * 0.5

    def move_joints(self, angles_deg, speed=0.3, wait=True):
        self._require()
        with self._lock:
            self._angles = [float(x) for x in angles_deg]

    # gripper (CAN path)
    def gripper_move(self, position, speed=0.5, current=500, wait=True):
        self._require()
        self._grip = max(0.0, min(1.0, float(position)))
        return 0

    def gripper_open(self, **kw):
        return self.gripper_move(0.0, **kw)

    def gripper_close(self, **kw):
        return self.gripper_move(1.0, **kw)

    def gripper_calibrate(self):
        return 0

    def _require(self):
        if not self._connected:
            raise RuntimeError("not connected")
        if not self._enabled:
            raise RuntimeError("motors disabled, tap Enable first")
        time.sleep(0.005)
