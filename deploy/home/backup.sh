#!/usr/bin/env bash
# Snapshot the training log onto the external backup drive.
#
# Run hourly by health-backup.timer. Each run copies every gymlog*.json in the
# data directory into a dated snapshot directory on the drive — but only when
# something has changed since the newest snapshot, because the log changes
# twice a week and an hourly copy of identical bytes is just noise to restore
# from.
#
# A plain copy is safe without stopping the app: store.py writes via a
# temporary file and a rename, so the file on disk is always one whole
# document, never half of one.
#
# **It refuses to run unless the drive is mounted.** With the drive unplugged,
# its mountpoint is an empty directory on the internal disk, and "backing up"
# into it would succeed silently while protecting nothing.
#
# Snapshot names are UTC timestamps (2026-10-04T201500Z), so they sort in time
# order as plain strings. No `latest` symlink: an external drive formatted
# exFAT or NTFS for use elsewhere cannot hold one.

set -euo pipefail

DATA_DIR="${DATA_DIR:-/srv/health/data}"
BACKUP_MOUNT="${BACKUP_MOUNT:-/mnt/backup}"
BACKUP_DIR="${BACKUP_DIR:-$BACKUP_MOUNT/health}"
# Snapshots older than this are pruned...
KEEP_DAYS="${KEEP_DAYS:-180}"
# ...except the newest KEEP_MIN, always. Without that floor, a drive left
# unplugged for six months would have every snapshot pruned on its return.
KEEP_MIN="${KEEP_MIN:-10}"

fail() {
  echo "backup: $*" >&2
  exit 1
}

mountpoint -q "$BACKUP_MOUNT" || fail "$BACKUP_MOUNT is not mounted; is the backup drive plugged in?"
[ -f "$DATA_DIR/gymlog.json" ] || fail "no $DATA_DIR/gymlog.json to back up"

mkdir -p "$BACKUP_DIR"
# The Garmin session file holds OAuth tokens, so the snapshots are not for
# anyone else on the box. (A no-op on exFAT/NTFS, which have no modes.)
chmod 700 "$BACKUP_DIR" 2>/dev/null || true

stamp="$(date -u +%Y-%m-%dT%H%M%SZ)"
incoming="$BACKUP_DIR/.incoming-$stamp"
trap 'rm -rf "$incoming"' EXIT

# Copy first, then checksum the copy: checksumming the source and copying
# afterwards could record one version and store another if a save landed in
# between.
mkdir "$incoming"
cp -p "$DATA_DIR"/gymlog*.json "$incoming"/
(cd "$incoming" && sha256sum gymlog*.json >SHA256SUMS)

python3 -m json.tool "$incoming/gymlog.json" >/dev/null ||
  fail "$DATA_DIR/gymlog.json is not valid JSON; not snapshotting it"

latest="$(find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -name '20*' | sort | tail -n 1)"
if [ -n "$latest" ] && cmp -s "$incoming/SHA256SUMS" "$latest/SHA256SUMS"; then
  echo "backup: unchanged since $(basename "$latest")"
  exit 0
fi

# -T: fail rather than nest inside an existing directory of the same name.
mv -T "$incoming" "$BACKUP_DIR/$stamp"
echo "backup: wrote $BACKUP_DIR/$stamp"

cutoff="$(date -u -d "-$KEEP_DAYS days" +%Y-%m-%dT%H%M%SZ)"
find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -name '20*' | sort -r | tail -n +"$((KEEP_MIN + 1))" |
  while read -r snapshot; do
    if [[ "$(basename "$snapshot")" < "$cutoff" ]]; then
      rm -rf "$snapshot"
      echo "backup: pruned $(basename "$snapshot")"
    fi
  done
