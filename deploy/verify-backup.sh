#!/usr/bin/env bash
# Restore only into a program-generated disposable schema; never overwrite ramp.
set -euo pipefail
test "$#" -eq 1
task_dump=$(readlink -f "$1")
case "$task_dump" in /opt/ramp/backups/ramp-*.sql.gz) ;; *) echo 'Unexpected backup path'; exit 1;; esac
test -f "$task_dump"
gzip -t "$task_dump"
task_schema="ramp_restore_$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"
[[ "$task_schema" =~ ^ramp_restore_[0-9a-f]{16}$ ]]
sql() { docker exec ramp-mysql sh -c 'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" mariadb -uroot -N -e "$1"' sh "$1"; }
cleanup() { sql "DROP DATABASE IF EXISTS \`$task_schema\`" >/dev/null; }
trap cleanup EXIT
sql "CREATE DATABASE \`$task_schema\` CHARACTER SET utf8mb4"
gzip -dc "$task_dump" | docker exec -i ramp-mysql sh -c 'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" mariadb -uroot "$1"' sh "$task_schema"
task_tables=$(sql "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='$task_schema'")
test "$task_tables" -gt 0
sql "SELECT COUNT(*) FROM \`$task_schema\`.users" >/dev/null
printf 'PASS: backup restored into isolated schema (%s tables); application data unchanged.\n' "$task_tables"
