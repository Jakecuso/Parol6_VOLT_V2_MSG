#!/usr/bin/env bash
# Print the phone pairing link again.
cd "$(dirname "$0")/.."
TOKEN="$(python3 -c 'import json;print(json.load(open("data/settings.json"))["security"]["token"])')"
echo "http://$(hostname).local:${VOLT_PORT:-8000}/#pair=$TOKEN"
echo "http://$(hostname -I | awk '{print $1}'):${VOLT_PORT:-8000}/#pair=$TOKEN"
