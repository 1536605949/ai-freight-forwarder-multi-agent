#!/usr/bin/env sh
set -eu
TS=$(date +%Y%m%d_%H%M%S); mkdir -p backups/$TS
if [ -n "${POSTGRES_CONTAINER:-}" ]; then docker exec "$POSTGRES_CONTAINER" pg_dump -U "${POSTGRES_USER:-freight}" "${POSTGRES_DB:-freight}" > "backups/$TS/freight.sql"; fi
cp deployment/.env.example "backups/$TS/env_schema.example"
echo "Backup written to backups/$TS"
