#!/usr/bin/env bash
# Print the link to open VOLT on a phone (with the pairing token if pairing is on).
cd "$(dirname "$0")/.."
read -r REQ TOKEN < <(python3 -c 'import json;s=json.load(open("data/settings.json"))["security"];print(int(s.get("require_token",False)), s["token"])')
SUFFIX=""
[ "$REQ" = "1" ] && SUFFIX="/#pair=$TOKEN"
echo "   http://$(hostname).local:${VOLT_PORT:-8000}$SUFFIX"
echo "   http://$(hostname -I | awk '{print $1}'):${VOLT_PORT:-8000}$SUFFIX"
