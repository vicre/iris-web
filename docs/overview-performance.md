# IRIS v2.4.29 Overview performance patch

Branch: `perf/defender-case-overview-v2.4.29`  
Upstream base: `340ce040` (`v2.4.29`)  
Local application image: `iriswebapp_app:v2.4.29-overview.1`

## Purpose and diagnosis

Support a growing IRIS installation with one case per Microsoft Defender incident.
The restored development database contained 8,051 cases, of which 7,575 were open.
The original Overview fetched all open cases and serialized full details before
showing its first 25 rows. Each case triggered separate tag, protagonist, alert,
and note-directory queries. A missing `note_directory(case_id)` index made those
7,575 directory lookups repeatedly scan 55,507 rows.

On 2026-10-06, a read-only benchmark of `get_overview_db(1, False)` measured
63.604 seconds, 30,315 SQL queries and a 22,230,308-byte JSON array. Database cursor
execution accounted for 32.198 seconds, including 24.068 seconds on note folders.
The user subsequently created `idx_note_directory_case_id` and ran ANALYZE.

## Changes and reasons

- Add authenticated `GET /overview/page`. Default 25 rows, maximum 250, with
  DataTables draw/count/page response fields. Counts and rows use the existing
  user's effective case access; denied and inaccessible cases are excluded.
- Apply open/closed selection, global search, per-column search, ordering, and
  SearchBuilder groups in SQL before pagination. Case IDs break sort ties.
  Newest case first is the default. Global search includes SOC ID (`XDR-12345`).
- Return only fields needed by the table. Fetch related rows in batches, reuse the
  serializer, and aggregate task counts for the current page. Large descriptions,
  custom attributes, histories, note directories and protagonists are not loaded
  by the page query.
- Load a full case preview through the existing access-controlled `/case/meta`
  endpoint when its title is clicked.
- Preserve the existing `/overview/filter` full-detail response for API consumers,
  while batching all relationships and protagonists to remove per-case queries.
- CSV **Export filtered** fetches all matching summaries through `/overview/export`
  on demand. A single case SELECT fixes membership, avoiding shifting offsets
  and duplicate rows during incident ingestion.
  **Copy page** explicitly copies only loaded rows. Export uses named summary
  columns and escapes spreadsheet formula prefixes. Related data may reflect concurrent edits during that request; export is not
  a database backup. Its memory and response size grow with the matching cases.
- Debounce text filters and release loading indicators on request failure.
  Abort superseded requests. Include the already bundled DataTables Buttons
  scripts and version the Overview script URL to avoid stale cached JavaScript.
- Provide an idempotent operational index script and a small Docker image overlay.
  No dependency upgrades, credential changes or production data changes.

SearchBuilder retains AND/OR groups and standard text, numeric and ISO-date
conditions. Unfinished rules are ignored until complete; unknown columns and
malformed completed rules are rejected. Negative text filters include empty
values. Header searches use literal case-insensitive substrings (no regex).
Numeric Tasks filters use the completed-task fraction (0–1), treating missing
progress as zero for comparisons; Empty still matches missing progress. Dates use `YYYY-MM-DD`; Open since uses days.
Search values are limited to 256 characters, advanced filters to 50 nodes and four
nested levels. These bounds keep a single request's work predictable.

## Measurements

Read-only backend measurements on the same restored development database, after
the manually added index; these exclude network and browser rendering time:

| Request | Seconds | SQL queries | JSON bytes | Rows |
|---|---:|---:|---:|---:|
| Original full Overview, before index | 63.604 | 30,315 | 22,230,308 | 7,575 |
| Patched first page | 0.064 | 5 | 15,049 | 25 |
| Patched global search `Defender` | 0.151 | 5 | 15,049 | 25 |
| Patched legacy full-detail request | 4.667 | 51 | 22,230,308 | 7,575 |

The old and new first-page workloads intentionally differ: bounded loading is the
main improvement. These are single local measurements, not production latency
promises. The legacy request still grows with total data volume; batch eager loads
use multiple chunks. Case counts and substring filtering also scan matching data;
this patch does not claim constant-time searches at millions of cases.

## One case per Defender incident

The separate `defender-iris-sync` project already associates incidents with the
exact SOC ID `XDR-<incidentId>`, reuses that case, and rejects ambiguous duplicate
links. This patch does not change synchronization policy or enable Defender writes.
Use one scheduled sync writer per configured scope, preserve its state file, and
keep separate development/production state and credentials. The current sync's
default discovery scope is **High** incidents: one case per discovered incident
does not mean all severities are enabled. Change discovery scope deliberately in
that project's configuration if required.

An incident volume increase should be handled through paging and efficient reads,
not by merging unrelated incidents into one case or auto-closing historical cases.
No incident states or existing case data are rewritten by this patch.

## Build and deploy

Run from this branch in the IRIS repository. Take the usual backup before a
production deployment and carry the same patch files/commit to `/opt/iris-web`.

```bash
docker compose -f docker-compose.yml -f docker-compose.overview.yml build app
```

The overlay starts from the official v2.4.29 app image and copies only the changed
Overview files. The regular `docker-compose.yml` is unchanged. Always include the
overlay file in subsequent Compose commands to retain the patched image.

The user's development index is already present. On a database without it:

```bash
docker exec -i iriswebapp_db sh -c \
  'exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d iris_db' \
  < scripts/overview-index.sql
```

The index is concurrent and must run outside an explicit transaction. Check its
existing definition if PostgreSQL reports it already exists. No automatic
migration or index deletion is performed by the app patch.

Deploy the web app, then restart nginx to refresh its upstream container address:

```bash
docker compose -f docker-compose.yml -f docker-compose.overview.yml up -d --no-deps app
docker compose -f docker-compose.yml -f docker-compose.overview.yml restart nginx
```

This does not start the currently stopped development worker or sync timer. Where
a worker is already intentionally running (such as production), recreate it with
the same patched image during the deployment:

```bash
docker compose -f docker-compose.yml -f docker-compose.overview.yml up -d --no-deps worker
```

Verify Overview loads, the next page and searches return expected cases, closed
cases remain optional, and the quick preview opens. Check a restricted user's
visibility before production rollout. Browser network requests should use
`/overview/page`, not `/overview/filter`.

Rollback the app using only the original Compose file:

```bash
docker compose -f docker-compose.yml up -d --no-deps app
docker compose -f docker-compose.yml restart nginx
# Only if the worker was also upgraded and should be running:
docker compose -f docker-compose.yml up -d --no-deps worker
```

Keep the note-directory index during rollback; it is compatible with unpatched
v2.4.29 and was independently added by the operator. No data restoration is needed.

## Verification

Run the targeted PostgreSQL regression suite from the repository root:

```bash
./tests/overview/run.sh
```

It creates a uniquely named disposable database using only the local IRIS schema,
seeds synthetic users/cases/tags/tasks, runs tests in the existing v2.4.29 image,
and drops that disposable database on exit. Tests import the data layer without
running app startup, migrations, background modules or external integrations.
It needs the local `iriswebapp_db`, network `iris_backend`, and a compatible `.env`.

Tests cover authorized counts/rows, denied/missing access, deterministic pagination,
open/closed filtering, global/column/advanced searches, literal wildcard handling,
request bounds, malformed inputs, incomplete filters, NULL values, summary size,
query budgets, legacy detail compatibility, and endpoint authentication.

The upstream test harness in `tests/iris.py` replaces the repository `.env` and runs
`docker compose down --volumes` against fixed container names. Do not run it in the
restored development checkout. Run that suite in a separate disposable Docker
installation. The targeted suite avoids that destructive harness.

## Verified delivery

The 12 PostgreSQL regression tests pass. Browser checks on synthetic data verified
first-page rendering, next-page navigation, global SOC-ID search, numeric advanced
filters, clearing filters, and on-demand preview. The downloaded CSV contained all
60 authorized open fixture cases exactly once while only 25 were displayed.
Independent review identified and verified fixes for incomplete filters, nullable
negative filters, Closed-state labels, and export membership during ingestion.

The patch was deployed to the local development web app on 2026-10-06. An
existing authenticated administrator HTTP request to the running app returned
200, 25 rows, the correct total of 7,575 visible open cases, and a 13,893-byte
response in 0.073 seconds. Nginx is healthy. The local worker remains stopped;
production was not modified.
