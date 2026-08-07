#!/bin/sh
set -eu

db_path="${MEDIAENGINE__STORAGE__DB_PATH:-/data/db/library.db}"
db_dir=$(dirname "$db_path")
deadline=$(( $(date +%s) + 30 ))

while [ ! -d "$db_dir" ] || [ ! -w "$db_dir" ]; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
        echo "database directory is not writable after 30s: $db_dir" >&2
        exit 1
    fi
    sleep 1
done

if [ -f /config/config.yaml ]; then
    mediaengine --config /config/config.yaml migrate
else
    mediaengine migrate
fi

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

if [ -f /config/config.yaml ]; then
    exec mediaengine --config /config/config.yaml serve --host 0.0.0.0 --port 8420
fi
exec mediaengine serve --host 0.0.0.0 --port 8420

