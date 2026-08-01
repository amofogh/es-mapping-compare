# Elasticsearch Mapping Comparison

Tools to inventory Elasticsearch index field mappings across Stage (and optionally Beta), measure ECS readiness, and group services into Fluentd **central log format** archetypes for migration planning.

## What this project does

1. **Discovers** service index prefixes from a live cluster (`discover_prefixes.py`)
2. **Resolves** the latest daily index per service (prefers `<prefix>-logs-YYYY.MM.DD`)
3. **Compares / analyzes** field mappings and ECS compliance (`compare_es_mappings.py`)
4. **Exports** CSV, JSON, and YAML reports for Fluentd standardization work

Typical questions it answers:

- Which services are already ECS-native?
- Which only need light Fluentd mutate (legacy `level` / `service` → ECS)?
- Which are sparse legacy workers that need heavy parsing?
- How many mapped fields does each index have?

## Project layout

| Path | Purpose |
|------|---------|
| `discover_prefixes.py` | Scan ES, merge prefixes into `prefixes.json` |
| `compare_es_mappings.py` | Main mapping analysis + exports |
| `Dockerfile` / `docker-compose.yml` | Pipeline: `discover-prefixes` → `es-mapping-compare` |
| `prefixes.json` | Curated / discovered service + domain prefixes |
| `results/` | Generated CSV / JSON / YAML reports (gitignored) |
| `.env` / `.env.example` | Cluster URLs and credentials |
| `requirements.txt` | Python dependencies |

### Generated outputs (after a compare run)

All generated files go under **`results/`** (not the project root):

| File | Description |
|------|-------------|
| `results/mapping_comparison.json` | Full machine-readable report |
| `results/all_index_mappings.csv` | One row per field (`project_prefix`, `field_name`, Stage type, ECS flag) |
| `results/central_format_readiness.csv` | Per-service summary + `core_team` + `target_log_format` |
| `results/index_field_counts.csv` | Per-service Stage vs Beta: docs, index size, avg log size (no duplicate columns) |
| `results/all_index_mappings.yaml` | All services in one YAML (Stage/Beta blocks + ES field types + diffs) |
| `results/index_mappings/<prefix>.yaml` | One YAML file per service |

## Prerequisites

- Python 3.10+
- Network access to Elasticsearch **API** port (**9200**, not Kibana **5601**)
- Cluster credentials with permission to `cat.indices` and `indices.get_mapping`

## Setup

```bash
cd codes

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt

cp .env.example .env
# Edit .env with real Stage/Beta URLs and passwords
```

### `.env` variables

| Variable | Description |
|----------|-------------|
| `STAGE_ES_URL` | Stage Elasticsearch HTTP URL (e.g. `http://fs-log-elk:9200`) |
| `STAGE_ES_USER` / `STAGE_ES_PASSWORD` | Stage basic auth |
| `BETA_ES_URL` | Beta Elasticsearch HTTP URL (optional) |
| `BETA_ES_USER` / `BETA_ES_PASSWORD` | Beta basic auth |
| `ES_VERIFY_CERTS` | `true` / `false` (default often `false` for lab TLS) |
| `ES_TIMEOUT` | Request timeout seconds |
| `ENABLE_BETA` | `true` to also inspect Beta (default `false` = Stage-only) |
| `PREFIX_FILTER` | Optional: `mic` / `mic,cd` / `mic-api` — limit compare to team(s) or exact prefixes |
| `DISCOVER_CLUSTER` | `stage` or `beta` for discovery (default `stage`) |

> Use the Elasticsearch API port (**9200**). Pointing at Kibana (**5601**) causes `TransportError(302)`.

## How to run

### 1) Discover / refresh prefixes

```bash
source .venv/bin/activate
python discover_prefixes.py
```

What it does:

- Connects to Stage (or `--cluster beta`)
- Lists indices, ignores system/infra names (`.`, `k8s-`, `fluentd-`, `metrics-`, `apm-`, …)
- Normalizes names (`cd-express-logs-2026.08.01` → `cd-express`)
- **Merges** new prefixes into `prefixes.json` without deleting manual entries
- Prints newly discovered services and team namespaces

Optional:

```bash
python discover_prefixes.py --cluster stage
python discover_prefixes.py --cluster beta
python discover_prefixes.py --dry-run    # print only, do not write
```

### 2) Review `prefixes.json`

```json
{
  "core_domain_prefixes": ["ams", "ats", "cd", "hrm", "ime", "mic", "oms", ...],
  "service_prefixes": ["ams-fundhub", "cd-express", "hrm-asapay-channel-kuber", ...]
}
```

Edit this file to remove noise or pin a curated subset before comparing.

Optional ``beta_prefix_aliases`` maps Stage service names to Beta stream
names when naming differs (e.g. Stage ``mic-ava`` → Beta ``mic-beta-ava``,
or many ``mic-iss.*`` → one ``mic-beta-iss``).

### 3) Run mapping analysis

**Stage-only (recommended default):**

```bash
ENABLE_BETA=false python compare_es_mappings.py
```

**One team only (e.g. mic):**

```bash
PREFIX_FILTER=mic python compare_es_mappings.py
# or with Docker:
PREFIX_FILTER=mic docker compose run --rm es-mapping-compare
```

**Both Beta and Stage:**

```bash
ENABLE_BETA=true python compare_es_mappings.py
```

(`ENABLE_BETA` can also be set in `.env`.)

## Index resolution order

For each service prefix, the comparer picks one index in this order:

1. `<prefix>-logs-YYYY.MM.DD` (today, UTC)
2. `<prefix>-YYYY.MM.DD` (today, UTC)
3. Exact name `<prefix>` (non-rotated indices)
4. Newest match of `<prefix>-logs-*` then `<prefix>-*`

## Log format archetypes (`target_log_format`)

Used in `results/central_format_readiness.csv` / YAML metadata for Fluentd planning:

| Archetype | Meaning |
|-----------|---------|
| **Format 1 (ECS Native)** | Core ECS fields present (`@timestamp`, `log.level`, `message`, `service.name`, `host.name`) |
| **Format 2 (Web API - Standard Mutate)** | Most concepts exist, often via legacy names (`level`, `service`, …) — light Fluentd mutate |
| **Format 3 (Legacy Worker - Heavy Mutate)** | Sparse / unstructured (often only `@timestamp`) — heavier parsing needed |

`core_team` is the root namespace before the first hyphen (e.g. `cd-express` → `cd`).

## ECS score (`ecs_score` / `ecs_ready`)

In YAML (`results/index_mappings/<prefix>.yaml`) and the CLI summary, each Stage/Beta index gets an ECS readiness score.

**`ecs_score: N/5`** means **N of 5 core ECS fields** are present in that index’s mapping:

| # | Required ECS field | Common legacy substitutes (do **not** count toward the score) |
|---|--------------------|----------------------------------------------------------------|
| 1 | `@timestamp` | `time`, `timestamp`, `date` |
| 2 | `log.level` | `level`, `Level`, `severity`, `log_level` |
| 3 | `message` | `msg`, `MessageTemplate`, `log` |
| 4 | `service.name` | `service`, `application`, `Properties.Application` |
| 5 | `host.name` | `hostname`, `host`, `MachineName` |

- **`ecs_ready: true`** only when the score is **5/5** (all five ECS field names present).
- **`ecs_score: 1/5`** usually means only `@timestamp` exists (typical Format 3 / legacy worker logs).
- Legacy field names may still help classify **Format 2** (light Fluentd mutate), but they do not raise `ecs_score` until renamed/mapped to the ECS paths above.

## Feature toggle: `ENABLE_BETA`

| Value | Behavior |
|-------|----------|
| `false` (default) | Skip Beta client/ping entirely; Stage-only mode; Beta columns show `DISABLED` / `N/A` where applicable; missing Beta is **not** treated as schema drift |
| `true` | Connect to both clusters and compare field types / missing fields |

## Notes

- Field counts can **grow during the day** on dynamically mapped indices (new document shapes add fields). Two runs of the same daily index can differ.
- `field_count` counts **primary** mapped fields only. Elasticsearch multi-fields such as `message.keyword` are **excluded** (they are index helpers, not separate log fields).
- `index_size` / `index_size_bytes` are the resolved **daily index** store size (from `_stats`).
- `avg_log_size` ≈ `index_size_bytes / docs_count` (average compressed document size on disk; empty when the index has 0 docs).
- Exit code `2` means schema drift / type mismatches were detected (useful in CI). Exit `0` means clean for the configured mode.

## Run with Docker (one-shot pipeline)

Two services:

1. **`discover-prefixes`** — scans Elasticsearch and merges into `prefixes.json`
2. **`es-mapping-compare`** — runs mapping analysis; **depends on** discover completing successfully

Both write through bind mounts (`./results`, `./prefixes.json`).

```bash
# Ensure .env exists with STAGE_ES_* credentials
cp -n .env.example .env   # if needed
mkdir -p results

docker compose build
# Optional: hand ./results + prefixes.json back to your user after root writes
HOST_UID=$(id -u) HOST_GID=$(id -g) docker compose up --build --abort-on-container-exit --exit-code-from es-mapping-compare

# Or explicitly:
HOST_UID=$(id -u) HOST_GID=$(id -g) docker compose run --rm discover-prefixes
HOST_UID=$(id -u) HOST_GID=$(id -g) docker compose run --rm es-mapping-compare

If `results/` was created by an earlier root/nobody Docker run and local
`python compare_es_mappings.py` hits `PermissionError`, fix once:

```bash
docker compose run --rm --entrypoint /docker-entrypoint.sh es-mapping-compare fix-perms
# or:
docker run --rm -u 0 -v "$PWD/results:/results" alpine \
  chown -R "$(id -u):$(id -g)" /results
```

After a normal `docker compose up`, the entrypoint chowns `results/` to the
owner of `prefixes.json` (or `HOST_UID`/`HOST_GID` if set). Local runs that
still cannot write fall back to `results_local/`.

# Inspect outputs on the host
ls results/
ls results/index_mappings/ | head
```

Notes:

- `./results` → `/app/results`
- `./prefixes.json` → `/app/prefixes.json` (updated by discover)
- `network_mode: host` so internal ES hostnames in `.env` resolve (Linux)
- Compare exit code `2` means schema drift / type mismatches (results still written)

Stage-only (default):

```bash
ENABLE_BETA=false docker compose up --abort-on-container-exit --exit-code-from es-mapping-compare
```

Both clusters:

```bash
ENABLE_BETA=true docker compose up --abort-on-container-exit --exit-code-from es-mapping-compare
```

Run discover only:

```bash
docker compose run --rm discover-prefixes
```

## Quick reference

```bash
# setup (once)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit credentials

# every analysis cycle
python discover_prefixes.py
# edit prefixes.json if needed
ENABLE_BETA=false python compare_es_mappings.py

# inspect results
less results/central_format_readiness.csv
less results/index_field_counts.csv
less results/index_mappings/ams-fundhub.yaml
```
