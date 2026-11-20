# VOLT v2

Phone-first controller for **VOLT**, a PAROL6 6-axis arm with an MSG gripper.
Runs on the Raspberry Pi next to the arm. Works on its own (travel mode), and
plugs into the **Dell Brain** at home for camera vision, voice and Claude.

```
Phone ──Wi-Fi──► Pi: VOLT v2 (this repo) ──USB──► PAROL6 controller ──► arm + gripper
                   ▲
                   └── Dell: Brain (vision, voice, Claude, memory)  [next repo]
```

## What's in it

| Tab | What it does |
|---|---|
| **Drive** | Hold-to-jog joints or Cartesian axes, speed slider, gripper, Home |
| **Poses** | Save the current position, go to saved poses (with confirm) |
| **Look** | Camera feed, from the Dell or a camera on the Pi |
| **AI** | Off / Chat / Supervised / Autonomous, talks to the Dell Brain |
| **System** | One-tap hardware check, motion tests, every setting |

STOP is always on screen. ENABLE is separate and never automatic unless you turn it on.

## Safety layer (`volt/hub.py`)

Everything (your thumbs, the Brain, the AI) goes through the same rules:

- **STOP always works** and never waits on anything
- **One motion at a time**
- **Speed cap** from Settings; AI moves have a lower cap of their own
- **Soft joint limits** (official PAROL6 limits minus 2°), checked before every move and jog
- **Deadman jog**: each jog message moves the arm for 0.15 s. Lift your finger, lose
  Wi-Fi or close the app and it stops on its own
- **Motors off at boot** until you tap Enable
- **Pairing token** so nobody else on the Wi-Fi can drive it
- AI can only use actions you allow (`goto_pose`, `gripper`, `home`, `look`), and
  never raw joint angles

## Install on the Pi

```bash
git clone <this repo> ~/volt-v2
cd ~/volt-v2
bash scripts/setup_pi.sh
```

It prints a pairing link. Open it once on your phone, then **Share → Add to Home Screen**.
The installer stops the v1 app (both can't use the arm's USB port at once).
`bash scripts/use_v1.sh` switches back; `bash scripts/use_v2.sh` returns.

## Develop on the Mac (fake arm, nothing moves)

```bash
bash scripts/dev.sh          # then open the link it prints
pytest -q                    # 10 safety/API tests
```

Without the PAROL6 API installed, VOLT uses `volt/mock_robot.py`.

## Layout

```
volt/
  robot.py           PAROL6 wrapper, carried over unchanged from v1
  gripper_serial.py  MSG gripper over UART, carried over unchanged from v1
  hub.py             safety layer: every motion goes through here
  server.py          FastAPI: REST + one WebSocket (telemetry out, jog in)
  diagnostics.py     System check + motion tests
  ai.py              Pi side of AI mode (forwards to the Brain, validates plans)
  camera.py          camera on the Pi (travel mode)
  config.py          settings, saved to data/settings.json
  mock_robot.py      fake arm for development
web/                 the phone app (plain HTML/CSS/JS, no build step)
scripts/             setup_pi.sh, dev.sh, pair_link.sh, use_v1.sh, use_v2.sh
docs/BRAIN_API.md    the contract the Dell Brain implements
```
