#!/usr/bin/env python3
"""
Analyze Elasticsearch index field mappings on the ELK cluster.

Resolves the latest date-based index per project prefix (prefers today's
``<prefix>-logs-YYYY.MM.DD``, then ``<prefix>-YYYY.MM.DD``, then wildcard
fallback), flattens mappings, checks ECS compliance, tags Fluentd
log-format archetypes, and exports CSV/YAML reports for central log
format standardization.

Usage:
  # 1) Discover / merge live prefixes into prefixes.json
  python discover_prefixes.py

  # 2) Review/edit prefixes.json, then analyze cluster mappings
  python compare_es_mappings.py
  INDEX_DATE=2026-07-28 python compare_es_mappings.py

Outputs (under ``results/<run_id>/``):
  - mapping_comparison.json
  - all_index_mappings.csv
  - central_format_readiness.csv
  - index_field_counts.csv
  - all_index_mappings.yaml
  - index_mappings/<prefix>.yaml

Each run writes a unique dated folder (``YYYY-MM-DD_HHMMSS``) so previous
runs are kept for side-by-side comparison. ``results/latest`` is a
symlink to the most recent run. Override the folder name with ``RESULTS_RUN``.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import shutil
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from elasticsearch import Elasticsearch
from elasticsearch.exceptions import NotFoundError, TransportError

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional until deps installed
    load_dotenv = None  # type: ignore[assignment]

logger = logging.getLogger("compare_es_mappings")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _utc_today_index_suffix() -> str:
    return _utc_now().strftime("%Y.%m.%d")


def parse_index_date(value: Optional[str] = None) -> date:
    """
    Parse ``INDEX_DATE`` / panel day into a ``date``.

    Accepts ``YYYY-MM-DD``, ``YYYY.MM.DD``, ``YYYY/MM/DD``. Default: today UTC.
    """
    raw = (value if value is not None else os.environ.get("INDEX_DATE", "")).strip()
    if not raw:
        return _utc_now().date()
    normalized = raw.replace("/", "-").replace(".", "-")
    try:
        return date.fromisoformat(normalized)
    except ValueError as exc:
        raise SystemExit(
            f"Invalid INDEX_DATE={raw!r}. Use YYYY-MM-DD (e.g. 2026-07-28)."
        ) from exc


def index_date_suffix(as_of: Optional[date] = None) -> str:
    """Elasticsearch daily index suffix ``YYYY.MM.DD`` for ``as_of`` (default today)."""
    day = as_of or _utc_now().date()
    return day.strftime("%Y.%m.%d")


def index_date_label(as_of: Optional[date] = None) -> str:
    """Folder / panel label ``YYYY-MM-DD`` for ``as_of`` (default today)."""
    day = as_of or _utc_now().date()
    return day.strftime("%Y-%m-%d")

# Load .env from the script directory (does not override existing env vars).
_ENV_FILE = Path(__file__).resolve().parent / ".env"
if load_dotenv is not None and _ENV_FILE.is_file():
    load_dotenv(dotenv_path=_ENV_FILE, override=False)
elif load_dotenv is None and _ENV_FILE.is_file():
    # Minimal fallback parser when python-dotenv is not installed.
    with open(_ENV_FILE, encoding="utf-8") as _fh:
        for _line in _fh:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _key, _, _val = _line.partition("=")
            _key = _key.strip()
            _val = _val.strip().strip("'").strip('"')
            os.environ.setdefault(_key, _val)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PREFIXES_FILE = Path(__file__).resolve().parent / "prefixes.json"

# Fallback list used only when prefixes.json is missing/empty.
_FALLBACK_SERVICE_PREFIXES: List[str] = [
    "ams-fundhub",
    "ams-fundriskassessment",
    "ams-fundserviceapi",
    "ams-fundservicewcf",
    "ams-issuingbybalanceservice",
    "ams-retirementfund-api",
    "cd-acchubapi",
    "cd-acchubcashflow",
    "cd-accounting",
    "cd-ccms",
    "cd-customerapigateway",
    "cd-einvoicing",
    "cd-express",
    "cd-fundaccounting",
    "cd-keycloak",
    "cd-loginprovidersystem",
    "hrm-asapay-channel-kuber",
    "hrm-asapay-customer-kuber",
    "hrm-asapay-gateway-kuber",
    "hrm-asapay-plus-kuber",
    "hrm-banktransfer-api",
    "hrm-deposit-bank-account-reference",
    "hrm-deposit-newswitch-gateway",
    "hrm-deposit-parsian",
    "hrm-deposit-provider-resalat",
    "hrm-deposit-switch-middleeast",
    "hrm-deposit-switch-tejarat",
    "hrm-directdebit-agent",
    "hrm-directdebit-asabank",
    "hrm-directdebit-channel",
    "hrm-directdebit-gateway",
    "hrm-directfund-agent",
    "hrm-directfund-gateway",
    "hrm-harmony-asaban",
    "hrm-harmony-panel-gateway",
    "hrm-neobank-backend",
    "hrm-netbank-backend",
    "hrm-withdraw-api",
    "ime-imex",
    "mic-myagah",
]


def load_service_prefixes(path: Optional[Path] = None) -> List[str]:
    """Load ``service_prefixes`` from prefixes.json (sorted, deduped)."""
    prefixes_path = path or PREFIXES_FILE
    if not prefixes_path.is_file():
        logger.warning(
            "prefixes.json not found at %s — using built-in fallback list",
            prefixes_path,
        )
        return list(_FALLBACK_SERVICE_PREFIXES)
    try:
        with open(prefixes_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read %s (%s) — using fallback list", prefixes_path, exc)
        return list(_FALLBACK_SERVICE_PREFIXES)

    services = [
        str(p).strip()
        for p in (data.get("service_prefixes") or [])
        if str(p).strip()
    ]
    if not services:
        logger.warning("prefixes.json has empty service_prefixes — using fallback list")
        return list(_FALLBACK_SERVICE_PREFIXES)
    return sorted(set(services))


def load_core_domain_prefixes(path: Optional[Path] = None) -> List[str]:
    """Load ``core_domain_prefixes`` from prefixes.json."""
    prefixes_path = path or PREFIXES_FILE
    if not prefixes_path.is_file():
        return ["ams", "ats", "cd", "ecs", "hrm", "ime", "mic", "oms"]
    try:
        with open(prefixes_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return ["ams", "ats", "cd", "ecs", "hrm", "ime", "mic", "oms"]
    domains = [
        str(p).strip().lower()
        for p in (data.get("core_domain_prefixes") or [])
        if str(p).strip()
    ]
    return sorted(set(domains)) or ["ams", "ats", "cd", "ecs", "hrm", "ime", "mic", "oms"]


def core_team_of(prefix: str) -> str:
    """Derive root team namespace from a service prefix (e.g. cd-express -> cd)."""
    if not prefix:
        return "unknown"
    return prefix.split("-", 1)[0].lower() or "unknown"


def filter_prefixes(
    prefixes: List[str], filter_spec: Optional[str] = None
) -> List[str]:
    """
    Restrict prefixes via ``PREFIX_FILTER`` (comma-separated).

    Examples:
      PREFIX_FILTER=mic              → all ``mic-*`` services
      PREFIX_FILTER=mic,cd           → mic + cd teams
      PREFIX_FILTER=mic-api,mic-ava  → exact service prefixes only
    """
    raw = (
        filter_spec
        if filter_spec is not None
        else os.getenv("PREFIX_FILTER", "")
    ).strip()
    if not raw:
        return prefixes

    tokens = {t.strip().lower() for t in raw.split(",") if t.strip()}
    if not tokens:
        return prefixes

    # Exact service matches (contain '-' or '.') vs bare team namespaces.
    exact = {t for t in tokens if "-" in t or "." in t}
    teams = tokens - exact

    selected = [
        p
        for p in prefixes
        if p.lower() in exact or core_team_of(p) in teams
    ]
    return selected


# Loaded at import time; refreshable via load_service_prefixes() in main().
CORE_INDEX_PREFIXES: List[str] = load_service_prefixes()

# Core ECS fields used for readiness / centralization checks, plus legacy aliases.
ECS_FIELDS = {
    "@timestamp": ["time", "timestamp", "date"],
    "log.level": ["level", "log_level", "severity", "loglevel"],
    "message": ["msg", "log", "log_message"],
    "service.name": ["service", "app", "application", "service_name"],
    "host.name": ["hostname", "host", "host_name"],
}

# Broader ECS path set / namespaces for CSV ``is_ecs_standard`` tagging.
ECS_STANDARD_FIELDS: Set[str] = {
    "@timestamp",
    "message",
    "tags",
    "labels",
    "ecs.version",
    "log.level",
    "log.logger",
    "log.origin.file.name",
    "log.origin.file.line",
    "log.origin.function",
    "service.name",
    "service.type",
    "service.version",
    "service.environment",
    "service.node.name",
    "host.name",
    "host.hostname",
    "host.id",
    "host.ip",
    "host.os.name",
    "host.os.family",
    "host.os.version",
    "trace.id",
    "transaction.id",
    "span.id",
    "event.dataset",
    "event.module",
    "event.action",
    "event.category",
    "event.type",
    "event.outcome",
    "event.created",
    "event.ingested",
    "error.message",
    "error.type",
    "error.stack_trace",
    "http.request.method",
    "http.response.status_code",
    "url.full",
    "url.path",
    "user.name",
    "user.id",
    "source.ip",
    "destination.ip",
    "client.ip",
    "server.ip",
    "agent.name",
    "agent.type",
    "agent.version",
    "container.id",
    "container.name",
    "container.image.name",
    "orchestrator.cluster.name",
    "orchestrator.namespace",
    "process.name",
    "process.pid",
    "cloud.provider",
    "cloud.region",
    "cloud.availability_zone",
}

# Field is ECS-standard if exact match OR under a known ECS root namespace.
ECS_ROOT_NAMESPACES: Tuple[str, ...] = (
    "agent",
    "client",
    "cloud",
    "container",
    "destination",
    "dns",
    "ecs",
    "error",
    "event",
    "file",
    "group",
    "host",
    "http",
    "labels",
    "log",
    "network",
    "observer",
    "orchestrator",
    "organization",
    "package",
    "process",
    "registry",
    "related",
    "rule",
    "server",
    "service",
    "source",
    "span",
    "threat",
    "tls",
    "trace",
    "transaction",
    "url",
    "user",
    "user_agent",
    "vulnerability",
)

RESULTS_ROOT = Path(
    os.environ.get("RESULTS_DIR")
    or (Path(__file__).resolve().parent / "results")
)
# Back-compat alias: historically RESULTS_DIR was the write target.
RESULTS_DIR = RESULTS_ROOT

RUN_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(_\d{6})?$")
RESULT_ARTIFACTS = (
    "mapping_comparison.json",
    "all_index_mappings.csv",
    "central_format_readiness.csv",
    "index_field_counts.csv",
    "all_index_mappings.yaml",
)


def resolve_results_dir(preferred: Optional[Path] = None) -> Path:
    """
    Prefer ``results/``; if Docker left it non-writable for the current user,
    fall back to ``results_local/`` so local runs still succeed.

    Returns the **root** results directory (not a dated run folder).
    """
    base = Path(__file__).resolve().parent
    candidates: List[Path] = []
    if preferred is not None:
        candidates.append(preferred)
    env_dir = os.environ.get("RESULTS_DIR", "").strip()
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.extend([base / "results", base / "results_local"])

    seen: Set[str] = set()
    for path in candidates:
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return path
        except OSError:
            continue

    # Last resort: user-owned directory under the project.
    fallback = base / "results_local"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def new_run_id(now: Optional[datetime] = None) -> str:
    """Return ``YYYY-MM-DD_HHMMSS`` (UTC) for a unique run folder name."""
    stamp = now or datetime.now(timezone.utc)
    return stamp.strftime("%Y-%m-%d_%H%M%S")


def make_run_dir(
    results_root: Path,
    run_id: Optional[str] = None,
    index_day: Optional[date] = None,
) -> Path:
    """
    Create ``results_root/<run_id>/`` for this compare execution.

    Prefer ``INDEX_DATE`` / ``index_day`` as ``YYYY-MM-DD`` so the panel can
    open results by log-index day. ``RESULTS_RUN`` still overrides the name.
    """
    override = (run_id or os.environ.get("RESULTS_RUN", "").strip() or "").strip()
    if override:
        rid = override
    elif index_day is not None or os.environ.get("INDEX_DATE", "").strip():
        rid = index_date_label(index_day or parse_index_date(None))
    else:
        # Unpinned CLI run: keep timestamped folders so re-runs do not clobber.
        rid = new_run_id()
    rid = rid.replace("/", "-").replace("\\", "-")
    run_dir = results_root / rid
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def point_latest_symlink(results_root: Path, run_dir: Path) -> None:
    """Point ``results_root/latest`` at ``run_dir`` (best-effort)."""
    link = results_root / "latest"
    try:
        if link.is_symlink() or link.is_file():
            link.unlink()
        elif link.is_dir() and not link.is_symlink():
            # Do not wipe a real directory named latest.
            return
        link.symlink_to(run_dir.name, target_is_directory=True)
    except OSError as exc:
        logger.warning("Could not update results/latest symlink: %s", exc)


def migrate_legacy_flat_results(results_root: Path) -> Optional[Path]:
    """
    If artifacts sit directly under ``results/`` (pre-dated layout), move them
    into a dated run folder derived from ``generated_at`` or file mtime.
    """
    flat_json = results_root / "mapping_comparison.json"
    if not flat_json.is_file():
        return None

    run_id = new_run_id()
    try:
        with flat_json.open(encoding="utf-8") as fh:
            meta = json.load(fh)
        generated = meta.get("generated_at")
        if generated:
            # e.g. 2026-08-01T14:29:48.438753Z
            dt = datetime.fromisoformat(str(generated).replace("Z", "+00:00"))
            run_id = dt.astimezone(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    except (OSError, ValueError, json.JSONDecodeError, TypeError):
        try:
            run_id = datetime.fromtimestamp(
                flat_json.stat().st_mtime, tz=timezone.utc
            ).strftime("%Y-%m-%d_%H%M%S")
        except OSError:
            pass

    dest = results_root / run_id
    if dest.exists():
        run_id = f"{run_id}_migrated"
        dest = results_root / run_id
    dest.mkdir(parents=True, exist_ok=True)

    moved: List[str] = []
    for name in RESULT_ARTIFACTS:
        src = results_root / name
        if src.is_file():
            shutil.move(str(src), str(dest / name))
            moved.append(name)

    mappings_dir = results_root / "index_mappings"
    if mappings_dir.is_dir() and not mappings_dir.is_symlink():
        shutil.move(str(mappings_dir), str(dest / "index_mappings"))
        moved.append("index_mappings/")

    if moved:
        logger.info(
            "Migrated legacy flat results into %s (%s)",
            dest,
            ", ".join(moved),
        )
        point_latest_symlink(results_root, dest)
        return dest
    return None


OUTPUT_FILE = str(RESULTS_DIR / "mapping_comparison.json")
MAPPINGS_CSV_FILE = str(RESULTS_DIR / "all_index_mappings.csv")
READINESS_CSV_FILE = str(RESULTS_DIR / "central_format_readiness.csv")
FIELD_COUNTS_CSV_FILE = str(RESULTS_DIR / "index_field_counts.csv")
MAPPINGS_YAML_FILE = str(RESULTS_DIR / "all_index_mappings.yaml")
MAPPINGS_YAML_DIR = str(RESULTS_DIR / "index_mappings")

# Date fragments commonly used in ILM / daily indices (YYYY.MM.DD, YYYY-MM-DD, etc.)
_DATE_FRAGMENT_RE = re.compile(
    r"(?P<y>\d{4})[.\-_](?P<m>\d{2})[.\-_](?P<d>\d{2})"
)


# ---------------------------------------------------------------------------
# Client helpers
# ---------------------------------------------------------------------------

def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_url_auth(url: str) -> Tuple[str, Optional[Tuple[str, str]]]:
    """Strip embedded credentials from URL; return clean URL and (user, pass)."""
    parsed = urlparse(url)
    auth: Optional[Tuple[str, str]] = None
    if parsed.username or parsed.password:
        auth = (parsed.username or "", parsed.password or "")
        # Rebuild netloc without credentials
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        clean = parsed._replace(netloc=host).geturl()
        return clean, auth
    return url, None


def _env_first(*names: str) -> Optional[str]:
    for name in names:
        val = os.environ.get(name)
        if val is not None and str(val).strip() != "":
            return val
    return None


def build_es_client(
    url_env: str = "ES_URL",
    user_env: str = "ES_USER",
    password_env: str = "ES_PASSWORD",
    label: str = "Elasticsearch",
    config: Optional[Dict[str, Any]] = None,
) -> Elasticsearch:
    """
    Build an Elasticsearch 7.x client from env vars and/or a config dict.

    Defaults to ``ES_URL`` / ``ES_USER`` / ``ES_PASSWORD``. Falls back to
    legacy ``STAGE_ES_*`` names when the new vars are unset.
    Precedence: config dict overrides environment variables.
    """
    cfg = config or {}
    legacy_url = "STAGE_ES_URL" if url_env == "ES_URL" else None
    legacy_user = "STAGE_ES_USER" if user_env == "ES_USER" else None
    legacy_password = "STAGE_ES_PASSWORD" if password_env == "ES_PASSWORD" else None

    url = cfg.get("url") or _env_first(
        url_env, *( [legacy_url] if legacy_url else [] )
    )
    if not url:
        raise SystemExit(
            f"Missing {label} URL. "
            f"Set {url_env} or pass it in the config dictionary."
        )

    url, url_auth = _parse_url_auth(url)
    parsed = urlparse(url)
    if parsed.port == 5601 or url.rstrip("/").endswith(":5601"):
        logger.warning(
            "%s URL %s uses port 5601 (Kibana). "
            "The Elasticsearch API is usually on port 9200. "
            "TransportError(302) is expected when pointing at Kibana.",
            label,
            url,
        )

    user = cfg.get("user") or _env_first(
        user_env, *( [legacy_user] if legacy_user else [] )
    )
    password = cfg.get("password") or _env_first(
        password_env, *( [legacy_password] if legacy_password else [] )
    )
    http_auth = None
    if user is not None and password is not None:
        http_auth = (user, password)
    elif url_auth:
        http_auth = url_auth

    verify = cfg.get("verify_certs")
    if verify is None:
        verify = _env_bool("ES_VERIFY_CERTS", default=True)

    timeout = int(cfg.get("timeout") or os.environ.get("ES_TIMEOUT", "30"))

    client = Elasticsearch(
        [url],
        http_auth=http_auth,
        verify_certs=verify,
        ssl_show_warn=False,
        timeout=timeout,
        max_retries=2,
        retry_on_timeout=True,
    )
    return client


def _explain_transport_error(exc: BaseException) -> str:
    """Append a hint when the failure looks like Kibana/redirect misconfiguration."""
    msg = f"{type(exc).__name__}: {exc}"
    status = getattr(exc, "status_code", None)
    text = str(exc)
    if status == 302 or "TransportError(302" in text or "(302" in text:
        msg += (
            " — HTTP 302 usually means the URL points at Kibana (port 5601) "
            "or another redirecting proxy. Set ES_URL to "
            "the Elasticsearch API endpoint (typically http://host:9200)."
        )
    return msg


# ---------------------------------------------------------------------------
# Index resolution
# ---------------------------------------------------------------------------

def _extract_date_key(index_name: str) -> Optional[Tuple[int, int, int]]:
    """Return (Y, M, D) from the last date-like fragment in an index name."""
    matches = list(_DATE_FRAGMENT_RE.finditer(index_name))
    if not matches:
        return None
    m = matches[-1]
    try:
        return int(m.group("y")), int(m.group("m")), int(m.group("d"))
    except ValueError:
        return None


def _index_belongs_to_prefix(index_name: str, prefix: str) -> bool:
    """True if index is exactly prefix or ``prefix-<suffix>`` (not a longer prefix)."""
    return index_name == prefix or index_name.startswith(prefix + "-")


def _try_get_index(es: Elasticsearch, index_name: str) -> bool:
    """Return True if the named index exists (open or closed)."""
    try:
        return bool(es.indices.exists(index=index_name))
    except TransportError:
        return False


def resolve_latest_index(
    es: Elasticsearch,
    prefix: str,
    cluster_label: str = "",
    as_of: Optional[date] = None,
) -> Optional[str]:
    """
    Resolve a daily index for ``prefix`` on a cluster.

    Candidate order:
      1. ``<prefix>-logs-YYYY.MM.DD``
      2. ``<prefix>-YYYY.MM.DD``

    When ``as_of`` is set (pinned day), **only** those two shapes are accepted —
    no undated exact-name fallback and no jump to another day.

    When ``as_of`` is ``None``, prefers **today** UTC, then exact ``<prefix>``,
    then the newest ``<prefix>-logs-*`` / ``<prefix>-*`` match.
    """
    label = cluster_label or "cluster"
    day = as_of or _utc_now().date()
    suffix = index_date_suffix(day)
    day_logs = f"{prefix}-logs-{suffix}"
    day_plain = f"{prefix}-{suffix}"

    if _try_get_index(es, day_logs):
        logger.debug("[%s] Using day logs index for '%s': %s", label, prefix, day_logs)
        return day_logs
    if _try_get_index(es, day_plain):
        logger.debug("[%s] Using day index for '%s': %s", label, prefix, day_plain)
        return day_plain

    if as_of is not None:
        # Pinned calendar day: never use undated exact names or another day.
        return None

    # Non-rotated index that matches the prefix exactly (e.g. ``ams-fma``).
    if _try_get_index(es, prefix):
        logger.debug("[%s] Using exact index name for '%s'", label, prefix)
        return prefix

    for pattern in (f"{prefix}-logs-*", f"{prefix}-*"):
        resolved = _resolve_from_wildcard(es, prefix, pattern)
        if resolved:
            logger.debug(
                "[%s] Resolved '%s' via %s -> %s",
                label,
                prefix,
                pattern,
                resolved,
            )
            return resolved

    return None


_INDEX_DAY_RE = re.compile(r"(?:^|-)(\d{4})\.(\d{2})\.(\d{2})(?:$|-)")


def list_available_index_dates(
    es: Elasticsearch,
    prefixes: Optional[List[str]] = None,
    limit_prefixes: int = 8,
) -> List[date]:
    """
    Scan a cluster for daily index suffixes across a sample of service prefixes.
    Returns dates newest-first.
    """
    prefixes = prefixes or load_service_prefixes()
    found: Set[date] = set()
    for prefix in prefixes[: max(1, limit_prefixes)]:
        for pattern in (f"{prefix}-logs-*", f"{prefix}-*"):
            try:
                rows = es.cat.indices(index=pattern, format="json", h="index")
            except Exception:  # noqa: BLE001
                continue
            for row in rows or []:
                name = str((row or {}).get("index") or "")
                if not _index_belongs_to_prefix(name, prefix):
                    continue
                match = _INDEX_DAY_RE.search(name)
                if not match:
                    continue
                try:
                    found.add(
                        date(
                            int(match.group(1)),
                            int(match.group(2)),
                            int(match.group(3)),
                        )
                    )
                except ValueError:
                    continue
    return sorted(found, reverse=True)


def count_indices_for_day(
    es: Elasticsearch,
    prefixes: List[str],
    as_of: date,
) -> int:
    """How many prefixes have an index for the pinned calendar day."""
    hits = 0
    for prefix in prefixes:
        if resolve_latest_index(es, prefix, cluster_label="ES", as_of=as_of):
            hits += 1
    return hits


def _resolve_from_wildcard(
    es: Elasticsearch,
    prefix: str,
    pattern: str,
) -> Optional[str]:
    """Pick the newest open index matching ``pattern`` belonging to ``prefix``."""
    try:
        rows = es.cat.indices(
            index=pattern,
            format="json",
            h="index,status,creation.date",
            s="creation.date:desc",
        )
    except NotFoundError:
        return None
    except TransportError as exc:
        if getattr(exc, "status_code", None) == 404:
            return None
        raise

    if not rows:
        return None

    open_rows = [r for r in rows if str(r.get("status", "open")).lower() == "open"]
    candidates = open_rows or list(rows)

    dated: List[Tuple[Tuple[int, int, int], str]] = []
    for row in candidates:
        name = row.get("index") or ""
        if not name or not _index_belongs_to_prefix(name, prefix):
            continue
        key = _extract_date_key(name)
        if key:
            dated.append((key, name))

    if dated:
        dated.sort(key=lambda t: t[0], reverse=True)
        return dated[0][1]

    for row in candidates:
        name = row.get("index") or ""
        if name and _index_belongs_to_prefix(name, prefix):
            return name
    return None


def is_ecs_standard_field(field_name: str) -> bool:
    """Return True if ``field_name`` matches a known ECS path or namespace."""
    if field_name in ECS_STANDARD_FIELDS or field_name in ECS_FIELDS:
        return True
    if field_name in {"message", "tags"}:
        return True
    for root in ECS_ROOT_NAMESPACES:
        if field_name == root or field_name.startswith(root + "."):
            return True
    return False


FORMAT_1_ECS_NATIVE = "Format 1 (ECS Native)"
FORMAT_2_WEB_API = "Format 2 (Web API - Standard Mutate)"
FORMAT_3_LEGACY_WORKER = "Format 3 (Legacy Worker - Heavy Mutate)"


def classify_log_format_archetype(
    fields: Dict[str, str],
    ecs_info: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Tag a mapping into one of three Fluentd central-format archetypes.

    - Format 1: all core ECS fields present (ecs_ready).
    - Format 2: 3+ core concepts covered via ECS *or* legacy names
      (e.g. level/service) — standard mutate filters.
    - Format 3: sparse / legacy worker logs (typically only @timestamp).
    """
    if not fields:
        return FORMAT_3_LEGACY_WORKER

    ecs_info = ecs_info or check_ecs_compliance(fields)
    if ecs_info.get("ecs_ready"):
        return FORMAT_1_ECS_NATIVE

    # Count core concepts present as either ECS path or a known legacy alias.
    covered = 0
    for _ecs_field, detail in (ecs_info.get("fields") or {}).items():
        status = detail.get("status")
        if status in {"ecs", "legacy"}:
            covered += 1

    if covered >= 3:
        return FORMAT_2_WEB_API

    return FORMAT_3_LEGACY_WORKER


# ---------------------------------------------------------------------------
# Mapping flatten
# ---------------------------------------------------------------------------

def _walk_properties(
    props: Dict[str, Any],
    prefix: str,
    out: Dict[str, str],
    *,
    include_multifields: bool = False,
    multifields_out: Optional[Dict[str, str]] = None,
) -> None:
    """
    Walk mapping properties into dot-notation paths.

    By default **excludes** Elasticsearch multi-fields (``fields.keyword`` etc.),
    which inflate counts without being separate document fields. Set
    ``include_multifields=True`` to merge them into ``out``, or pass
    ``multifields_out`` to collect them separately.
    """
    for field, spec in props.items():
        path = f"{prefix}.{field}" if prefix else field
        if not isinstance(spec, dict):
            continue

        # Multi-fields (e.g. message.keyword) — not primary document fields
        fields = spec.get("fields")
        if isinstance(fields, dict):
            for sub, sub_spec in fields.items():
                if isinstance(sub_spec, dict):
                    sub_type = sub_spec.get("type")
                    if sub_type:
                        mf_path = f"{path}.{sub}"
                        if include_multifields:
                            out[mf_path] = str(sub_type)
                        elif multifields_out is not None:
                            multifields_out[mf_path] = str(sub_type)

        nested = spec.get("properties")
        if isinstance(nested, dict):
            # object / nested with child properties — do not count intermediate
            # object nodes toward field_count; only leaf value fields matter.
            _walk_properties(
                nested,
                path,
                out,
                include_multifields=include_multifields,
                multifields_out=multifields_out,
            )
            continue

        ftype = spec.get("type")
        if ftype:
            out[path] = str(ftype)
        elif "properties" not in spec:
            # Dynamic object without explicit type — treat as object
            out[path] = "object"


def flatten_mapping(
    mapping_response: Dict[str, Any],
    index_name: str,
    *,
    include_multifields: bool = False,
) -> Dict[str, str]:
    """
    Flatten ``indices.get_mapping`` JSON into ``{dot.path: type}``.

    Counts **primary** mapped fields only (excludes ``*.keyword`` multi-fields
    unless ``include_multifields=True``).

    Example: ``{"@timestamp": "date", "log.level": "keyword", "message": "text"}``
    """
    flat: Dict[str, str] = {}
    index_body = mapping_response.get(index_name) or {}
    # ES 7.x: mappings are under "mappings" (no type name for default _doc)
    mappings = index_body.get("mappings") or {}

    def _consume(props: Dict[str, Any]) -> None:
        _walk_properties(
            props,
            "",
            flat,
            include_multifields=include_multifields,
        )

    # Legacy typed mappings (pre-7 style still sometimes present)
    if "properties" in mappings:
        _consume(mappings.get("properties") or {})
        return flat

    for _type_name, type_body in mappings.items():
        if isinstance(type_body, dict) and "properties" in type_body:
            _consume(type_body.get("properties") or {})
    return flat


def fetch_flattened_mapping(
    es: Elasticsearch,
    index_name: str,
    *,
    include_multifields: bool = False,
) -> Dict[str, str]:
    resp = es.indices.get_mapping(index=index_name)
    return flatten_mapping(
        resp, index_name, include_multifields=include_multifields
    )


def human_bytes(num_bytes: Optional[int]) -> Optional[str]:
    """Format byte counts as compact human strings (e.g. ``1.2mb``)."""
    if num_bytes is None:
        return None
    try:
        n = float(num_bytes)
    except (TypeError, ValueError):
        return None
    if n < 0:
        n = 0.0
    units = ("b", "kb", "mb", "gb", "tb", "pb")
    idx = 0
    while n >= 1024.0 and idx < len(units) - 1:
        n /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(n)}{units[idx]}"
    return f"{n:.1f}{units[idx]}"


def empty_index_size_stats() -> Dict[str, Any]:
    return {
        "docs_count": 0,
        "store_size_bytes": 0,
        "pri_store_size_bytes": 0,
        "avg_log_size_bytes": None,
        "store_size": "0b",
        "avg_log_size": None,
    }


def fetch_index_size_stats(
    es: Elasticsearch,
    index_name: Optional[str],
) -> Dict[str, Any]:
    """
    Index (daily) store size + average log/document size.

    Uses primary+replica ``store.size`` and ``docs.count`` from ``_stats``.
    ``avg_log_size_bytes`` ≈ store_size_bytes / docs_count (None if no docs).
    """
    stats = empty_index_size_stats()
    if not index_name or index_name in {"DISABLED", "MISSING"}:
        return stats

    try:
        resp = es.indices.stats(index=index_name, metric="store,docs")
    except NotFoundError:
        return stats
    except TransportError as exc:
        if getattr(exc, "status_code", None) == 404:
            return stats
        logger.warning(
            "Failed to fetch size stats for %s: %s",
            index_name,
            _explain_transport_error(exc),
        )
        return stats
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to fetch size stats for %s: %s", index_name, exc)
        return stats

    indices = (resp or {}).get("indices") or {}
    # Prefer exact name; fall back to the single returned index if aliased.
    body = indices.get(index_name)
    if body is None and len(indices) == 1:
        body = next(iter(indices.values()))
    if not isinstance(body, dict):
        # Aggregate totals as last resort
        total = ((resp or {}).get("_all") or {}).get("total") or {}
        docs_count = int(((total.get("docs") or {}).get("count")) or 0)
        store_bytes = int(((total.get("store") or {}).get("size_in_bytes")) or 0)
        pri_bytes = int(
            ((((resp or {}).get("_all") or {}).get("primaries") or {})
             .get("store") or {})
            .get("size_in_bytes")
            or 0
        )
    else:
        total = body.get("total") or {}
        primaries = body.get("primaries") or {}
        docs_count = int(((total.get("docs") or {}).get("count")) or 0)
        store_bytes = int(((total.get("store") or {}).get("size_in_bytes")) or 0)
        pri_bytes = int(((primaries.get("store") or {}).get("size_in_bytes")) or 0)

    avg: Optional[int] = None
    if docs_count > 0:
        avg = int(round(store_bytes / docs_count))

    return {
        "docs_count": docs_count,
        "store_size_bytes": store_bytes,
        "pri_store_size_bytes": pri_bytes,
        "avg_log_size_bytes": avg,
        "store_size": human_bytes(store_bytes) or "0b",
        "avg_log_size": human_bytes(avg),
    }


def attach_index_size_stats(
    entry: Dict[str, Any],
    es: Elasticsearch,
) -> None:
    """Populate ``index_size`` on an analysis result entry."""
    index_name = entry_index_name(entry)
    entry["index_size"] = (
        fetch_index_size_stats(es, index_name)
        if index_name
        else empty_index_size_stats()
    )


# ---------------------------------------------------------------------------
# ECS analysis helpers
# ---------------------------------------------------------------------------

def entry_index_name(entry: Dict[str, Any]) -> Optional[str]:
    """Resolved index name (supports legacy ``stage_index``)."""
    return entry.get("index_name") or entry.get("stage_index")


def entry_fields(entry: Dict[str, Any]) -> Dict[str, str]:
    """Mapped fields dict (supports legacy ``stage_fields``)."""
    fields = entry.get("fields")
    if fields is None:
        fields = entry.get("stage_fields")
    return fields or {}


def entry_index_size(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Index size stats (supports legacy ``stage_size``)."""
    size = entry.get("index_size")
    if size is None:
        size = entry.get("stage_size")
    return size or empty_index_size_stats()


def normalize_ecs(ecs: Any) -> Dict[str, Any]:
    """Normalize ecs payload (flat or legacy nested ``{"stage": ...}``)."""
    if not isinstance(ecs, dict):
        return {}
    if "stage" in ecs and "ecs_ready" not in ecs:
        nested = ecs.get("stage") or {}
        return nested if isinstance(nested, dict) else {}
    return ecs


def missing_required_ecs_fields(ecs_info: Optional[Dict[str, Any]]) -> List[str]:
    """Return core ECS field names that are not present (status != ecs)."""
    if not ecs_info:
        return list(ECS_FIELDS)
    missing: List[str] = []
    for field, detail in (ecs_info.get("fields") or {}).items():
        if (detail or {}).get("status") != "ecs":
            missing.append(field)
    return missing


def field_issues(
    fields: Dict[str, str],
    ecs_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Cluster schema issues (missing required ECS fields, legacy aliases)."""
    ecs_info = ecs_info or (check_ecs_compliance(fields) if fields else None)
    missing_ecs = missing_required_ecs_fields(ecs_info) if fields else list(ECS_FIELDS)
    legacy_found: List[Dict[str, Any]] = []
    for field, detail in ((ecs_info or {}).get("fields") or {}).items():
        detail = detail or {}
        if detail.get("status") == "legacy":
            legacy_found.append(
                {
                    "ecs_field": field,
                    "legacy_alternatives": list(
                        detail.get("legacy_alternatives_found") or []
                    ),
                }
            )

    return {
        "field_count": len(fields),
        "missing_ecs_fields": missing_ecs,
        "legacy_ecs_alternatives": legacy_found,
        "ecs_fields_present": (ecs_info or {}).get("ecs_fields_present", 0),
        "ecs_fields_total": (ecs_info or {}).get("ecs_fields_total", len(ECS_FIELDS)),
    }


def check_ecs_compliance(fields: Dict[str, str]) -> Dict[str, Any]:
    """
    Flag presence of standard ECS fields vs legacy alternatives.

    Returns per-field status and an overall ``ecs_ready`` boolean
    (true only when all five ECS fields are present).
    """
    present = set(fields)
    details: Dict[str, Any] = {}
    ecs_present_count = 0

    for ecs_field, legacy_names in ECS_FIELDS.items():
        has_ecs = ecs_field in present
        found_legacy = [name for name in legacy_names if name in present]
        if has_ecs:
            ecs_present_count += 1
            status = "ecs"
        elif found_legacy:
            status = "legacy"
        else:
            status = "missing"

        details[ecs_field] = {
            "status": status,
            "present": has_ecs,
            "type": fields.get(ecs_field),
            "legacy_alternatives_found": found_legacy,
        }

    return {
        "ecs_ready": ecs_present_count == len(ECS_FIELDS),
        "ecs_fields_present": ecs_present_count,
        "ecs_fields_total": len(ECS_FIELDS),
        "fields": details,
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def analyze_cluster(
    es: Elasticsearch,
    prefixes: Optional[List[str]] = None,
    as_of: Optional[date] = None,
) -> Dict[str, Any]:
    """Resolve cluster indices, flatten mappings, and score ECS readiness."""
    prefixes = prefixes or CORE_INDEX_PREFIXES
    results: List[Dict[str, Any]] = []
    index_day = index_date_label(as_of) if as_of is not None else None

    for prefix in prefixes:
        entry: Dict[str, Any] = {
            "prefix": prefix,
            "index_name": None,
            "status": "ok",
            "error": None,
            "issues": None,
            "ecs": None,
            "has_schema_drift": False,
            "has_ecs_gaps": False,
            "fields": {},
            "index_size": empty_index_size_stats(),
        }

        try:
            index_name = resolve_latest_index(
                es, prefix, cluster_label="ES", as_of=as_of
            )
            entry["index_name"] = index_name

            if not index_name:
                logger.warning(
                    "Prefix '%s': MISSING on ES cluster — CSV will use MISSING/N/A",
                    prefix,
                )
                entry["status"] = "missing_index"
                entry["error"] = f"No index matching '{prefix}-*' on ES cluster"
                entry["has_schema_drift"] = True
                entry["has_ecs_gaps"] = True
                entry["issues"] = field_issues({})
                entry["ecs"] = None
                attach_index_size_stats(entry, es)
                results.append(entry)
                continue

            fields = fetch_flattened_mapping(es, index_name)
            entry["fields"] = fields
            ecs_info = check_ecs_compliance(fields) if fields else None
            entry["ecs"] = ecs_info
            issues = field_issues(fields, ecs_info=ecs_info)
            entry["issues"] = issues
            entry["has_ecs_gaps"] = bool(issues.get("missing_ecs_fields"))
            entry["has_schema_drift"] = entry["has_ecs_gaps"]
        except Exception as exc:  # noqa: BLE001 — surface per-prefix failures
            entry["status"] = "error"
            entry["error"] = _explain_transport_error(exc)
            entry["has_schema_drift"] = True
            entry["has_ecs_gaps"] = True
            logger.exception("Prefix '%s' failed: %s", prefix, exc)

        attach_index_size_stats(entry, es)
        results.append(entry)

    drifted = [r for r in results if r.get("has_schema_drift")]
    ecs_not_ready = [
        r
        for r in results
        if entry_index_name(r)
        and not normalize_ecs(r.get("ecs")).get("ecs_ready", False)
    ]

    return {
        "generated_at": _utc_now_iso(),
        "index_date": index_day or index_date_label(as_of or _utc_now().date()),
        "index_date_pinned": as_of is not None,
        "mode": "cluster",
        "prefixes_total": len(prefixes),
        "prefixes_with_schema_drift": len(drifted),
        "prefixes_with_ecs_gaps": len(ecs_not_ready),
        "prefixes_not_ecs_ready": len(ecs_not_ready),
        "results": results,
    }


# Back-compat aliases
compare_clusters = analyze_cluster
analyze_stage_cluster = analyze_cluster


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def _bool_csv(value: bool) -> str:
    return "TRUE" if value else "FALSE"


def export_mappings_to_csv(
    report: Dict[str, Any],
    csv_path: str = MAPPINGS_CSV_FILE,
) -> str:
    """
    Flatten field mappings into ``all_index_mappings.csv``.

    Columns:
      project_prefix, field_name, resolved_index, data_type, is_ecs_standard
    """
    fieldnames = [
        "project_prefix",
        "field_name",
        "resolved_index",
        "data_type",
        "is_ecs_standard",
    ]

    rows: List[Dict[str, str]] = []
    for entry in report.get("results") or []:
        prefix = entry.get("prefix") or ""
        index_name = entry_index_name(entry) or "MISSING"
        fields = entry_fields(entry)

        if not fields:
            rows.append(
                {
                    "project_prefix": prefix,
                    "field_name": "N/A",
                    "resolved_index": index_name,
                    "data_type": "N/A",
                    "is_ecs_standard": _bool_csv(False),
                }
            )
            continue

        for field_name in sorted(fields):
            rows.append(
                {
                    "project_prefix": prefix,
                    "field_name": field_name,
                    "resolved_index": index_name,
                    "data_type": fields[field_name],
                    "is_ecs_standard": _bool_csv(is_ecs_standard_field(field_name)),
                }
            )

    Path(csv_path).parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    abs_path = os.path.abspath(csv_path)
    logger.info("Wrote field mappings CSV (%d rows): %s", len(rows), abs_path)
    return abs_path


def export_central_format_readiness_csv(
    report: Dict[str, Any],
    csv_path: str = READINESS_CSV_FILE,
) -> str:
    """
    Per-prefix gap analysis for central log format standardization.

    Readiness is based on mapping validity and ECS compliance.
    """
    fieldnames = [
        "project_prefix",
        "core_team",
        "total_fields",
        "ecs_compliant_fields_count",
        "missing_ecs_fields_count",
        "can_centralize_as_is",
        "target_log_format",
        "docs_count",
        "index_size",
        "avg_log_size",
    ]

    rows: List[Dict[str, Any]] = []
    for entry in report.get("results") or []:
        prefix = entry.get("prefix") or ""
        fields = entry_fields(entry)
        ecs_info = normalize_ecs(entry.get("ecs"))
        issues = entry.get("issues") or field_issues(fields, ecs_info=ecs_info)
        missing_ecs = issues.get("missing_ecs_fields") or []
        ecs_compliant = sum(1 for f in fields if is_ecs_standard_field(f))

        target_log_format = classify_log_format_archetype(
            fields, ecs_info=ecs_info or None
        )

        present = bool(entry_index_name(entry))
        ecs_ok = bool(ecs_info and ecs_info.get("ecs_ready"))
        can_centralize = present and bool(fields) and ecs_ok

        size = entry_index_size(entry)
        rows.append(
            {
                "project_prefix": prefix,
                "core_team": core_team_of(prefix),
                "total_fields": len(fields),
                "ecs_compliant_fields_count": ecs_compliant,
                "missing_ecs_fields_count": len(missing_ecs),
                "can_centralize_as_is": _bool_csv(can_centralize),
                "target_log_format": target_log_format,
                "docs_count": size.get("docs_count", 0),
                "index_size": size.get("store_size") or "0b",
                "avg_log_size": size.get("avg_log_size") or "",
            }
        )

    Path(csv_path).parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    abs_path = os.path.abspath(csv_path)
    logger.info("Wrote central format readiness CSV (%d rows): %s", len(rows), abs_path)
    return abs_path


def export_index_field_counts_csv(
    report: Dict[str, Any],
    csv_path: str = FIELD_COUNTS_CSV_FILE,
) -> str:
    """Per-service summary: index size and field counts (human-readable sizes)."""
    fieldnames = [
        "service",
        "team",
        "log_format",
        "index_name",
        "field_count",
        "docs_count",
        "index_size",
        "avg_log_size",
    ]

    rows: List[Dict[str, Any]] = []

    for entry in report.get("results") or []:
        prefix = entry.get("prefix") or ""
        fields = entry_fields(entry)
        index_name = entry_index_name(entry) or ""
        size = entry_index_size(entry)
        ecs_info = normalize_ecs(entry.get("ecs"))

        target_log_format = classify_log_format_archetype(
            fields, ecs_info=ecs_info or None
        )

        rows.append(
            {
                "service": prefix,
                "team": core_team_of(prefix),
                "log_format": target_log_format,
                "index_name": index_name or "",
                "field_count": len(fields) if index_name else "",
                "docs_count": size.get("docs_count", 0) if index_name else "",
                "index_size": (size.get("store_size") or "0b") if index_name else "",
                "avg_log_size": (size.get("avg_log_size") or "") if index_name else "",
                "_sort_bytes": int(size.get("store_size_bytes") or 0) if index_name else 0,
            }
        )

    rows.sort(key=lambda r: int(r.get("_sort_bytes") or 0), reverse=True)
    for row in rows:
        row.pop("_sort_bytes", None)

    Path(csv_path).parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    abs_path = os.path.abspath(csv_path)
    logger.info("Wrote index field-counts CSV (%d rows): %s", len(rows), abs_path)
    return abs_path


def _build_prefix_yaml_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Human-readable per-service YAML from live ES mappings."""
    fields = entry_fields(entry)
    ecs_info = normalize_ecs(entry.get("ecs"))
    size = entry_index_size(entry)
    issues = entry.get("issues") or field_issues(fields, ecs_info=ecs_info or None)

    index_name = entry_index_name(entry)
    if not index_name or index_name in ("MISSING", "DISABLED"):
        body: Dict[str, Any] = {
            "log_format": classify_log_format_archetype(fields, ecs_info=ecs_info or None),
            "index": "MISSING",
            "present": False,
        }
    else:
        body = {
            "log_format": classify_log_format_archetype(fields, ecs_info=ecs_info or None),
            "index": index_name,
            "present": True,
            "docs": size.get("docs_count", 0),
            "index_size": size.get("store_size") or "0b",
            "avg_log_size": size.get("avg_log_size"),
            "field_count": len(fields),
            "ecs_ready": bool(ecs_info and ecs_info.get("ecs_ready")),
            "ecs_score": (
                f"{(ecs_info or {}).get('ecs_fields_present', 0)}/"
                f"{(ecs_info or {}).get('ecs_fields_total', 5)}"
            ),
            "fields": {k: fields[k] for k in sorted(fields)},
        }

    missing_ecs = issues.get("missing_ecs_fields") or []
    legacy = issues.get("legacy_ecs_alternatives") or []
    if missing_ecs or legacy:
        body["ecs_gaps"] = {
            "missing_ecs_fields": missing_ecs,
            "legacy_ecs_alternatives": legacy,
        }

    return body


def export_mappings_to_yaml(
    report: Dict[str, Any],
    yaml_path: str = MAPPINGS_YAML_FILE,
    per_index_dir: str = MAPPINGS_YAML_DIR,
) -> Tuple[str, str]:
    """
    Export field mappings as YAML for easier reading.

    Writes:
      - ``results/all_index_mappings.yaml`` — all prefixes in one file
      - ``results/index_mappings/<prefix>.yaml`` — one file per prefix
    """
    if yaml is None:
        raise SystemExit(
            "PyYAML is required for YAML export. Install with: pip install pyyaml"
        )

    combined: Dict[str, Any] = {}

    for entry in report.get("results") or []:
        prefix = entry.get("prefix") or "unknown"
        combined[prefix] = _build_prefix_yaml_entry(entry)

    Path(yaml_path).parent.mkdir(parents=True, exist_ok=True)
    with open(yaml_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(
            combined,
            fh,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
            width=120,
        )
    abs_combined = os.path.abspath(yaml_path)

    os.makedirs(per_index_dir, exist_ok=True)
    for prefix, body in combined.items():
        safe_name = prefix.replace("/", "_").replace(os.sep, "_")
        single_path = os.path.join(per_index_dir, f"{safe_name}.yaml")
        with open(single_path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(
                {prefix: body},
                fh,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
                width=120,
            )

    abs_dir = os.path.abspath(per_index_dir)
    logger.info(
        "Wrote YAML mappings: %s (%d prefixes) and %s/",
        abs_combined,
        len(combined),
        abs_dir,
    )
    return abs_combined, abs_dir


# ---------------------------------------------------------------------------
# CLI output
# ---------------------------------------------------------------------------

def _hr(char: str = "=", width: int = 78) -> str:
    return char * width


def print_cli_summary(report: Dict[str, Any]) -> None:
    print(_hr())
    print(" Elasticsearch Mapping Analysis")
    print(f" Generated: {report['generated_at']}")
    print(_hr())
    print(
        f" Prefixes scanned : {report['prefixes_total']}\n"
        f" Schema issues    : {report['prefixes_with_schema_drift']}\n"
        f" Not ECS-ready    : {report['prefixes_not_ecs_ready']}"
    )
    print(_hr("-"))

    for entry in report["results"]:
        prefix = entry["prefix"]
        status = entry["status"]
        drift = entry.get("has_schema_drift")
        ecs_info = normalize_ecs(entry.get("ecs"))

        if status == "ok" and not drift and ecs_info.get("ecs_ready"):
            continue

        flags = []
        if drift:
            flags.append("SCHEMA ISSUES")
        if status not in {"ok"}:
            flags.append(status.upper())
        if ecs_info and not ecs_info.get("ecs_ready"):
            flags.append("!ECS")

        flag_str = " | ".join(flags) if flags else "REVIEW"
        print(f"\n[{flag_str}]  {prefix}")
        print(f"  Index : {entry_index_name(entry) or '(none)'}")

        if entry.get("error"):
            print(f"  Error : {entry['error']}")
            continue

        issues = entry.get("issues") or {}
        missing_ecs = issues.get("missing_ecs_fields") or []
        if missing_ecs:
            print(
                f"  Missing required ECS ({len(missing_ecs)}): "
                + ", ".join(missing_ecs)
            )

        if ecs_info:
            ready = "yes" if ecs_info.get("ecs_ready") else "NO"
            print(
                f"  ECS: ready={ready} "
                f"({ecs_info.get('ecs_fields_present')}/{ecs_info.get('ecs_fields_total')})"
            )
            for field, detail in (ecs_info.get("fields") or {}).items():
                st = detail.get("status")
                if st == "ecs":
                    continue
                legacy = detail.get("legacy_alternatives_found") or []
                legacy_str = f" (legacy: {', '.join(legacy)})" if legacy else ""
                print(f"    · {field}: {st}{legacy_str}")

    clean = [
        r["prefix"]
        for r in report["results"]
        if r["status"] == "ok"
        and not r.get("has_schema_drift")
        and normalize_ecs(r.get("ecs")).get("ecs_ready")
    ]
    print()
    print(_hr("-"))
    if clean:
        print(f" Clean (present, ECS-ready): {len(clean)}")
        print("  " + ", ".join(clean))
    else:
        print(" Clean (present, ECS-ready): 0")
    print(_hr())


def save_report(report: Dict[str, Any], path: str = OUTPUT_FILE) -> str:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=False)
    abs_path = os.path.abspath(path)
    print(f" Full report written to: {abs_path}")
    return abs_path


def main(
    config: Optional[Dict[str, Any]] = None,
    prefixes: Optional[List[str]] = None,
    output: str = OUTPUT_FILE,
    mappings_csv: str = MAPPINGS_CSV_FILE,
    readiness_csv: str = READINESS_CSV_FILE,
    field_counts_csv: str = FIELD_COUNTS_CSV_FILE,
    mappings_yaml: str = MAPPINGS_YAML_FILE,
    mappings_yaml_dir: str = MAPPINGS_YAML_DIR,
    index_date: Optional[str] = None,
) -> int:
    """
    Entry point.

    ``index_date`` / env ``INDEX_DATE`` pins resolution to that calendar day's
    indices (``YYYY-MM-DD``). Results are written under ``results/<YYYY-MM-DD>/``.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("elasticsearch").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    raw_index_date = (index_date or os.environ.get("INDEX_DATE", "")).strip()
    as_of: Optional[date] = parse_index_date(raw_index_date) if raw_index_date else None
    day_label = index_date_label(as_of or _utc_now().date())

    global CORE_INDEX_PREFIXES
    if prefixes is None:
        CORE_INDEX_PREFIXES = load_service_prefixes()
        prefixes = CORE_INDEX_PREFIXES
        logger.info(
            "Loaded %d service prefixes from %s",
            len(prefixes),
            PREFIXES_FILE,
        )

    before = len(prefixes)
    prefixes = filter_prefixes(prefixes)
    if len(prefixes) != before:
        logger.info(
            "PREFIX_FILTER=%r → %d/%d prefixes",
            os.getenv("PREFIX_FILTER", ""),
            len(prefixes),
            before,
        )
    if not prefixes:
        logger.error(
            "No prefixes left after PREFIX_FILTER=%r — nothing to analyze",
            os.getenv("PREFIX_FILTER", ""),
        )
        return 1

    global RESULTS_ROOT, RESULTS_DIR, OUTPUT_FILE, MAPPINGS_CSV_FILE, READINESS_CSV_FILE
    global FIELD_COUNTS_CSV_FILE, MAPPINGS_YAML_FILE, MAPPINGS_YAML_DIR
    results_root = resolve_results_dir(RESULTS_ROOT)
    preferred_root = Path(
        os.environ.get("RESULTS_DIR") or (Path(__file__).resolve().parent / "results")
    )
    if results_root.resolve() != preferred_root.resolve():
        logger.warning(
            "Cannot write to results/ — using %s "
            "(fix with: docker compose run --rm --entrypoint /docker-entrypoint.sh "
            "es-mapping-compare fix-perms)",
            results_root,
        )

    migrate_legacy_flat_results(results_root)

    config = config or {}
    shared_verify = config.get("verify_certs")

    es_cfg = dict(config.get("elasticsearch") or config.get("es") or config.get("stage") or {})
    if shared_verify is not None:
        es_cfg.setdefault("verify_certs", shared_verify)

    es = build_es_client(
        "ES_URL", "ES_USER", "ES_PASSWORD", "Elasticsearch", es_cfg
    )

    try:
        if not es.ping():
            print(
                "WARNING: Elasticsearch cluster did not respond to ping()",
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001
        print(
            f"WARNING: Elasticsearch ping failed: {_explain_transport_error(exc)}",
            file=sys.stderr,
        )

    # Pinned day: require daily indices BEFORE creating results/<day>/.
    if as_of is not None:
        hits = count_indices_for_day(es, prefixes, as_of)
        if hits == 0:
            suffix = index_date_suffix(as_of)
            msg = (
                f"No Elasticsearch indices for INDEX_DATE={day_label} "
                f"(looked for *-logs-{suffix} / *-{suffix}). "
                "Results folder was NOT created."
            )
            logger.error(msg)
            print(f"ERROR: {msg}", file=sys.stderr)
            stale = results_root / day_label
            if stale.is_dir():
                shutil.rmtree(stale, ignore_errors=True)
                print(f"Removed empty/invalid results folder: {stale}", file=sys.stderr)
            return 1
        logger.info(
            "INDEX_DATE=%s — found %d index(es) for %s",
            day_label,
            hits,
            index_date_suffix(as_of),
        )

    if as_of is not None and not os.environ.get("RESULTS_RUN", "").strip():
        run_dir = make_run_dir(results_root, run_id=day_label)
    else:
        run_dir = make_run_dir(results_root, index_day=as_of)
    point_latest_symlink(results_root, run_dir)

    RESULTS_ROOT = results_root
    RESULTS_DIR = run_dir
    output = str(RESULTS_DIR / "mapping_comparison.json")
    mappings_csv = str(RESULTS_DIR / "all_index_mappings.csv")
    readiness_csv = str(RESULTS_DIR / "central_format_readiness.csv")
    field_counts_csv = str(RESULTS_DIR / "index_field_counts.csv")
    mappings_yaml = str(RESULTS_DIR / "all_index_mappings.yaml")
    mappings_yaml_dir = str(RESULTS_DIR / "index_mappings")
    OUTPUT_FILE = output
    MAPPINGS_CSV_FILE = mappings_csv
    READINESS_CSV_FILE = readiness_csv
    FIELD_COUNTS_CSV_FILE = field_counts_csv
    MAPPINGS_YAML_FILE = mappings_yaml
    MAPPINGS_YAML_DIR = mappings_yaml_dir
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    if as_of is not None:
        logger.info("Writing INDEX_DATE=%s results to %s", day_label, RESULTS_DIR)
    else:
        logger.info(
            "Writing this run to %s (prefer today, fallback newest)",
            RESULTS_DIR,
        )

    report = analyze_cluster(es, prefixes=prefixes, as_of=as_of)
    json_path = save_report(report, path=output)
    mappings_path = export_mappings_to_csv(report, csv_path=mappings_csv)
    readiness_path = export_central_format_readiness_csv(
        report, csv_path=readiness_csv
    )
    field_counts_path = export_index_field_counts_csv(
        report, csv_path=field_counts_csv
    )
    yaml_path, yaml_dir = export_mappings_to_yaml(
        report, yaml_path=mappings_yaml, per_index_dir=mappings_yaml_dir
    )
    print_cli_summary(report)

    print(_hr("-"))
    print(" Exported files:")
    print(f"  Index day : {report.get('index_date', day_label)}")
    print(f"  JSON : {json_path}")
    print(f"  CSV  : {mappings_path}")
    print(f"  CSV  : {readiness_path}")
    print(f"  CSV  : {field_counts_path}")
    print(f"  YAML : {yaml_path}")
    print(f"  YAML : {yaml_dir}/<prefix>.yaml")
    print(_hr())

    if report["prefixes_with_schema_drift"]:
        return 2
    return 0


if __name__ == "__main__":
    # Optional inline config dictionary (overrides / supplements env vars).
    # Leave empty to rely solely on environment variables.
    CONFIG: Dict[str, Any] = {
        # "elasticsearch": {"url": "https://es:9200", "user": "elastic", "password": "..."},
        # "verify_certs": False,
    }
    sys.exit(main(config=CONFIG))
