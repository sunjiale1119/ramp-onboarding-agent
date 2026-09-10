#!/usr/bin/env bash
set -euo pipefail
umask 077
task_backup_root=/opt/ramp/backups
mkdir -p "$task_backup_root"
task_stamp=$(date -u +%Y%m%dT%H%M%SZ)
task_target="$task_backup_root/ramp-$task_stamp.sql.gz"
docker exec ramp-mysql sh -c 'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" mariadb-dump --user=root --single-transaction --routines --events --hex-blob ramp' | gzip > "$task_target"
gzip -t "$task_target"
sha256sum "$task_target" > "$task_target.sha256"
printf '%s\n' "$task_target"
# No automatic deletion: retention is an explicit operator decision.
