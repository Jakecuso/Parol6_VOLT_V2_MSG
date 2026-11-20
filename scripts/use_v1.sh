#!/usr/bin/env bash
# Switch the Pi back to the old v1 app (parol6-controller on :5050).
set -e
sudo systemctl disable --now volt
sudo systemctl enable --now parol6
echo "v1 is back: http://$(hostname).local:5050   (scripts/use_v2.sh to return)"
