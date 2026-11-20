#!/usr/bin/env bash
# Run VOLT on a laptop/Mac with the fake arm. Nothing real can move.
set -e
cd "$(dirname "$0")/.."
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q -r requirements-dev.txt
python -m volt --sim --port 8000
