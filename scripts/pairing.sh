#!/usr/bin/env bash
# Turn phone pairing on or off:  bash scripts/pairing.sh on|off
#   off: anyone on the network can open VOLT (fine at home)
#   on : each phone opens the pairing link once (fairs, shared Wi-Fi)
set -e
cd "$(dirname "$0")/.."
case "${1:-}" in
  on)  V=1 ;;
  off) V=0 ;;
  *)   echo "usage: $0 on|off"; exit 1 ;;
esac
python3 -c '
import json, sys
p = "data/settings.json"
d = json.load(open(p))
d.setdefault("security", {})["require_token"] = sys.argv[1] == "1"
json.dump(d, open(p, "w"), indent=2)
' "$V"
sudo systemctl restart volt
echo "Pairing is now ${1^^}."
bash scripts/pair_link.sh
