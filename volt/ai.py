"""volt/ai.py — the Pi side of AI mode.

The thinking (Claude, vision, voice, memory) happens on the Dell Brain. The Pi
only does two things:

  1. Forward your command to the Brain:  POST {brain}/ai/command
        request : {"text", "mode", "allowed_actions", "poses", "state"}
        response: {"reply": str, "plan": [{"action": str, "args": {...}, "why": str}]}

  2. Check and run the plan the Brain sends back. The Brain can only ask for
     these actions, and each one goes through the same RobotHub safety layer
     as your thumbs, with the lower AI speed cap:

        goto_pose  {"name": "home_safe"}
        gripper    {"action": "open"|"close"|"set", "position": 0-1}
        home       {}
        look       {}            (Brain-side only; nothing moves)
        stop       {}            (always allowed)

Modes (Settings > AI):
  off         AI disabled.
  chat        AI talks and looks. Any motion in a plan is dropped.
  supervised  Plan comes back to the phone; you approve it before anything moves.
  autonomous  Plan runs straight away, but only with allowed actions.
"""
from __future__ import annotations

import httpx

from .hub import MotionRefused, RobotHub

ACTIONS = {"goto_pose", "gripper", "home", "look", "stop"}
MOTION = {"goto_pose", "gripper", "home"}


def brain_url(hub: RobotHub, path: str) -> str:
    return hub.settings.get("brain")["url"].rstrip("/") + path


def brain_online(hub: RobotHub) -> bool:
    try:
        return httpx.get(brain_url(hub, "/health"), timeout=1.5).status_code == 200
    except Exception:
        return False


def validate_plan(hub: RobotHub, plan: list) -> tuple[list, list]:
    """Return (ok_steps, rejected_steps_with_reason)."""
    ai = hub.settings.get("ai")
    allowed = set(ai["allowed_actions"]) | {"stop"}
    ok, bad = [], []
    for step in plan or []:
        a = (step or {}).get("action")
        if a not in ACTIONS:
            bad.append({**step, "reason": "unknown action"})
        elif a not in allowed:
            bad.append({**step, "reason": "not allowed in Settings > AI"})
        elif ai["mode"] == "chat" and a in MOTION:
            bad.append({**step, "reason": "chat mode never moves the arm"})
        elif a == "goto_pose" and step.get("args", {}).get("name") not in hub.poses():
            bad.append({**step, "reason": "unknown pose"})
        else:
            ok.append(step)
    return ok, bad


def command(hub: RobotHub, text: str) -> dict:
    ai = hub.settings.get("ai")
    if ai["mode"] == "off":
        return {"reply": "AI mode is off. Turn it on in Settings > AI.", "plan": [], "rejected": []}
    payload = {
        "text": text, "mode": ai["mode"], "allowed_actions": ai["allowed_actions"],
        "poses": list(hub.poses().keys()), "state": hub.snapshot(),
    }
    try:
        r = httpx.post(brain_url(hub, "/ai/command"), json=payload, timeout=60)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return {"reply": f"The Brain on the Dell isn't reachable ({e.__class__.__name__}).",
                "plan": [], "rejected": [], "offline": True}
    ok, bad = validate_plan(hub, data.get("plan", []))
    hub.journal.add("ai_command", f"Asked: {text}. Replied: {data.get('reply', '')}",
                    text=text, reply=data.get("reply", ""), mode=ai["mode"], plan=ok, rejected=bad)
    result = {"reply": data.get("reply", ""), "plan": ok, "rejected": bad,
              "needs_approval": ai["mode"] == "supervised" and any(s["action"] in MOTION for s in ok)}
    if ai["mode"] == "autonomous" and ok:
        result["results"] = execute(hub, ok)
    return result


def execute(hub: RobotHub, plan: list) -> list:
    """Run validated steps in order. Stops at the first failure."""
    ok, bad = validate_plan(hub, plan)
    if bad:
        return [{"step": s, "ok": False, "error": s["reason"]} for s in bad]
    out = []
    speed = hub.settings.get("ai")["max_ai_speed_pct"]
    for step in ok:
        a, args = step["action"], step.get("args", {}) or {}
        try:
            if a == "stop":
                hub.stop("AI")
            elif a == "home":
                hub.home()
            elif a == "goto_pose":
                hub.goto_pose(args["name"], speed, ai=True)
            elif a == "gripper":
                hub.gripper(args.get("action", "set"), args.get("position"))
            out.append({"step": step, "ok": True})
        except (MotionRefused, Exception) as e:
            out.append({"step": step, "ok": False, "error": str(e)})
            hub.stop("AI step failed")
            break
    hub.journal.add("ai_executed",
                    "AI ran " + ", ".join(("ok " if r["ok"] else "FAILED ") + r["step"]["action"] for r in out),
                    results=out)
    return out
