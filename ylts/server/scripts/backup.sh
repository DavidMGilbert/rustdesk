#!/bin/sh
# Nightly backup of the portal database and the RustDesk server keys.
# Example cron (as root): 15 2 * * * /opt/ylts-remote/server/scripts/backup.sh
set -eu
cd "$(dirname "$0")/.."
DEST=${BACKUP_DIR:-/var/backups/ylts-remote}
mkdir -p "$DEST"
STAMP=$(date +%Y%m%d-%H%M)
# Consistent SQLite copy while the portal is running
docker compose exec -T portal python -c "import sqlite3;s=sqlite3.connect('/data/portal.db');d=sqlite3.connect('/data/backup.db');s.backup(d);d.close()"
tar czf "$DEST/ylts-remote-$STAMP.tgz" data/portal/backup.db data/rustdesk/id_ed25519 data/rustdesk/id_ed25519.pub .env
rm -f data/portal/backup.db
find "$DEST" -name 'ylts-remote-*.tgz' -mtime +30 -delete
echo "Backup written to $DEST/ylts-remote-$STAMP.tgz"
