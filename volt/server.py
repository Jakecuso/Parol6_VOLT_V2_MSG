"""volt/server.py — FastAPI app: REST for actions, one WebSocket for live data + jog.

Auth: every /api and /ws call needs the token from data/settings.json, sent as
the X-Volt-Token header or ?token= (for <img> streams and the WebSocket).
The phone gets it once from the pairing link printed at startup.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
import subprocess
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import ai, diagnostics
from .camera import camera
from .config import ROOT, Settings
from .hub import MotionRefused, RobotHub

WEB = ROOT / "web"
VERSION = "2.0.0"


def _git_version() -> str:
    try:
        return subprocess.run(["git", "log", "-1", "--pretty=%h %cr"], cwd=ROOT,
                              capture_output=True, text=True, timeout=2).stdout.strip()
    except Exception:
        return ""


def create_app(simulate: bool | None = None) -> FastAPI:
    settings = Settings()
    hub = RobotHub(settings, simulate=simulate)
    @asynccontextmanager
    async def lifespan(_app):
        hub.connect_background()
        yield
        hub.stop("server shutdown")

    app = FastAPI(title="VOLT", version=VERSION, lifespan=lifespan)
    app.state.hub = hub
    app.state.settings = settings

    # ── auth ───────────────────────────────────────────────────────────────
    def token_ok(t: str | None) -> bool:
        sec = settings.get("security")
        if not sec.get("require_token", False):
            return True
        return bool(t) and t == sec["token"]

    def auth(request: Request):
        t = request.headers.get("x-volt-token") or request.query_params.get("token")
        if not token_ok(t):
            raise HTTPException(401, "Pair this device first (scan the link from the Pi)")

    @app.exception_handler(MotionRefused)
    async def refused(_, e: MotionRefused):
        return JSONResponse({"ok": False, "error": str(e)}, status_code=409)

    @app.exception_handler(Exception)
    async def crashed(_, e: Exception):
        hub.log(f"Error: {e}", "error")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    def run(fn, *a, **kw):
        return run_in_threadpool(fn, *a, **kw)

    # ── basics ─────────────────────────────────────────────────────────────
    @app.get("/api/hello")
    async def hello():
        return {"name": "VOLT", "version": VERSION, "build": _git_version(),
                "pairing": bool(settings.get("security").get("require_token", False))}

    @app.get("/api/state", dependencies=[Depends(auth)])
    async def state():
        return await run(hub.snapshot)

    @app.get("/api/log", dependencies=[Depends(auth)])
    async def log():
        return list(hub.events)

    @app.post("/api/stop", dependencies=[Depends(auth)])
    async def stop():
        hub.stop("button")
        return {"ok": True}

    @app.post("/api/enable", dependencies=[Depends(auth)])
    async def enable():
        await run(hub.enable)
        return {"ok": True}

    @app.post("/api/home", dependencies=[Depends(auth)])
    async def home():
        await run(hub.home)
        return {"ok": True}

    @app.post("/api/reconnect", dependencies=[Depends(auth)])
    async def reconnect():
        await run(hub.reconnect)
        return {"ok": True}

    @app.post("/api/move", dependencies=[Depends(auth)])
    async def move(body: dict):
        await run(hub.move_joints, body["joints"], body.get("speed", settings.get("motion")["default_speed_pct"]))
        return {"ok": True}

    # ── gripper ────────────────────────────────────────────────────────────
    @app.post("/api/gripper", dependencies=[Depends(auth)])
    async def gripper(body: dict):
        resp = await run(hub.gripper, body.get("action", "set"), body.get("position"))
        return {"ok": True, "resp": str(resp)}

    # ── poses ──────────────────────────────────────────────────────────────
    @app.get("/api/poses", dependencies=[Depends(auth)])
    async def poses():
        return hub.poses()

    @app.post("/api/poses", dependencies=[Depends(auth)])
    async def save_pose(body: dict):
        return {"ok": True, "joints": await run(hub.save_pose, body.get("name", ""))}

    @app.delete("/api/poses/{name}", dependencies=[Depends(auth)])
    async def del_pose(name: str):
        hub.delete_pose(name)
        return {"ok": True}

    @app.post("/api/poses/{name}/go", dependencies=[Depends(auth)])
    async def go_pose(name: str, body: dict | None = None):
        speed = (body or {}).get("speed", settings.get("motion")["default_speed_pct"])
        await run(hub.goto_pose, name, speed)
        return {"ok": True}

    # ── settings + diagnostics ─────────────────────────────────────────────
    @app.get("/api/settings", dependencies=[Depends(auth)])
    async def get_settings():
        return settings.public()

    @app.patch("/api/settings", dependencies=[Depends(auth)])
    async def patch_settings(body: dict):
        import copy
        snap = copy.deepcopy(settings.public())
        before = dict(settings.get("robot"))
        out = settings.update(body)
        changed = {}
        for sec, vals in out.items():
            for k, v in vals.items():
                if snap.get(sec, {}).get(k) != v:
                    changed[f"{sec}.{k}"] = {"from": snap.get(sec, {}).get(k), "to": v}
        if changed:
            hub.journal.add("settings_changed",
                            "Changed " + ", ".join(f"{k} from {c['from']} to {c['to']}" for k, c in changed.items()),
                            changes=changed)
        camera.configure(settings.get("camera"))
        if before != settings.get("robot"):
            await run(hub.reconnect)
        return out

    @app.get("/api/ports", dependencies=[Depends(auth)])
    async def ports():
        try:
            from serial.tools import list_ports
            return [{"device": p.device, "desc": p.description,
                     "vid": f"{p.vid:04x}" if p.vid else ""} for p in list_ports.comports()]
        except Exception:
            return []

    @app.get("/api/diagnostics", dependencies=[Depends(auth)])
    async def diag():
        return await run(diagnostics.run_all, hub)

    @app.post("/api/diagnostics/motion/{which}", dependencies=[Depends(auth)])
    async def diag_motion(which: str):
        return await run(diagnostics.motion_test, hub, which)

    # ── camera ─────────────────────────────────────────────────────────────
    @app.get("/api/camera", dependencies=[Depends(auth)])
    async def cam_info():
        c = settings.get("camera")
        if c["source"] == "remote":
            return {"source": "remote", "url": c["remote_url"]}
        if c["source"] == "local":
            return {"source": "local", "url": "/api/camera/stream"}
        return {"source": "none", "url": ""}

    @app.get("/api/camera/stream", dependencies=[Depends(auth)])
    async def cam_stream():
        camera.configure(settings.get("camera"))
        camera.ensure_started()
        if await run(camera.grab, 4.0) is None:
            raise HTTPException(503, camera.error or "No camera")
        return StreamingResponse(camera.frames(), media_type="multipart/x-mixed-replace; boundary=frame")

    # ── AI ─────────────────────────────────────────────────────────────────
    @app.get("/api/ai/status", dependencies=[Depends(auth)])
    async def ai_status():
        return {"mode": settings.get("ai")["mode"], "brain": await run(ai.brain_online, hub)}

    @app.post("/api/ai/command", dependencies=[Depends(auth)])
    async def ai_command(body: dict):
        return await run(ai.command, hub, body.get("text", ""))

    @app.post("/api/ai/execute", dependencies=[Depends(auth)])
    async def ai_execute(body: dict):
        return await run(ai.execute, hub, body.get("plan", []))

    # ── memory journal (the Dell Brain syncs from here) ─────────────────────
    @app.get("/api/memory", dependencies=[Depends(auth)])
    async def memory(since: int = 0, limit: int = 1000):
        return hub.journal.since(since, min(limit, 5000))

    @app.post("/api/memory/note", dependencies=[Depends(auth)])
    async def memory_note(body: dict):
        text = (body.get("text") or "").strip()[:2000]
        if not text:
            raise MotionRefused("Note is empty")
        joints = await run(hub._angles_or_none)
        return hub.journal.add("note", text, source=body.get("source", "phone"), joints=joints)

    # ── WebSocket: telemetry out, jog in ───────────────────────────────────
    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        if not token_ok(sock.query_params.get("token")):
            await sock.close(code=4401)
            return
        await sock.accept()
        jogging = {"active": False}

        async def pump():
            while True:
                await sock.send_json({"t": "state", **(await run(hub.snapshot))})
                await asyncio.sleep(0.1)  # 10 Hz is plenty for a phone

        task = asyncio.create_task(pump())
        try:
            while True:
                msg = await sock.receive_json()
                kind = msg.get("t")
                try:
                    if kind == "stop":
                        hub.stop("button")
                    elif kind == "jog":
                        jogging["active"] = True
                        speed = msg.get("speed", settings.get("motion")["default_speed_pct"])
                        if msg.get("mode") == "cart":
                            await run(hub.jog_cartesian, msg["axis"], int(msg["dir"]), speed)
                        else:
                            await run(hub.jog_joint, int(msg["joint"]), int(msg["dir"]), speed)
                    elif kind == "jog_end":
                        jogging["active"] = False
                except MotionRefused as e:
                    await sock.send_json({"t": "refused", "error": str(e)})
                except Exception as e:
                    await sock.send_json({"t": "error", "error": str(e)})
        except WebSocketDisconnect:
            pass
        finally:
            task.cancel()
            if jogging["active"]:
                hub.stop("phone disconnected mid-jog")

    # ── web app ────────────────────────────────────────────────────────────
    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.get("/")
    async def index():
        return FileResponse(WEB / "index.html")

    @app.get("/manifest.webmanifest")
    async def manifest():
        return FileResponse(WEB / "manifest.webmanifest", media_type="application/manifest+json")


    return app
