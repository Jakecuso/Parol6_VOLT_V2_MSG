#!/usr/bin/env bash
# scripts/setup_pi.sh — install VOLT v2 on the Pi and run it on boot.
#
#   bash scripts/setup_pi.sh          # install / update (safe to re-run)
#   bash scripts/setup_pi.sh --clean  # rebuild the venv from scratch
#
# What it does:
#   • checks the Pi can run the PAROL6 API (64-bit, glibc >= 2.39 = Trixie)
#   • makes a venv and installs the PAROL6 API + VOLT deps
#     (reuses the API already cloned by v1 in ~/parol6-controller if present)
#   • copies your v1 poses over (first run only)
#   • installs volt.service and STOPS the v1 parol6.service, because only one
#     program can talk to the arm's USB port. Go back any time: scripts/use_v1.sh
#   • starts in REAL ARM mode with motors OFF until you tap Enable on the phone
set -euo pipefail

CLEAN=0; [ "${1:-}" = "--clean" ] && CLEAN=1
APPDIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="$(whoami)"
PORT="${VOLT_PORT:-8000}"
V1="$HOME/parol6-controller"
cd "$APPDIR"

echo "── VOLT v2 setup ── dir=$APPDIR user=$RUN_USER port=$PORT"

# ── preflight ───────────────────────────────────────────────
ver_ge() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" = "$2" ]; }
[ "$(uname -m)" = "aarch64" ] || { echo "✗ Needs 64-bit Raspberry Pi OS"; exit 1; }
GLIBC="$(ldd --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -n1)" || true
if [ -n "$GLIBC" ] && ! ver_ge "$GLIBC" "2.39"; then
  echo "✗ glibc $GLIBC is too old for the PAROL6 API. Flash Raspberry Pi OS Trixie (64-bit)."; exit 1
fi
echo "✓ aarch64, glibc ${GLIBC:-?}, $(python3 --version)"

# ── packages ────────────────────────────────────────────────
echo "[1/6] System packages"
sudo apt-get update -qq
sudo apt-get install -y -qq git python3-venv python3-dev avahi-daemon

# ── swap: a 2GB Pi runs out of RAM building the robotics libs and freezes ──
SWAP_MB=2048
CUR_MB=$(( $(awk '/SwapTotal/{print $2}' /proc/meminfo) / 1024 ))
if [ "$CUR_MB" -lt "$SWAP_MB" ]; then
  echo "      swap is ${CUR_MB}MB, adding a ${SWAP_MB}MB swapfile"
  if [ ! -f /swapfile-volt ]; then
    sudo fallocate -l ${SWAP_MB}M /swapfile-volt
    sudo chmod 600 /swapfile-volt
    sudo mkswap /swapfile-volt >/dev/null
  fi
  sudo swapon /swapfile-volt 2>/dev/null || true
  grep -q swapfile-volt /etc/fstab || echo '/swapfile-volt none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
else
  echo "      swap ${CUR_MB}MB, fine"
fi

# ── venv + deps ─────────────────────────────────────────────
echo "[2/6] Python environment"
[ "$CLEAN" = 1 ] && rm -rf .venv
[ -d .venv ] || python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -q --upgrade pip
export MAKEFLAGS="-j1" MAX_JOBS=1

echo "[3/6] PAROL6 API (slow the first time)"
if ! python -c "import parol6" 2>/dev/null; then
  if [ -d "$V1/external/PAROL6-python-API" ]; then
    API="$V1/external/PAROL6-python-API"; echo "      reusing v1's copy at $API"
  else
    mkdir -p external
    [ -d external/PAROL6-python-API ] || git clone --depth 1 https://github.com/PCrnjak/PAROL6-python-API external/PAROL6-python-API
    API="external/PAROL6-python-API"
  fi
  pip install --no-cache-dir "$API"   # not quiet, so you can see it working
fi
pip install --no-cache-dir -q -r requirements.txt
python -c "import parol6" || { echo "✗ parol6 failed to import. Check swap (free -h) and re-run with --clean"; exit 1; }
echo "      ✓ parol6 imports"

# ── data ────────────────────────────────────────────────────
echo "[4/6] Settings + poses"
mkdir -p data
if [ ! -f data/poses.json ] && [ -f "$V1/data/poses.json" ]; then
  cp "$V1/data/poses.json" data/poses.json && echo "      copied your v1 poses"
fi
if [ ! -f data/settings.json ]; then
  python - <<'PY'
from volt.config import Settings
s = Settings()
s.update({"robot": {"simulate": False, "auto_enable": False}})
print("      first run: real arm mode, motors off until you tap Enable")
PY
fi
sudo usermod -aG dialout "$RUN_USER" || true

# ── service ─────────────────────────────────────────────────
echo "[5/6] Service (stopping v1 so it frees the arm's USB port)"
if systemctl list-unit-files | grep -q '^parol6.service'; then
  sudo systemctl disable --now parol6 || true
fi
sudo tee /etc/systemd/system/volt.service >/dev/null <<EOF
[Unit]
Description=VOLT v2 robot controller
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APPDIR
ExecStart=$APPDIR/.venv/bin/python -m volt --port $PORT
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now volt
sudo systemctl restart volt

# ── done ────────────────────────────────────────────────────
echo "[6/6] Done"
sleep 4
TOKEN="$(python -c 'import json;print(json.load(open("data/settings.json"))["security"]["token"])')"
cat <<EOF

────────────────────────────────────────────────────────────
 VOLT v2 is running and starts on every boot.

 On your phone, open this link ONCE (it pairs the phone):
   http://$(hostname).local:$PORT/#pair=$TOKEN
   http://$(hostname -I | awk '{print $1}'):$PORT/#pair=$TOKEN

 Then Share → Add to Home Screen.

   sudo systemctl status volt     journalctl -u volt -f
   bash scripts/pair_link.sh      (show the link again)
   bash scripts/use_v1.sh         (switch back to the old app)
────────────────────────────────────────────────────────────
EOF
