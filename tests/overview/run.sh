#!/usr/bin/env bash
# Fixture tests use a new, disposable database on the existing local DB service.
set -euo pipefail
cd "$(dirname "$0")/../.."
test_db="iris_overview_${RANDOM}_test"
cleanup() {
  docker exec iriswebapp_db sh -c 'dropdb -U "$POSTGRES_USER" --if-exists "$1"' sh "$test_db"
}
docker exec iriswebapp_db sh -c 'createdb -U "$POSTGRES_USER" "$1"' sh "$test_db"
trap cleanup EXIT
docker exec iriswebapp_db sh -c 'set -e; pg_dump -U "$POSTGRES_USER" --schema-only iris_db > /tmp/overview-test-schema.sql; psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1" -f /tmp/overview-test-schema.sql >/dev/null; rm /tmp/overview-test-schema.sql' sh "$test_db"
docker run --rm --network iris_backend --env-file .env \
  -e POSTGRES_DB="$test_db" -e IRIS_AUTHENTICATION_TYPE=local -e PYTHONPATH=/iriswebapp \
  -v "$PWD/source:/iriswebapp:ro" -v "$PWD/tests/overview:/test:ro" \
  --entrypoint python ghcr.io/dfir-iris/iriswebapp_app:v2.4.29 /test/test_overview.py
