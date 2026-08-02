#!/usr/bin/env python3
"""
Discover Elasticsearch index prefixes and merge them into prefixes.json.

Connects to the ELK/Elasticsearch cluster, lists indices, normalizes service
prefixes and core domain namespaces, then safely merges into prefixes.json
without removing existing manual entries.

Usage:
  python discover_prefixes.py
  PREFIX_FILTER=mic python discover_prefixes.py   # only merge/report mic-*
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from elasticsearch import Elasticsearch

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]

logger = logging.getLogger("discover_prefixes")

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / ".env"
PREFIXES_FILE = SCRIPT_DIR / "prefixes.json"

# Infrastructure / system index name prefixes to ignore.
IGNORE_PREFIXES: Tuple[str, ...] = (
    ".",
    "k8s-",
    "fluentd-",
    ".ds-",
    ".monitoring-",
    "metrics-",
    "apm-",
    "redlog-",
)

# Seed domains always retained even if not observed in this scan.
DEFAULT_CORE_DOMAINS: List[str] = [
    "ams",
    "ats",
    "cd",
    "ecs",
    "hrm",
    "ime",
    "mic",
    "oms",
]

# Strip trailing date fragments: -YYYY.MM.DD, -YYYY-MM-DD, -YYYY.MM, -YYYY-MM
_DATE_SUFFIX_RE = re.compile(
    r"(?:"
    r"-\d{4}[.\-]\d{2}[.\-]\d{2}"  # day
    r"|"
    r"-\d{4}[.\-]\d{2}"            # month
    r")$"
)


def _load_env() -> None:
    if load_dotenv is not None and ENV_FILE.is_file():
        load_dotenv(dotenv_path=ENV_FILE, override=False)
    elif ENV_FILE.is_file():
        with open(ENV_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip().strip("'").strip('"'))


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_url_auth(url: str) -> Tuple[str, Optional[Tuple[str, str]]]:
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        auth = (parsed.username or "", parsed.password or "")
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return parsed._replace(netloc=host).geturl(), auth
    return url, None


def _env_first(*names: str) -> Optional[str]:
    for name in names:
        val = os.environ.get(name)
        if val is not None and str(val).strip() != "":
            return val
    return None


def build_client() -> Elasticsearch:
    url = _env_first("ES_URL", "STAGE_ES_URL")
    if not url:
        raise SystemExit(
            "Missing Elasticsearch URL. Set ES_URL in .env or the environment."
        )

    url, url_auth = _parse_url_auth(url)
    user = _env_first("ES_USER", "STAGE_ES_USER")
    password = _env_first("ES_PASSWORD", "STAGE_ES_PASSWORD")
    http_auth = None
    if user is not None and password is not None:
        http_auth = (user, password)
    elif url_auth:
        http_auth = url_auth

    return Elasticsearch(
        [url],
        http_auth=http_auth,
        verify_certs=_env_bool("ES_VERIFY_CERTS", default=True),
        ssl_show_warn=False,
        timeout=int(os.environ.get("ES_TIMEOUT", "30")),
        max_retries=2,
        retry_on_timeout=True,
    )


def should_ignore_index(name: str) -> bool:
    lower = name.lower()
    for prefix in IGNORE_PREFIXES:
        if lower.startswith(prefix):
            return True
    # Template / fluentd garbage index names
    if "%{" in name:
        return True
    return False


def normalize_index_to_service_prefix(index_name: str) -> Optional[str]:
    """
    Normalize an index name to a service prefix.

    Examples:
      cd-express-logs-2026.08.01 -> cd-express
      hrm-asapay-channel-kuber-2026.08.01 -> hrm-asapay-channel-kuber
      mic-myagah-2026.08 -> mic-myagah
    """
    name = index_name.strip()
    if not name or should_ignore_index(name):
        return None

    # Repeatedly strip date suffixes, then a trailing -logs segment.
    changed = True
    while changed:
        changed = False
        stripped = _DATE_SUFFIX_RE.sub("", name)
        if stripped != name:
            name = stripped
            changed = True
            continue
        if name.endswith("-logs"):
            name = name[: -len("-logs")]
            changed = True

    name = name.strip("-")
    if not name or should_ignore_index(name):
        return None
    # Require at least team-service shape (has a hyphen) for service_prefixes;
    # bare team tokens go to core domains only.
    return name


def core_domain_of(service_prefix: str) -> Optional[str]:
    if not service_prefix:
        return None
    return service_prefix.split("-", 1)[0].lower() or None


def parse_prefix_filter(raw: Optional[str] = None) -> Tuple[Set[str], Set[str]]:
    """
    Parse PREFIX_FILTER into (teams, exact_services).

    Empty filter → empty sets (meaning: no restriction).
    """
    text = (raw if raw is not None else os.environ.get("PREFIX_FILTER", "")).strip()
    if not text:
        return set(), set()
    tokens = {t.strip().lower() for t in text.split(",") if t.strip()}
    exact = {t for t in tokens if "-" in t or "." in t}
    teams = tokens - exact
    return teams, exact


def matches_prefix_filter(name: str, teams: Set[str], exact: Set[str]) -> bool:
    if not teams and not exact:
        return True
    lower = name.lower()
    if lower in exact:
        return True
    team = core_domain_of(lower)
    return bool(team and team in teams)


def apply_prefix_filter(
    services: Set[str], domains: Set[str]
) -> Tuple[Set[str], Set[str], str]:
    """Restrict discovered sets when PREFIX_FILTER is set. Returns filter label."""
    teams, exact = parse_prefix_filter()
    if not teams and not exact:
        return services, domains, ""

    label = os.environ.get("PREFIX_FILTER", "").strip()
    filtered_services = {
        s for s in services if matches_prefix_filter(s, teams, exact)
    }
    filtered_domains = set(teams) | {
        d for d in domains if matches_prefix_filter(d, teams, exact)
    }
    for s in filtered_services:
        team = core_domain_of(s)
        if team:
            filtered_domains.add(team)
    return filtered_services, filtered_domains, label


def load_prefixes(path: Path) -> Dict[str, List[str]]:
    if not path.is_file():
        return {
            "core_domain_prefixes": list(DEFAULT_CORE_DOMAINS),
            "service_prefixes": [],
        }
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return {
        "core_domain_prefixes": list(data.get("core_domain_prefixes") or []),
        "service_prefixes": list(data.get("service_prefixes") or []),
    }


def save_prefixes(path: Path, data: Dict[str, List[str]]) -> None:
    payload = {
        "core_domain_prefixes": sorted(set(data.get("core_domain_prefixes") or [])),
        "service_prefixes": sorted(set(data.get("service_prefixes") or [])),
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")


def discover_from_cluster(es: Elasticsearch) -> Tuple[Set[str], Set[str]]:
    rows = es.cat.indices(format="json", h="index") or []
    services: Set[str] = set()
    domains: Set[str] = set()

    for row in rows:
        name = (row.get("index") or "").strip()
        if not name or should_ignore_index(name):
            continue
        service = normalize_index_to_service_prefix(name)
        if not service:
            continue
        if "-" in service:
            services.add(service)
            domain = core_domain_of(service)
            if domain:
                domains.add(domain)
        else:
            domains.add(service.lower())

    return services, domains


def merge_and_report(
    existing: Dict[str, List[str]],
    discovered_services: Set[str],
    discovered_domains: Set[str],
    prefix_filter: str = "",
) -> Tuple[Dict[str, List[str]], List[str], List[str], int]:
    """
    Merge discoveries into prefixes.json.

    With PREFIX_FILTER set, rewrite the file to **only** matching teams/services
    (non-matching entries are removed). Clear PREFIX_FILTER and re-run to restore
    the full inventory from the cluster.
    """
    old_services = set(existing.get("service_prefixes") or [])
    old_domains = set(existing.get("core_domain_prefixes") or [])
    teams, exact = parse_prefix_filter(prefix_filter or None)
    filtered = bool(teams or exact)

    if filtered:
        kept_services = {
            s for s in old_services if matches_prefix_filter(s, teams, exact)
        }
        services = kept_services | discovered_services
        domains = discovered_domains | {
            d for d in old_domains if matches_prefix_filter(d, teams, exact)
        } | set(teams)
        removed = len(old_services - services)
        new_services = sorted(discovered_services - old_services)
        new_domains = sorted(domains - old_domains)
        merged = {
            "core_domain_prefixes": sorted(domains),
            "service_prefixes": sorted(services),
        }
        return merged, new_services, new_domains, removed

    new_services = sorted(discovered_services - old_services)
    new_domains = sorted((discovered_domains | set(DEFAULT_CORE_DOMAINS)) - old_domains)
    merged = {
        "core_domain_prefixes": sorted(
            old_domains | discovered_domains | set(DEFAULT_CORE_DOMAINS)
        ),
        "service_prefixes": sorted(old_services | discovered_services),
    }
    return merged, new_services, new_domains, 0


def print_summary(
    merged: Dict[str, List[str]],
    new_services: List[str],
    new_domains: List[str],
    discovered_services: Set[str],
    discovered_domains: Set[str],
    prefix_filter: str = "",
    removed_services: int = 0,
) -> None:
    print("=" * 72)
    print(" Elasticsearch Prefix Discovery")
    if prefix_filter:
        print(
            f" PREFIX_FILTER={prefix_filter!r} "
            f"(prefixes.json rewritten to matching entries only)"
        )
    print("=" * 72)
    print(f" Discovered service prefixes : {len(discovered_services)}")
    print(f" Discovered core domains     : {len(discovered_domains)}")
    print(f" New services merged         : {len(new_services)}")
    print(f" New domains merged          : {len(new_domains)}")
    if removed_services:
        print(f" Services removed by filter  : {removed_services}")
    print(f" Total services in file      : {len(merged['service_prefixes'])}")
    print(f" Total domains in file       : {len(merged['core_domain_prefixes'])}")
    print("-" * 72)

    print(" Core domain prefixes:")
    print("  " + ", ".join(merged["core_domain_prefixes"]))

    if new_domains:
        print("\n Newly added domains:")
        for d in new_domains:
            print(f"  + {d}")

    if new_services:
        print(f"\n Newly added service prefixes ({len(new_services)}):")
        for s in new_services:
            print(f"  + {s}")
    elif removed_services:
        print("\n File pruned to PREFIX_FILTER scope (no newly discovered services).")
    else:
        print("\n No new service prefixes (file already up to date).")

    print("\n Services by team namespace:")
    by_team: Dict[str, List[str]] = {}
    for s in merged["service_prefixes"]:
        team = core_domain_of(s) or "other"
        by_team.setdefault(team, []).append(s)
    if not by_team:
        print("  (empty)")
    for team in sorted(by_team):
        print(f"  [{team}] {len(by_team[team])} services")
    print("=" * 72)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Discover Elasticsearch index prefixes into prefixes.json"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover and print, but do not write prefixes.json",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("elasticsearch").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    _load_env()
    es = build_client()
    try:
        if not es.ping():
            print("WARNING: Elasticsearch ping failed", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: Elasticsearch ping error: {exc}", file=sys.stderr)

    existing = load_prefixes(PREFIXES_FILE)
    discovered_services, discovered_domains = discover_from_cluster(es)
    discovered_services, discovered_domains, prefix_filter = apply_prefix_filter(
        discovered_services, discovered_domains
    )
    merged, new_services, new_domains, removed_services = merge_and_report(
        existing,
        discovered_services,
        discovered_domains,
        prefix_filter=prefix_filter,
    )

    print_summary(
        merged,
        new_services,
        new_domains,
        discovered_services,
        discovered_domains,
        prefix_filter=prefix_filter,
        removed_services=removed_services,
    )

    if args.dry_run:
        print(" Dry-run: prefixes.json not modified.")
        return 0

    save_prefixes(PREFIXES_FILE, merged)
    print(f" Wrote: {PREFIXES_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
