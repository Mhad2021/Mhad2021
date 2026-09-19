#!/bin/sh
# Nightly database backup. Wire into cron on the host:
#   0 2 * * * docker compose exec -T db /usr/local/bin/backup.sh
set -eu

BACKUP_DIR="${BACKUP_DIR:-/var/lib/postgresql/backups}"
RETAIN_DAYS="${RETAIN_DAYS:-30}"
STAMP="$(date +%Y%m%d-%H%M%S)"

mkdir -p "$BACKUP_DIR"
pg_dump -U "${POSTGRES_USER:-presence}" -d "${POSTGRES_DB:-presence}" \
  --format=custom --compress=9 \
  --file "$BACKUP_DIR/presence-$STAMP.dump"

# Attendance records are pay evidence — verify the dump before trusting it.
pg_restore --list "$BACKUP_DIR/presence-$STAMP.dump" > /dev/null

find "$BACKUP_DIR" -name 'presence-*.dump' -mtime "+$RETAIN_DAYS" -delete
echo "Backup written: $BACKUP_DIR/presence-$STAMP.dump"
