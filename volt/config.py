"""volt/config.py — persistent settings, stored in data/settings.json.

Everything the Settings screen can change lives here. Defaults are the SAFE
choice: simulator on, low speed cap, AI mode off.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("VOLT_DATA", ROOT / "data"))
SETTINGS_FILE = DATA / "settings.json"

DEFAULTS: dict = {
    "robot": {
        # Env var VOLT_SIMULATE (set by the systemd unit) wins over this at boot.
        "simulate": True,
        "com_port": "",            # blank = auto-detect /dev/ttyACM*
        "auto_enable": False,      # enable motors automatically after connect
    },
    "motion": {
        "speed_cap_pct": 30,       # hard ceiling for every jog/move, 1-100
        "default_speed_pct": 15,
        "jog_pulse_s": 0.15,       # each jog message moves for this long (deadman)
        # Soft joint limits in degrees (J1..J6): the official PAROL6 limits
        # (PAROL6_ROBOT.py) pulled in 2° each side. Tighten if the MSG gripper
        # or cables hit anything.
        "joint_limits": [[-121.0, 121.0], [-143.0, -5.4], [109.9, 285.9],
                         [-103.5, 103.5], [-88.0, 88.0], [2.0, 358.0]],
    },
    "gripper": {
        # "can"  = through the arm controller (parol6 tool "MSG")
        # "uart" = direct USB-UART to the STEPFOC (#Grippos)
        # "none" = no gripper attached
        "link": "can",
        "uart_port": "",           # blank = auto-detect /dev/ttyUSB*
        "speed": 0.5,
        "current_ma": 400,
    },
    "camera": {
        # "local"  = USB camera on this Pi
        # "remote" = MJPEG stream from the Dell Brain (e.g. http://192.168.1.156:8100/stream)
        # "none"
        "source": "remote",
        "device": 0,
        "remote_url": "http://192.168.1.156:8100/camera/stream",
        "width": 640, "height": 480, "fps": 15,
    },
    "brain": {
        "url": "http://192.168.1.156:8100",
    },
    "ai": {
        # off        - no AI
        # chat       - AI can answer and look, never moves the arm
        # supervised - AI proposes motion, you approve each step on the phone
        # autonomous - AI may run approved primitives (poses, gripper) on its own
        "mode": "off",
        "voice": False,            # wake word + spoken replies via the Brain's mic
        "speak_replies": True,
        "max_ai_speed_pct": 15,    # AI-driven moves never exceed this
        "allowed_actions": ["goto_pose", "gripper", "home", "look"],
        "model": "claude",
    },
    "security": {
        # Off: anyone on the home network can open the app (your choice for home).
        # On: each phone must open the pairing link once. Turn on for fairs/shared
        # Wi-Fi with `bash scripts/pairing.sh on`. Not changeable from the phone.
        "require_token": False,
        "token": "",               # generated on first run; used when pairing is on
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Settings:
    def __init__(self, path: Path = SETTINGS_FILE):
        self.path = path
        self._lock = threading.Lock()
        self.data = deepcopy(DEFAULTS)
        self.load()

    def load(self):
        if self.path.exists():
            try:
                self.data = _merge(DEFAULTS, json.loads(self.path.read_text()))
            except Exception as e:
                print(f"[settings] bad settings.json, using defaults: {e}")
        if not self.data["security"]["token"]:
            self.data["security"]["token"] = secrets.token_urlsafe(16)
            self.save()

    def save(self):
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2))
            tmp.replace(self.path)

    def get(self, section: str) -> dict:
        return self.data[section]

    def update(self, patch: dict) -> dict:
        """Apply a partial update from the UI. Security section is read-only here."""
        patch = {k: v for k, v in (patch or {}).items() if k in DEFAULTS and k != "security"}
        self.data = _merge(self.data, patch)
        m = self.data["motion"]
        m["speed_cap_pct"] = max(1, min(100, int(m["speed_cap_pct"])))
        m["default_speed_pct"] = max(1, min(m["speed_cap_pct"], int(m["default_speed_pct"])))
        a = self.data["ai"]
        a["max_ai_speed_pct"] = max(1, min(m["speed_cap_pct"], int(a["max_ai_speed_pct"])))
        if a["mode"] not in ("off", "chat", "supervised", "autonomous"):
            a["mode"] = "off"
        self.save()
        return self.public()

    def public(self) -> dict:
        d = deepcopy(self.data)
        d.pop("security", None)
        return d
