#!/usr/bin/env sh
set -eu
[ $# -eq 1 ] || { echo "Usage: $0 backups/<timestamp>/freight.sql"; exit 2; }
: "${POSTGRES_CONTAINER:?set POSTGRES_CONTAINER}"
cat "$1" | docker exec -i "$POSTGRES_CONTAINER" psql -U "${POSTGRES_USER:-freight}" "${POSTGRES_DB:-freight}"
