"""Run VOLT:  python -m volt [--real | --sim] [--port 8000]

Sim vs real comes from Settings on the phone. Optional overrides:
  VOLT_SIMULATE=0|1
  VOLT_PORT=8000    web port
"""
import argparse
import os
import socket

import uvicorn


def main():
    # Sim/real normally comes from Settings (data/settings.json) so the phone
    # toggle sticks across reboots. --sim / --real or VOLT_SIMULATE override it.
    env = os.environ.get("VOLT_SIMULATE")
    env_sim = None if env is None else env.strip().lower() not in ("0", "false", "no", "off")
    p = argparse.ArgumentParser(description="VOLT v2 robot controller")
    p.add_argument("--real", dest="sim", action="store_false", default=env_sim)
    p.add_argument("--sim", dest="sim", action="store_true")
    p.add_argument("--port", type=int, default=int(os.environ.get("VOLT_PORT", "8000")))
    a = p.parse_args()

    from .server import create_app
    app = create_app(simulate=a.sim)
    token = app.state.settings.get("security")["token"]
    host = socket.gethostname()
    print("─" * 60)
    sim = app.state.hub.simulate
    print(f" VOLT v2  [{'SIMULATOR' if sim else 'REAL ARM (motors stay off until you tap Enable)'}]")
    if app.state.settings.get("security").get("require_token"):
        print(" Pair your phone (open once, then Add to Home Screen):")
        print(f"   http://{host}.local:{a.port}/#pair={token}")
    else:
        print(" Open on your phone (pairing is off), then Add to Home Screen:")
        print(f"   http://{host}.local:{a.port}")
    print("─" * 60)
    uvicorn.run(app, host="0.0.0.0", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
