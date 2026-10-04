#!/usr/bin/env bash
# Install or update the home deployment from the directory this script is in.
#
# Runs on the PC, as root. `make install-home` copies deploy/home over and runs
# it there; it is safe to re-run after any change to the files beside it. It
# never touches /opt/health/.env once that exists, nor the data directory's
# contents.

set -euo pipefail

[ "$(id -u)" -eq 0 ] || {
  echo "install: run as root" >&2
  exit 1
}
cd "$(dirname "$0")"

install -d -m 0755 /opt/health
install -m 0644 compose.yaml /opt/health/compose.yaml
install -m 0755 backup.sh /opt/health/backup.sh
install -m 0644 systemd/*.service systemd/*.timer /etc/systemd/system/

# Owned by 10001, the image's USER. Root-owned, the app starts fine and then
# fails on the first save, which is the worst moment to find out.
install -d -o 10001 -g 10001 -m 0700 /srv/health/data

if [ ! -f /opt/health/.env ]; then
  install -m 0600 .env.example /opt/health/.env
  echo "install: created /opt/health/.env from the template. Fill it in, then:"
  echo "         cd /opt/health && docker compose up -d"
fi

systemctl daemon-reload
systemctl enable --now health-insight.timer health-garmin.timer health-backup.timer
