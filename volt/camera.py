"""volt/camera.py — local USB camera as an MJPEG stream.

Only used when Settings > Camera > Source is "local" (camera plugged into the
Pi, e.g. travel mode). At home the camera is on the Dell and the phone loads
the Brain's stream URL directly.

OpenCV is imported lazily so the app still runs without it.
"""
from __future__ import annotations

import threading
import time

IDLE_STOP_SEC = 5.0


class LocalCamera:
    def __init__(self):
        self._lock = threading.Lock()
        self._frame = None
        self._running = False
        self._last_view = 0.0
        self.error = ""
        self.cfg = {"device": 0, "width": 640, "height": 480, "fps": 15}

    @property
    def running(self):
        return self._running

    def configure(self, cfg: dict):
        new = {k: cfg[k] for k in ("device", "width", "height", "fps") if k in cfg}
        if new != {k: self.cfg[k] for k in new}:
            self.cfg.update(new)
            self._running = False  # capture loop exits; next viewer restarts it

    def _loop(self):
        try:
            import cv2
        except Exception as e:
            self.error = f"OpenCV not installed ({e})"
            self._running = False
            return
        c = dict(self.cfg)
        cap = cv2.VideoCapture(int(c["device"]))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, c["width"])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, c["height"])
        cap.set(cv2.CAP_PROP_FPS, c["fps"])
        if not cap.isOpened():
            self.error = f"Could not open /dev/video{c['device']}"
            cap.release()
            self._running = False
            return
        self.error = ""
        interval = 1.0 / max(1, c["fps"])
        while self._running:
            ok, frame = cap.read()
            if ok:
                ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                if ok:
                    with self._lock:
                        self._frame = jpg.tobytes()
            if time.time() - self._last_view > IDLE_STOP_SEC:
                break
            time.sleep(interval)
        cap.release()
        with self._lock:
            self._frame = None
        self._running = False

    def ensure_started(self):
        self._last_view = time.time()
        if not self._running:
            self._running = True
            threading.Thread(target=self._loop, daemon=True, name="camera").start()

    def grab(self, timeout: float = 3.0):
        """Return one JPEG (bytes) or None."""
        self.ensure_started()
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:
                if self._frame:
                    return self._frame
            if not self._running:
                return None
            time.sleep(0.05)
        return None

    def frames(self):
        while True:
            self._last_view = time.time()
            with self._lock:
                f = self._frame
            if f is None and not self._running:
                return
            if f:
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + f + b"\r\n"
            time.sleep(1.0 / max(1, self.cfg["fps"]))


camera = LocalCamera()
