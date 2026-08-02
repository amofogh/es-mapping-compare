# EFK Schema Migration

Inventory Elasticsearch mappings (Stage ± Beta), score ECS readiness, and plan Fluentd central log formats.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # set STAGE_ES_* (and optional BETA_ES_*)

python discover_prefixes.py
# pin a calendar day of daily indices (YYYY-MM-DD):
INDEX_DATE=2026-07-28 ENABLE_BETA=false python compare_es_mappings.py

docker compose --profile panel up -d --build panel   # http://localhost:8501
```

In the panel: pick an **Index day** → **Fetch from Elasticsearch** (loads that day's `*-YYYY.MM.DD` indices). Use ES API port **9200**.

## Docker pipeline

```bash
HOST_UID=$(id -u) HOST_GID=$(id -g) \
  docker compose up --build --abort-on-container-exit --exit-code-from es-mapping-compare

# helpers
PREFIX_FILTER=mic docker compose run --rm es-mapping-compare
ENABLE_BETA=true docker compose up --abort-on-container-exit --exit-code-from es-mapping-compare
docker compose run --rm --entrypoint /docker-entrypoint.sh es-mapping-compare fix-perms
```

## Outputs

Pinned day → `results/<YYYY-MM-DD>/` (e.g. `INDEX_DATE=2026-07-28`).  
`results/latest` → newest run.

| File | What |
|------|------|
| `mapping_comparison.json` | Full report |
| `central_format_readiness.csv` | Per-service format + ECS |
| `all_index_mappings.csv` | One row per field |
| `index_field_counts.csv` | Docs / size / beta status |
| `all_index_mappings.yaml` | All services |
| `index_mappings/<prefix>.yaml` | Per service |

Optional: `RESULTS_RUN=label`, `RESULTS_DIR=/path`.

## Key concepts

**Index pick order:** `<prefix>-logs-today` → `<prefix>-today` → exact → newest wildcard.

**Formats:** `1` ECS native · `2` light mutate · `3` legacy/heavy mutate.

**ECS score:** `N/5` for `@timestamp`, `log.level`, `message`, `service.name`, `host.name`. Legacy names (`level`, `service`, …) do **not** count until remapped. Ready = `5/5`.

**`ENABLE_BETA=false`:** Stage-only (default). **`true`:** compare Stage↔Beta.

**Exit `2`:** schema drift / type mismatches (results still written).

## Layout

`discover_prefixes.py` · `compare_es_mappings.py` · `app.py` · `prefixes.json` · `docker-compose.yml` · `.env`
