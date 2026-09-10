#!/usr/bin/env bash
# Run BEFORE replacing the existing code or container. No volume deletion.
set -euo pipefail
umask 077
test -d /opt/ramp/deploy
test -f /opt/ramp/deploy/.env
mkdir -p /opt/ramp/backups
chmod 700 /opt/ramp/backups
task_stamp=$(date -u +%Y%m%dT%H%M%SZ)
task_image="ramp:pre-pilot-$task_stamp"
docker image tag "$(docker inspect ramp --format '{{.Image}}')" "$task_image"
tar --exclude='./backups' --exclude='./runtime' --exclude='./.git' -czf "/opt/ramp/backups/code-$task_stamp.tar.gz" -C /opt/ramp .
printf '%s\n' "$task_image" > "/opt/ramp/backups/image-$task_stamp.txt"
if [ ! -d /opt/ramp/runtime/reports ]; then
  mkdir -p /opt/ramp/runtime/reports
  docker cp ramp:/app/ramp/eval/reports/. /opt/ramp/runtime/reports/
fi
tar -czf "/opt/ramp/backups/reports-$task_stamp.tar.gz" -C /opt/ramp/runtime reports
task_backup=$(bash /opt/ramp/deploy/backup.sh)
bash /opt/ramp/deploy/verify-backup.sh "$task_backup"
printf 'Previous image: %s\nDatabase backup: %s\n' "$task_image" "$task_backup"
