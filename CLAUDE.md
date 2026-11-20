# VOLT v2: guide for Claude sessions

Phone controller for a PAROL6 arm ("VOLT") on a Raspberry Pi 5. See README.md.

## Hard rules
- **All motion goes through `volt/hub.py` (`RobotHub`).** Nothing else calls
  `robot.*` motion methods. Not the server, not AI, not diagnostics.
- **Don't edit `volt/robot.py` or `volt/gripper_serial.py`** without a reason; they're
  the proven v1 hardware layer (parol6 0.2.7 API quirks are documented in robot.py).
- STOP must never take the motion lock or wait on I/O beyond `robot.stop()`.
- Never auto-enable motors by default. Never let the AI send raw joint angles.
- Default to simulator/mock in anything that runs on a dev machine.
- Run `pytest -q` after backend changes.

## Runtime
- Pi: `volt.service`, port 8000, `journalctl -u volt -f`. Settings in `data/settings.json`.
- Dev: `bash scripts/dev.sh` (MockRobot when parol6 isn't installed).
- v1 (`~/parol6-controller`, port 5050) is disabled, not deleted: `scripts/use_v1.sh`.

## Hardware
- Arm board: STM32F446 on `/dev/ttyACM0` (VID 0483).
- Gripper: MSG (STEPFOC). Link is CAN via the controller (tool "MSG") or UART
  (`#Grippos 0-255`, 256000 baud). Chosen in Settings > Gripper.
- Camera: on the Dell by default (Brain stream), or `/dev/video0` on the Pi.
- Pi 192.168.1.155 (eth0), Dell 192.168.1.156.

## Next
- Dell Brain repo implementing docs/BRAIN_API.md (vision, ReSpeaker voice, Claude, memory).
- Re-tune homing for the MSG gripper; measure real joint limits and tighten Settings > Motion.
