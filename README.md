# EFK Schema Migration

Inventory Elasticsearch mappings, score ECS readiness, and plan Fluentd central log formats.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # set ES_URL / ES_USER / ES_PASSWORD

python discover_prefixes.py
# pin a calendar day of daily indices (YYYY-MM-DD):
INDEX_DATE=2026-07-28 python compare_es_mappings.py

docker compose up -d --build panel   # http://localhost:${PANEL_PORT:-8080}
```

Set `PANEL_PORT` in `.env` to change the **host** published port (maps to container port `8080`).

Compose uses **bridge networking** (no `network_mode: host`). If the container is healthy but the browser cannot open the panel, Docker FORWARD rules on the host may be blocking published ports. As root:

```bash
sudo iptables -I DOCKER-USER -j RETURN
sudo iptables -P FORWARD ACCEPT
docker compose up -d --force-recreate panel
curl -m 3 -I http://127.0.0.1:${PANEL_PORT:-8080}
```

In the panel: pick an **Index day** → **Fetch from Elasticsearch** (loads that day's `*-YYYY.MM.DD` indices). Use ES API port **9200**.

## Docker pipeline

```bash
HOST_UID=$(id -u) HOST_GID=$(id -g) \
  docker compose up --build --abort-on-container-exit --exit-code-from es-mapping-compare

# helpers
PREFIX_FILTER=mic docker compose run --rm es-mapping-compare
docker compose run --rm --entrypoint /docker-entrypoint.sh es-mapping-compare fix-perms
```

## Outputs

Pinned day → `results/<YYYY-MM-DD>/` (e.g. `INDEX_DATE=2026-07-28`).  
`results/latest` → newest run.

| File | What |
|------|------|
| `mapping_comparison.json` | Full cluster analysis report |
| `central_format_readiness.csv` | Per-service format + ECS |
| `all_index_mappings.csv` | One row per field |
| `index_field_counts.csv` | Docs / size / field counts |
| `all_index_mappings.yaml` | All services |
| `index_mappings/<prefix>.yaml` | Per service |

Optional: `RESULTS_RUN=label`, `RESULTS_DIR=/path`.

## Key concepts

**Index pick order:** `<prefix>-logs-today` → `<prefix>-today` → exact → newest wildcard.

**Formats:** `1` ECS native · `2` light mutate · `3` legacy/heavy mutate.

**ECS score:** `N/5` for `@timestamp`, `log.level`, `message`, `service.name`, `host.name`. Legacy names (`level`, `service`, …) do **not** count until remapped. Ready = `5/5`.

**Exit `2`:** schema / ECS gaps found (results still written).

## Layout

`discover_prefixes.py` · `compare_es_mappings.py` · `app.py` · `prefixes.json` · `docker-compose.yml` · `.env`
