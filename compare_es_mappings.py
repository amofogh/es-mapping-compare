#!/usr/bin/env python3
"""
Compare Elasticsearch index field mappings between Beta and Stage clusters.

Resolves the latest date-based index per project prefix (prefers today's
``<prefix>-logs-YYYY.MM.DD``, then ``<prefix>-YYYY.MM.DD``, then wildcard
fallback), flattens mappings, detects schema drift, checks ECS compliance,
tags Fluentd log-format archetypes, and exports CSV reports for central log
format standardization.

Usage:
  # 1) Discover / merge live prefixes into prefixes.json
  python discover_prefixes.py

  # 2) Review/edit prefixes.json, then compare mappings
  ENABLE_BETA=false python compare_es_mappings.py

  # Stage-only (default): ENABLE_BETA=false
  # Both clusters:       ENABLE_BETA=true

Outputs (under ``results/<run_id>/``):
  - mapping_comparison.json
  - all_index_mappings.csv
  - central_format_readiness.csv
  - index_field_counts.csv
  - all_index_mappings.yaml
  - index_mappings/<prefix>.yaml

Each compare run writes a unique dated folder (``YYYY-MM-DD_HHMMSS``) so
previous runs are kept for side-by-side comparison. ``results/latest`` is a
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


def load_beta_prefix_aliases(path: Optional[Path] = None) -> Dict[str, str]:
    """
    Load Stage→Beta service prefix aliases from prefixes.json.

    Beta often uses ``<team>-beta-<app>`` (e.g. ``mic-beta-ava``) while Stage
    uses ``<team>-<app>`` (``mic-ava``), and may consolidate many Stage
    services into one Beta stream (all ``mic-iss.*`` → ``mic-beta-iss``).
    """
    prefixes_path = path or PREFIXES_FILE
    if not prefixes_path.is_file():
        return {}
    try:
        with open(prefixes_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    raw = data.get("beta_prefix_aliases") or {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(k).strip(): str(v).strip()
        for k, v in raw.items()
        if str(k).strip() and str(v).strip()
    }


def beta_prefix_candidates(
    stage_prefix: str,
    aliases: Optional[Dict[str, str]] = None,
) -> List[str]:
    """
    Ordered Beta lookup prefixes for a Stage service prefix.

    1. Explicit alias from ``beta_prefix_aliases``
    2. Auto ``team-beta-rest`` (mic-ava → mic-beta-ava)
    3. Collapsed ``team-beta-head`` (mic-iss.webapi → mic-beta-iss)
    4. Exact Stage name (in case naming matches)
    """
    aliases = aliases if aliases is not None else load_beta_prefix_aliases()
    prefix = (stage_prefix or "").strip()
    if not prefix:
        return []

    ordered: List[str] = []

    def _add(name: str) -> None:
        name = name.strip()
        if name and name not in ordered:
            ordered.append(name)

    if prefix in aliases:
        _add(aliases[prefix])

    if "-" in prefix:
        team, rest = prefix.split("-", 1)
        _add(f"{team}-beta-{rest}")
        head = rest.replace(".", "-").split("-", 1)[0]
        if head:
            _add(f"{team}-beta-{head}")

    _add(prefix)
    return ordered


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

# Feature flag: when False, skip Beta entirely and run Stage-only mode.
ENABLE_BETA = os.environ.get("ENABLE_BETA", "false").lower() in ("true", "1", "yes")

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


def build_es_client(
    url_env: str,
    user_env: str,
    password_env: str,
    label: str,
    config: Optional[Dict[str, Any]] = None,
) -> Elasticsearch:
    """
    Build an Elasticsearch 7.x client from env vars and/or a config dict.

    Precedence: config dict overrides environment variables.
    """
    cfg = config or {}
    url = cfg.get("url") or os.environ.get(url_env)
    if not url:
        raise SystemExit(
            f"Missing {label} Elasticsearch URL. "
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

    user = cfg.get("user") or os.environ.get(user_env)
    password = cfg.get("password") or os.environ.get(password_env)
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
            "or another redirecting proxy. Set BETA_ES_URL / STAGE_ES_URL to "
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


def resolve_latest_index_beta(
    es: Elasticsearch,
    stage_prefix: str,
    aliases: Optional[Dict[str, str]] = None,
    as_of: Optional[date] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Resolve Beta index for a Stage service prefix.

    Tries alias / ``team-beta-*`` candidates. Returns
    ``(resolved_index, beta_prefix_used)``.
    """
    for candidate in beta_prefix_candidates(stage_prefix, aliases=aliases):
        resolved = resolve_latest_index(
            es, candidate, cluster_label="Beta", as_of=as_of
        )
        if resolved:
            if candidate != stage_prefix:
                logger.info(
                    "Prefix '%s': Beta alias '%s' -> %s",
                    stage_prefix,
                    candidate,
                    resolved,
                )
            return resolved, candidate
    return None, None


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


def count_stage_indices_for_day(
    stage: Elasticsearch,
    prefixes: List[str],
    as_of: date,
) -> int:
    """How many prefixes have a Stage index for the pinned calendar day."""
    hits = 0
    for prefix in prefixes:
        if resolve_latest_index(stage, prefix, cluster_label="Stage", as_of=as_of):
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
    beta: Optional[Elasticsearch],
    stage: Elasticsearch,
    beta_enabled: bool,
) -> None:
    """Populate ``stage_size`` / ``beta_size`` on a compare result entry."""
    stage_index = entry.get("stage_index")
    entry["stage_size"] = (
        fetch_index_size_stats(stage, stage_index)
        if stage_index
        else empty_index_size_stats()
    )

    if not beta_enabled or beta is None:
        entry["beta_size"] = empty_index_size_stats()
        return

    beta_index = entry.get("beta_index")
    if beta_index and beta_index != "DISABLED":
        entry["beta_size"] = fetch_index_size_stats(beta, beta_index)
    else:
        entry["beta_size"] = empty_index_size_stats()


# ---------------------------------------------------------------------------
# Comparison & ECS checks
# ---------------------------------------------------------------------------

def compare_fields(
    beta_fields: Dict[str, str],
    stage_fields: Dict[str, str],
) -> Dict[str, Any]:
    beta_keys = set(beta_fields)
    stage_keys = set(stage_fields)

    missing_in_stage = sorted(beta_keys - stage_keys)
    missing_in_beta = sorted(stage_keys - beta_keys)

    type_mismatches = []
    for key in sorted(beta_keys & stage_keys):
        b_type, s_type = beta_fields[key], stage_fields[key]
        if b_type != s_type:
            type_mismatches.append(
                {
                    "field": key,
                    "beta_type": b_type,
                    "stage_type": s_type,
                }
            )

    return {
        "missing_in_stage": missing_in_stage,
        "missing_in_beta": missing_in_beta,
        "type_mismatches": type_mismatches,
        "beta_field_count": len(beta_fields),
        "stage_field_count": len(stage_fields),
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

def compare_clusters(
    beta: Optional[Elasticsearch],
    stage: Elasticsearch,
    prefixes: Optional[List[str]] = None,
    enable_beta: Optional[bool] = None,
    as_of: Optional[date] = None,
) -> Dict[str, Any]:
    prefixes = prefixes or CORE_INDEX_PREFIXES
    beta_enabled = ENABLE_BETA if enable_beta is None else enable_beta
    results: List[Dict[str, Any]] = []
    beta_aliases = load_beta_prefix_aliases() if beta_enabled else {}
    index_day = index_date_label(as_of) if as_of is not None else None

    for prefix in prefixes:
        entry: Dict[str, Any] = {
            "prefix": prefix,
            "beta_index": "DISABLED" if not beta_enabled else None,
            "beta_prefix": None,
            "stage_index": None,
            "status": "ok",
            "error": None,
            "comparison": None,
            "ecs": {"beta": None, "stage": None},
            "has_schema_drift": False,
            "has_type_mismatch": False,
            "beta_fields": {},
            "stage_fields": {},
            "beta_disabled": not beta_enabled,
            "stage_size": empty_index_size_stats(),
            "beta_size": empty_index_size_stats(),
        }

        try:
            beta_prefix_used: Optional[str] = None
            if beta_enabled:
                if beta is None:
                    raise RuntimeError("ENABLE_BETA is True but Beta client is None")
                beta_index, beta_prefix_used = resolve_latest_index_beta(
                    beta, prefix, aliases=beta_aliases, as_of=as_of
                )
            else:
                beta_index = "DISABLED"

            stage_index = resolve_latest_index(
                stage, prefix, cluster_label="Stage", as_of=as_of
            )
            entry["beta_index"] = beta_index
            entry["beta_prefix"] = beta_prefix_used
            entry["stage_index"] = stage_index

            if beta_enabled and not beta_index:
                tried = ", ".join(beta_prefix_candidates(prefix, beta_aliases))
                logger.warning(
                    "Prefix '%s': MISSING on Beta (tried: %s) — CSV will use MISSING/N/A for Beta",
                    prefix,
                    tried,
                )
            if not stage_index:
                logger.warning(
                    "Prefix '%s': MISSING on Stage — CSV will use MISSING/N/A for Stage",
                    prefix,
                )

            beta_fields: Dict[str, str] = {}
            stage_fields: Dict[str, str] = {}

            if beta_enabled and beta_index and beta_index != "DISABLED":
                beta_fields = fetch_flattened_mapping(beta, beta_index)
            if stage_index:
                stage_fields = fetch_flattened_mapping(stage, stage_index)

            entry["beta_fields"] = beta_fields
            entry["stage_fields"] = stage_fields

            if not beta_enabled:
                # Stage-only mode: never treat disabled Beta as drift/failure.
                if not stage_index:
                    entry["status"] = "missing_stage"
                    entry["error"] = f"No index matching '{prefix}-*' on Stage"
                    entry["has_schema_drift"] = True
                    entry["comparison"] = compare_fields({}, stage_fields)
                    entry["ecs"] = {
                        "beta": None,
                        "stage": check_ecs_compliance(stage_fields) if stage_fields else None,
                    }
                    attach_index_size_stats(entry, beta, stage, beta_enabled)
                    results.append(entry)
                    continue

                entry["comparison"] = {
                    "missing_in_stage": [],
                    "missing_in_beta": [],
                    "type_mismatches": [],
                    "beta_field_count": 0,
                    "stage_field_count": len(stage_fields),
                    "beta_disabled": True,
                }
                entry["ecs"] = {
                    "beta": None,
                    "stage": check_ecs_compliance(stage_fields) if stage_fields else None,
                }
                entry["has_type_mismatch"] = False
                entry["has_schema_drift"] = False
                attach_index_size_stats(entry, beta, stage, beta_enabled)
                results.append(entry)
                continue

            if not beta_index and not stage_index:
                entry["status"] = "missing_both"
                entry["error"] = f"No index matching '{prefix}-*' on Beta or Stage"
                entry["has_schema_drift"] = True
                attach_index_size_stats(entry, beta, stage, beta_enabled)
                results.append(entry)
                continue

            if not beta_index:
                tried = ", ".join(beta_prefix_candidates(prefix, beta_aliases))
                entry["status"] = "missing_beta"
                entry["error"] = (
                    f"No Beta index for '{prefix}' (tried: {tried})"
                )
                entry["has_schema_drift"] = True
            elif not stage_index:
                entry["status"] = "missing_stage"
                entry["error"] = f"No index matching '{prefix}-*' on Stage"
                entry["has_schema_drift"] = True

            comparison = compare_fields(beta_fields, stage_fields)
            entry["comparison"] = comparison
            entry["ecs"] = {
                "beta": check_ecs_compliance(beta_fields) if beta_fields else None,
                "stage": check_ecs_compliance(stage_fields) if stage_fields else None,
            }
            entry["has_type_mismatch"] = bool(comparison["type_mismatches"])
            if entry["status"] == "ok":
                entry["has_schema_drift"] = bool(
                    comparison["missing_in_stage"]
                    or comparison["missing_in_beta"]
                    or comparison["type_mismatches"]
                )
            else:
                entry["has_schema_drift"] = True
        except Exception as exc:  # noqa: BLE001 — surface per-prefix failures
            entry["status"] = "error"
            entry["error"] = _explain_transport_error(exc)
            entry["has_schema_drift"] = True
            logger.exception("Prefix '%s' failed: %s", prefix, exc)

        attach_index_size_stats(entry, beta, stage, beta_enabled)
        results.append(entry)

    drifted = [r for r in results if r.get("has_schema_drift")]
    mismatched = [r for r in results if r.get("has_type_mismatch")]
    if beta_enabled:
        ecs_not_ready = [
            r
            for r in results
            if r.get("ecs", {}).get("beta")
            and (
                not r["ecs"]["beta"].get("ecs_ready")
                or not (r["ecs"].get("stage") or {}).get("ecs_ready", False)
            )
        ]
    else:
        ecs_not_ready = [
            r
            for r in results
            if r.get("stage_index")
            and not ((r.get("ecs") or {}).get("stage") or {}).get("ecs_ready", False)
        ]

    return {
        "generated_at": _utc_now_iso(),
        "index_date": index_day or index_date_label(as_of or _utc_now().date()),
        "index_date_pinned": as_of is not None,
        "enable_beta": beta_enabled,
        "mode": "beta_and_stage" if beta_enabled else "stage_only",
        "prefixes_total": len(prefixes),
        "prefixes_with_schema_drift": len(drifted),
        "prefixes_with_type_mismatches": len(mismatched),
        "prefixes_not_ecs_ready": len(ecs_not_ready),
        "results": results,
    }


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def _bool_csv(value: bool) -> str:
    return "TRUE" if value else "FALSE"


def _primary_size_stats(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Prefer Stage size stats; fall back to Beta."""
    stage_size = entry.get("stage_size") or empty_index_size_stats()
    if entry.get("stage_index") not in (None, "MISSING"):
        return stage_size
    beta_size = entry.get("beta_size") or empty_index_size_stats()
    if entry.get("beta_index") not in (None, "MISSING", "DISABLED"):
        return beta_size
    return stage_size


def export_mappings_to_csv(
    report: Dict[str, Any],
    csv_path: str = MAPPINGS_CSV_FILE,
) -> str:
    """
    Flatten field mappings into ``all_index_mappings.csv``.

    Required columns:
      project_prefix, field_name, stage_resolved_index, stage_data_type, is_ecs_standard

    When Beta is enabled, also includes beta_* / mismatch helper columns.
    """
    beta_enabled = bool(report.get("enable_beta", ENABLE_BETA))
    fieldnames = [
        "project_prefix",
        "field_name",
        "stage_resolved_index",
        "stage_data_type",
        "is_ecs_standard",
    ]
    if beta_enabled:
        fieldnames[2:2] = ["beta_resolved_index", "beta_data_type"]
        fieldnames.extend(["is_type_mismatch", "exists_in_both_envs"])

    rows: List[Dict[str, str]] = []
    for entry in report.get("results") or []:
        prefix = entry.get("prefix") or ""
        beta_disabled = (not beta_enabled) or entry.get("beta_disabled") or (
            entry.get("beta_index") == "DISABLED"
        )
        beta_index = "DISABLED" if beta_disabled else (entry.get("beta_index") or "MISSING")
        stage_index = entry.get("stage_index") or "MISSING"
        beta_fields: Dict[str, str] = {} if beta_disabled else (entry.get("beta_fields") or {})
        stage_fields: Dict[str, str] = entry.get("stage_fields") or {}

        all_fields = sorted(set(beta_fields) | set(stage_fields))

        def _row(field_name: str, stage_type: Optional[str], beta_type: Optional[str]) -> Dict[str, str]:
            row = {
                "project_prefix": prefix,
                "field_name": field_name,
                "stage_resolved_index": stage_index,
                "stage_data_type": stage_type if stage_type is not None else "N/A",
                "is_ecs_standard": _bool_csv(
                    False if field_name == "N/A" else is_ecs_standard_field(field_name)
                ),
            }
            if beta_enabled:
                exists_both = beta_type is not None and stage_type is not None
                row["beta_resolved_index"] = beta_index
                row["beta_data_type"] = (
                    "N/A" if beta_disabled or beta_type is None else beta_type
                )
                row["is_type_mismatch"] = _bool_csv(
                    bool(exists_both and beta_type != stage_type)
                )
                row["exists_in_both_envs"] = _bool_csv(bool(exists_both))
            return row

        if not all_fields:
            rows.append(_row("N/A", None, None))
            continue

        for field_name in all_fields:
            rows.append(
                _row(
                    field_name,
                    stage_fields.get(field_name),
                    None if beta_disabled else beta_fields.get(field_name),
                )
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

    When Beta is enabled, ``can_centralize_as_is`` requires both envs, zero type
    mismatches, and core ECS fields on both sides with data.

    When Beta is disabled, readiness is based purely on Stage mapping validity
    and Stage ECS compliance.
    """
    fieldnames = [
        "project_prefix",
        "core_team",
        "total_fields_beta",
        "total_fields_stage",
        "common_fields_count",
        "type_mismatches_count",
        "ecs_compliant_fields_count",
        "can_centralize_as_is",
        "target_log_format",
        "stage_docs",
        "stage_index_size",
        "stage_avg_log_size",
        "beta_docs",
        "beta_index_size",
        "beta_avg_log_size",
    ]

    beta_enabled = bool(report.get("enable_beta", ENABLE_BETA))
    rows: List[Dict[str, Any]] = []
    for entry in report.get("results") or []:
        prefix = entry.get("prefix") or ""
        beta_disabled = (not beta_enabled) or entry.get("beta_disabled") or (
            entry.get("beta_index") == "DISABLED"
        )
        beta_fields: Dict[str, str] = {} if beta_disabled else (entry.get("beta_fields") or {})
        stage_fields: Dict[str, str] = entry.get("stage_fields") or {}
        beta_keys = set(beta_fields)
        stage_keys = set(stage_fields)
        common = beta_keys & stage_keys

        comparison = entry.get("comparison") or {}
        mismatches = comparison.get("type_mismatches") or []
        # Recompute if comparison was skipped
        if entry.get("comparison") is None and not beta_disabled:
            mismatches = [
                k for k in common if beta_fields[k] != stage_fields[k]
            ]
        if beta_disabled:
            mismatches = []

        # ECS-compliant = union fields that match ECS standard paths
        union_fields = beta_keys | stage_keys
        ecs_compliant = sum(1 for f in union_fields if is_ecs_standard_field(f))

        beta_ecs = (entry.get("ecs") or {}).get("beta")
        stage_ecs = (entry.get("ecs") or {}).get("stage")

        # Archetype is based on Stage (primary planning source); fall back to Beta.
        archetype_fields = stage_fields or beta_fields
        archetype_ecs = stage_ecs or beta_ecs
        target_log_format = classify_log_format_archetype(
            archetype_fields, ecs_info=archetype_ecs
        )

        if beta_disabled:
            stage_present = bool(entry.get("stage_index"))
            stage_ecs_ok = bool(stage_ecs and stage_ecs.get("ecs_ready"))
            can_centralize = stage_present and bool(stage_fields) and stage_ecs_ok
        else:
            core_ecs_ok = True
            if beta_fields:
                core_ecs_ok = core_ecs_ok and bool(beta_ecs and beta_ecs.get("ecs_ready"))
            if stage_fields:
                core_ecs_ok = core_ecs_ok and bool(stage_ecs and stage_ecs.get("ecs_ready"))
            if not beta_fields and not stage_fields:
                core_ecs_ok = False

            both_envs_present = bool(
                entry.get("beta_index")
                and entry.get("beta_index") != "DISABLED"
                and entry.get("stage_index")
            )
            can_centralize = (
                both_envs_present
                and len(mismatches) == 0
                and core_ecs_ok
            )

        stage_size = entry.get("stage_size") or empty_index_size_stats()
        beta_size = entry.get("beta_size") or empty_index_size_stats()
        rows.append(
            {
                "project_prefix": prefix,
                "core_team": core_team_of(prefix),
                "total_fields_beta": 0 if beta_disabled else len(beta_fields),
                "total_fields_stage": len(stage_fields),
                "common_fields_count": 0 if beta_disabled else len(common),
                "type_mismatches_count": 0 if beta_disabled else len(mismatches),
                "ecs_compliant_fields_count": ecs_compliant,
                "can_centralize_as_is": _bool_csv(can_centralize),
                "target_log_format": target_log_format,
                "stage_docs": stage_size.get("docs_count", 0),
                "stage_index_size": stage_size.get("store_size") or "0b",
                "stage_avg_log_size": stage_size.get("avg_log_size") or "",
                "beta_docs": ""
                if beta_disabled
                else beta_size.get("docs_count", 0),
                "beta_index_size": ""
                if beta_disabled
                else (beta_size.get("store_size") or "0b"),
                "beta_avg_log_size": ""
                if beta_disabled
                else (beta_size.get("avg_log_size") or ""),
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
    """
    Per-service summary: Stage vs Beta index size and field counts.

    Human-readable sizes only (no duplicate primary/stage columns, no raw bytes).
    """
    fieldnames = [
        "service",
        "team",
        "log_format",
        "stage_index",
        "stage_fields",
        "stage_docs",
        "stage_index_size",
        "stage_avg_log_size",
        "beta_index",
        "beta_prefix",
        "beta_fields",
        "beta_docs",
        "beta_index_size",
        "beta_avg_log_size",
        "beta_status",
    ]

    beta_enabled = bool(report.get("enable_beta", ENABLE_BETA))
    rows: List[Dict[str, Any]] = []

    for entry in report.get("results") or []:
        prefix = entry.get("prefix") or ""
        beta_disabled = (not beta_enabled) or entry.get("beta_disabled") or (
            entry.get("beta_index") == "DISABLED"
        )
        beta_fields: Dict[str, str] = {} if beta_disabled else (entry.get("beta_fields") or {})
        stage_fields: Dict[str, str] = entry.get("stage_fields") or {}

        stage_index = entry.get("stage_index") or ""
        beta_index = entry.get("beta_index") or ""
        stage_size = entry.get("stage_size") or empty_index_size_stats()
        beta_size = (
            empty_index_size_stats()
            if beta_disabled
            else (entry.get("beta_size") or empty_index_size_stats())
        )

        if beta_disabled:
            beta_status = "disabled"
            beta_index_out = "DISABLED"
            beta_prefix_out = ""
            beta_fields_n = ""
            beta_docs = ""
            beta_index_size = ""
            beta_avg = ""
        elif not beta_index or beta_index == "MISSING":
            beta_status = "missing"
            beta_index_out = ""
            beta_prefix_out = ""
            beta_fields_n = ""
            beta_docs = ""
            beta_index_size = ""
            beta_avg = ""
        else:
            beta_status = "ok"
            beta_index_out = beta_index
            beta_prefix_out = entry.get("beta_prefix") or ""
            beta_fields_n = len(beta_fields)
            beta_docs = beta_size.get("docs_count", 0)
            beta_index_size = beta_size.get("store_size") or "0b"
            beta_avg = beta_size.get("avg_log_size") or ""

        stage_ecs = (entry.get("ecs") or {}).get("stage")
        beta_ecs = (entry.get("ecs") or {}).get("beta")
        target_log_format = classify_log_format_archetype(
            stage_fields or beta_fields, ecs_info=stage_ecs or beta_ecs
        )

        rows.append(
            {
                "service": prefix,
                "team": core_team_of(prefix),
                "log_format": target_log_format,
                "stage_index": stage_index or "",
                "stage_fields": len(stage_fields) if stage_index else "",
                "stage_docs": stage_size.get("docs_count", 0) if stage_index else "",
                "stage_index_size": (stage_size.get("store_size") or "0b")
                if stage_index
                else "",
                "stage_avg_log_size": (stage_size.get("avg_log_size") or "")
                if stage_index
                else "",
                "beta_index": beta_index_out,
                "beta_prefix": beta_prefix_out,
                "beta_fields": beta_fields_n,
                "beta_docs": beta_docs,
                "beta_index_size": beta_index_size,
                "beta_avg_log_size": beta_avg,
                "beta_status": beta_status,
                "_sort_bytes": int(stage_size.get("store_size_bytes") or 0)
                if stage_index
                else 0,
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


def _build_prefix_yaml_entry(entry: Dict[str, Any], beta_enabled: bool) -> Dict[str, Any]:
    """
    Human-readable per-service YAML from live ES mappings.

    ``fields`` under each env are exactly the flattened Elasticsearch mapping
    types (same as ``GET /<index>/_mapping``). Diffs live under ``comparison``
    instead of duplicating every field twice.
    """
    beta_disabled = (not beta_enabled) or entry.get("beta_disabled") or (
        entry.get("beta_index") == "DISABLED"
    )
    beta_fields: Dict[str, str] = {} if beta_disabled else (entry.get("beta_fields") or {})
    stage_fields: Dict[str, str] = entry.get("stage_fields") or {}
    stage_ecs = (entry.get("ecs") or {}).get("stage")
    beta_ecs = (entry.get("ecs") or {}).get("beta")
    stage_size = entry.get("stage_size") or empty_index_size_stats()
    beta_size = entry.get("beta_size") or empty_index_size_stats()

    def _env_block(
        *,
        index: Optional[str],
        fields: Dict[str, str],
        size: Dict[str, Any],
        ecs_info: Optional[Dict[str, Any]],
        prefix: Optional[str] = None,
        missing_label: str = "MISSING",
    ) -> Dict[str, Any]:
        if not index or index in ("MISSING", "DISABLED"):
            return {"index": missing_label, "present": False}
        block: Dict[str, Any] = {
            "index": index,
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
            # Exact ES mapping types for this index.
            "fields": {k: fields[k] for k in sorted(fields)},
        }
        if prefix:
            block["prefix"] = prefix
        return block

    archetype_fields = stage_fields or beta_fields
    archetype_ecs = stage_ecs or beta_ecs
    body: Dict[str, Any] = {
        "log_format": classify_log_format_archetype(
            archetype_fields, ecs_info=archetype_ecs
        ),
        "stage": _env_block(
            index=entry.get("stage_index"),
            fields=stage_fields,
            size=stage_size,
            ecs_info=stage_ecs,
        ),
    }

    if beta_disabled:
        body["beta"] = {"index": "DISABLED", "present": False}
    else:
        body["beta"] = _env_block(
            index=entry.get("beta_index"),
            fields=beta_fields,
            size=beta_size,
            ecs_info=beta_ecs,
            prefix=entry.get("beta_prefix") or None,
            missing_label="MISSING",
        )

    # Compact diff only (no full fields_by_env duplication).
    if stage_fields and beta_fields:
        stage_keys = set(stage_fields)
        beta_keys = set(beta_fields)
        only_stage = sorted(stage_keys - beta_keys)
        only_beta = sorted(beta_keys - stage_keys)
        type_mismatches = [
            {
                "field": name,
                "stage_type": stage_fields[name],
                "beta_type": beta_fields[name],
            }
            for name in sorted(stage_keys & beta_keys)
            if stage_fields[name] != beta_fields[name]
        ]
        body["comparison"] = {
            "common_fields": len(stage_keys & beta_keys),
            "only_in_stage_count": len(only_stage),
            "only_in_beta_count": len(only_beta),
            "type_mismatch_count": len(type_mismatches),
            "only_in_stage": only_stage,
            "only_in_beta": only_beta,
            "type_mismatches": type_mismatches,
        }
    elif entry.get("status") == "missing_beta":
        body["comparison"] = {
            "note": "No matching Beta index (check beta_prefix_aliases / shipping).",
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
      - ``results/all_index_mappings.yaml`` — all prefixes in one file::

            mic-myagah:
              log_format: Format 3 (...)
              stage:
                index: mic-myagah-2026.08.01
                docs: 41367
                index_size: 24.8mb
                avg_log_size: 628b
                fields:          # exact ES mapping types
                  "@timestamp": date
              beta:
                index: mic-beta-myagah-logs-2026.08.01
                fields: ...
              comparison:        # diffs only (not a full duplicate)
                only_in_stage: [...]
                type_mismatches: [...]

      - ``results/index_mappings/<prefix>.yaml`` — one file per prefix
    """
    if yaml is None:
        raise SystemExit(
            "PyYAML is required for YAML export. Install with: pip install pyyaml"
        )

    beta_enabled = bool(report.get("enable_beta", ENABLE_BETA))
    combined: Dict[str, Any] = {}

    for entry in report.get("results") or []:
        prefix = entry.get("prefix") or "unknown"
        combined[prefix] = _build_prefix_yaml_entry(entry, beta_enabled)

    # Combined file
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

    # Per-index files
    os.makedirs(per_index_dir, exist_ok=True)
    for prefix, body in combined.items():
        # Safe filename: keep dots/dashes, replace path separators only
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
    beta_enabled = bool(report.get("enable_beta", ENABLE_BETA))
    print(_hr())
    if beta_enabled:
        print(" Elasticsearch Mapping Comparison — Beta vs Stage")
    else:
        print(" Elasticsearch Mapping Comparison — Stage-only mode")
        print(" [NOTICE] Beta cluster inspection is DISABLED. Running in Stage-only mode.")
    print(f" Generated: {report['generated_at']}")
    print(_hr())
    print(
        f" Prefixes scanned : {report['prefixes_total']}\n"
        f" Schema drift     : {report['prefixes_with_schema_drift']}\n"
        f" Type mismatches  : {report['prefixes_with_type_mismatches']}\n"
        f" Not ECS-ready    : {report['prefixes_not_ecs_ready']}"
    )
    print(_hr("-"))

    for entry in report["results"]:
        prefix = entry["prefix"]
        status = entry["status"]
        drift = entry.get("has_schema_drift")
        mismatch = entry.get("has_type_mismatch")

        # Skip quiet, clean prefixes in the highlight section header logic
        if status == "ok" and not drift and not mismatch:
            beta_ecs = (entry.get("ecs") or {}).get("beta") or {}
            stage_ecs = (entry.get("ecs") or {}).get("stage") or {}
            if beta_enabled:
                if beta_ecs.get("ecs_ready") and stage_ecs.get("ecs_ready"):
                    continue
            elif stage_ecs.get("ecs_ready"):
                continue

        flags = []
        if mismatch:
            flags.append("TYPE MISMATCH")
        if drift and not mismatch:
            flags.append("SCHEMA DRIFT")
        elif drift and mismatch:
            flags.append("SCHEMA DRIFT")
        if status not in {"ok"}:
            flags.append(status.upper())

        beta_ecs = (entry.get("ecs") or {}).get("beta") or {}
        stage_ecs = (entry.get("ecs") or {}).get("stage") or {}
        if beta_enabled and beta_ecs and not beta_ecs.get("ecs_ready"):
            flags.append("BETA !ECS")
        if stage_ecs and not stage_ecs.get("ecs_ready"):
            flags.append("STAGE !ECS")

        flag_str = " | ".join(flags) if flags else "REVIEW"
        print(f"\n[{flag_str}]  {prefix}")
        print(f"  Beta  index : {entry.get('beta_index') or '(none)'}")
        print(f"  Stage index : {entry.get('stage_index') or '(none)'}")

        if entry.get("error"):
            print(f"  Error       : {entry['error']}")
            continue

        comparison = entry.get("comparison") or {}
        mismatches = comparison.get("type_mismatches") or []
        if mismatches:
            print(f"  Type mismatches ({len(mismatches)}):")
            for item in mismatches[:20]:
                print(
                    f"    - {item['field']}: "
                    f"beta={item['beta_type']}  stage={item['stage_type']}"
                )
            if len(mismatches) > 20:
                print(f"    ... and {len(mismatches) - 20} more")

        if beta_enabled:
            missing_stage = comparison.get("missing_in_stage") or []
            missing_beta = comparison.get("missing_in_beta") or []
            if missing_stage:
                preview = ", ".join(missing_stage[:8])
                more = f" (+{len(missing_stage) - 8} more)" if len(missing_stage) > 8 else ""
                print(f"  Missing in Stage ({len(missing_stage)}): {preview}{more}")
            if missing_beta:
                preview = ", ".join(missing_beta[:8])
                more = f" (+{len(missing_beta) - 8} more)" if len(missing_beta) > 8 else ""
                print(f"  Missing in Beta  ({len(missing_beta)}): {preview}{more}")

        env_checks = [("Stage", stage_ecs)]
        if beta_enabled:
            env_checks.insert(0, ("Beta", beta_ecs))
        for env_label, ecs in env_checks:
            if not ecs:
                continue
            ready = "yes" if ecs.get("ecs_ready") else "NO"
            print(
                f"  ECS ({env_label}): ready={ready} "
                f"({ecs.get('ecs_fields_present')}/{ecs.get('ecs_fields_total')})"
            )
            for field, detail in (ecs.get("fields") or {}).items():
                st = detail.get("status")
                if st == "ecs":
                    continue
                legacy = detail.get("legacy_alternatives_found") or []
                legacy_str = f" (legacy: {', '.join(legacy)})" if legacy else ""
                print(f"    · {field}: {st}{legacy_str}")

    # Clean prefixes one-liner
    if beta_enabled:
        clean = [
            r["prefix"]
            for r in report["results"]
            if r["status"] == "ok"
            and not r.get("has_schema_drift")
            and (r.get("ecs") or {}).get("beta", {}).get("ecs_ready")
            and (r.get("ecs") or {}).get("stage", {}).get("ecs_ready")
        ]
        clean_label = "Clean (no drift, ECS-ready on both)"
    else:
        clean = [
            r["prefix"]
            for r in report["results"]
            if r["status"] == "ok"
            and not r.get("has_schema_drift")
            and (r.get("ecs") or {}).get("stage", {}).get("ecs_ready")
        ]
        clean_label = "Clean (Stage present, ECS-ready)"
    print()
    print(_hr("-"))
    if clean:
        print(f" {clean_label}: {len(clean)}")
        print("  " + ", ".join(clean))
    else:
        print(f" {clean_label}: 0")
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
    enable_beta: Optional[bool] = None,
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

    beta_enabled = ENABLE_BETA if enable_beta is None else enable_beta

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
            "No prefixes left after PREFIX_FILTER=%r — nothing to compare",
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

    beta_cfg = dict(config.get("beta") or {})
    stage_cfg = dict(config.get("stage") or {})
    if shared_verify is not None:
        beta_cfg.setdefault("verify_certs", shared_verify)
        stage_cfg.setdefault("verify_certs", shared_verify)

    beta: Optional[Elasticsearch] = None
    if beta_enabled:
        beta = build_es_client(
            "BETA_ES_URL", "BETA_ES_USER", "BETA_ES_PASSWORD", "Beta", beta_cfg
        )
    else:
        logger.info(
            "ENABLE_BETA=false — skipping Beta client build/ping (Stage-only mode)"
        )

    stage = build_es_client(
        "STAGE_ES_URL", "STAGE_ES_USER", "STAGE_ES_PASSWORD", "Stage", stage_cfg
    )

    clients_to_ping: List[Tuple[str, Elasticsearch]] = [("Stage", stage)]
    if beta_enabled and beta is not None:
        clients_to_ping.insert(0, ("Beta", beta))

    for label, client in clients_to_ping:
        try:
            if not client.ping():
                print(
                    f"WARNING: {label} cluster did not respond to ping()",
                    file=sys.stderr,
                )
        except Exception as exc:  # noqa: BLE001
            print(
                f"WARNING: {label} ping failed: {_explain_transport_error(exc)}",
                file=sys.stderr,
            )

    # Pinned day: require Stage daily indices BEFORE creating results/<day>/.
    if as_of is not None:
        stage_hits = count_stage_indices_for_day(stage, prefixes, as_of)
        if stage_hits == 0:
            suffix = index_date_suffix(as_of)
            msg = (
                f"No Stage indices for INDEX_DATE={day_label} "
                f"(looked for *-logs-{suffix} / *-{suffix}). "
                "Results folder was NOT created."
            )
            logger.error(msg)
            print(f"ERROR: {msg}", file=sys.stderr)
            stale = results_root / day_label
            if stale.is_dir():
                # Do not keep a day folder when Stage has no daily indices.
                shutil.rmtree(stale, ignore_errors=True)
                print(f"Removed empty/invalid results folder: {stale}", file=sys.stderr)
            return 1
        logger.info(
            "INDEX_DATE=%s — found %d Stage index(es) for %s",
            day_label,
            stage_hits,
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

    report = compare_clusters(
        beta,
        stage,
        prefixes=prefixes,
        enable_beta=beta_enabled,
        as_of=as_of,
    )
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

    if report["prefixes_with_type_mismatches"] or report["prefixes_with_schema_drift"]:
        return 2
    return 0



if __name__ == "__main__":
    # Optional inline config dictionary (overrides / supplements env vars).
    # Leave empty to rely solely on environment variables.
    CONFIG: Dict[str, Any] = {
        # "beta": {"url": "https://beta-es:9200", "user": "elastic", "password": "..."},
        # "stage": {"url": "https://stage-es:9200", "user": "elastic", "password": "..."},
        # "verify_certs": False,
    }
    sys.exit(main(config=CONFIG))
