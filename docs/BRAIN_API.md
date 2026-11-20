# Brain API (what the Dell implements)

The Pi calls the Brain; the Brain never drives motors directly. Default URL:
`http://192.168.1.156:8100` (Settings > AI > Brain URL).

## `GET /health`
200 with any short text, e.g. `{"ok": true, "camera": true, "llm": "claude"}`.

## `GET /camera/stream`
MJPEG (`multipart/x-mixed-replace; boundary=frame`). The phone loads it directly
when Settings > Camera > Source is "Dell".

## `POST /ai/command`
Request from the Pi:
```json
{
  "text": "open the gripper and go to ready",
  "mode": "supervised",
  "allowed_actions": ["goto_pose", "gripper", "home", "look"],
  "poses": ["ready", "home_safe", "over_bin"],
  "state": { "joints": [...], "pose": [...], "enabled": true, "gripper": 0.0, "...": "..." }
}
```
Response:
```json
{
  "reply": "Opening the gripper, then moving to ready.",
  "plan": [
    {"action": "gripper",   "args": {"action": "open"},   "why": "free the jaws"},
    {"action": "goto_pose", "args": {"name": "ready"},    "why": "you asked"}
  ]
}
```

Allowed plan actions (the Pi rejects anything else):

| action | args |
|---|---|
| `goto_pose` | `{"name": <one of poses>}` |
| `gripper` | `{"action": "open" \| "close" \| "set", "position": 0.0-1.0}` |
| `home` | `{}` |
| `look` | `{}` (Brain-side: grab a frame and describe it; nothing moves) |
| `stop` | `{}` |

The Pi then drops steps that aren't allowed in Settings, drops all motion in
`chat` mode, asks the phone for approval in `supervised` mode, and runs the steps
itself (with the AI speed cap) in `autonomous` mode.

## Memory sync (Brain pulls from the Pi)

`GET http://<pi>:8000/api/memory?since=<last_id>&limit=1000` with header
`X-Volt-Token: <token>` returns journal entries newer than `last_id`:

```json
[{"id": 42, "t": 1795216000.1, "kind": "pose_saved",
  "text": "Pose 'ready' updated",
  "data": {"name": "ready", "joints": [...], "previous": [...]}}]
```

The Brain stores `last_id`, embeds `text` (bge-small-en-v1.5 via fastembed),
keeps `data` as structured metadata, and indexes both in SQLite
(sqlite-vec for meaning, FTS5 for exact words). Kinds: pose_saved, pose_deleted,
pose_used, homed, gripper_calibrated, settings_changed, enabled, stop, estop,
refused, ai_command, ai_executed, note.

`POST /api/memory/note {"text": "...", "source": "voice"}` lets the Brain write
a memory onto the Pi (e.g. a spoken "remember that...").
