#!/usr/bin/env bash
# Switch the Pi to VOLT v2 (stops v1, they can't share the arm's USB port).
set -e
sudo systemctl disable --now parol6 2>/dev/null || true
sudo systemctl enable --now volt
echo "v2 is running: http://$(hostname).local:8000"
