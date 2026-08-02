"""EFKSchema Analyzer — Streamlit dashboard."""

from __future__ import annotations

import json
import os
import re
from datetime import date as date_cls, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import yaml

from compare_es_mappings import (
    ECS_FIELDS,
    build_es_client,
    entry_fields,
    entry_index_name,
    entry_index_size,
    normalize_ecs,
)

RESULTS_ROOT = Path(__file__).resolve().parent / "results"
REQUIRED_FILES = (
    "central_format_readiness.csv",
    "all_index_mappings.csv",
    "mapping_comparison.json",
)
DAY_FOLDER_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:_\d{6})?$")

# Legacy CSV column names → current schema
_LEGACY_CSV_COLUMNS = {
    "stage_index": "index_name",
    "stage_fields": "field_count",
    "stage_docs": "docs_count",
    "stage_index_size": "index_size",
    "stage_avg_log_size": "avg_log_size",
    "total_fields_stage": "total_fields",
    "stage_resolved_index": "resolved_index",
    "stage_data_type": "data_type",
    # Old readiness used one overloaded count for the 0–5 progress bar.
    "ecs_compliant_fields_count": "ecs_total_fields",
}

st.set_page_config(
    page_title="EFK Schema Analyzer",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Disable Streamlit "C" = Clear cache (conflicts with copy / Ctrl+C workflows).
try:
    st.set_option("client.toolbarMode", "viewer")
except Exception:  # noqa: BLE001
    pass

# UI theme toggle (dark by default). Syncs Streamlit's native theme so
# dataframes / selects / date inputs actually retheme (canvas + BaseWeb).
if "ui_theme" not in st.session_state:
    # Prefer URL ?theme= when present (survives full reload).
    _qp_theme = str(st.query_params.get("theme", "") or "").lower()
    st.session_state.ui_theme = (
        _qp_theme if _qp_theme in {"dark", "light"} else "dark"
    )

_UI_THEME = st.session_state.ui_theme
_IS_DARK = _UI_THEME == "dark"
_NATIVE_THEME_NAME = "Dark" if _IS_DARK else "Light"

# Keep query param in sync for reload persistence.
if st.query_params.get("theme") != _UI_THEME:
    st.query_params["theme"] = _UI_THEME

# Apply Streamlit native theme via localStorage (Light/Dark), then reload once.
components.html(
    f"""
    <script>
    (function () {{
      const desired = {_NATIVE_THEME_NAME!r};
      const path = window.parent.location.pathname;
      const key = `stActiveTheme-${{path}}-v2`;
      let current = null;
      try {{
        const raw = window.parent.localStorage.getItem(key);
        current = raw ? JSON.parse(raw) : null;
      }} catch (e) {{
        current = null;
      }}
      if (current !== desired) {{
        window.parent.localStorage.setItem(key, JSON.stringify(desired));
        window.parent.location.reload();
      }}
    }})();
    </script>
    """,
    height=0,
)

st.markdown(
    """
    <style>
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header {visibility: hidden;}

    .block-container {
        padding-top: 1.25rem;
        padding-bottom: 2rem;
        max-width: 1480px;
    }

    h1 {
        letter-spacing: -0.02em;
        margin-bottom: 0.35rem !important;
    }

    /* Metrics — subtle cards on top of Streamlit theme surfaces */
    div[data-testid="stMetric"] {
        background-color: var(--secondary-background-color, rgba(125, 125, 125, 0.06));
        border: 1px solid rgba(125, 125, 125, 0.22);
        border-radius: 12px;
        padding: 1rem;
        height: 100%;
        min-height: 110px;
        display: flex;
        flex-direction: column;
        justify-content: center;
        box-sizing: border-box;
    }

    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stMetric"]) {
        align-items: stretch;
    }

    div[data-testid="stHorizontalBlock"]:has(div[data-testid="stMetric"])
      > div[data-testid="stColumn"] {
        flex: 1 1 0 !important;
        width: 0 !important;
        min-width: 0 !important;
    }

    div[data-testid="stMetric"] label {
        color: inherit !important;
        opacity: 0.75;
        min-height: 2.4rem;
        display: flex;
        align-items: flex-end;
        line-height: 1.2;
    }

    div[data-testid="stMetric"] [data-testid="stMetricValue"] {
        color: inherit !important;
        font-weight: 700;
        font-size: 1.75rem !important;
    }

    .stTabs [data-baseweb="tab-list"] {
        gap: 0.35rem;
        border-bottom: 1px solid rgba(125, 125, 125, 0.22);
        flex-wrap: wrap;
    }

    .stTabs [data-baseweb="tab"] {
        border-radius: 8px 8px 0 0;
        padding: 0.55rem 0.9rem;
        font-weight: 600;
    }

    div[data-testid="stRadio"] > div {
        flex-wrap: wrap;
        gap: 0.35rem;
        border-bottom: 1px solid rgba(125, 125, 125, 0.22);
        padding-bottom: 0.45rem;
        margin-bottom: 0.65rem;
    }
    div[data-testid="stRadio"] label {
        background-color: var(--secondary-background-color, rgba(125, 125, 125, 0.06));
        border: 1px solid rgba(125, 125, 125, 0.22);
        border-radius: 8px 8px 0 0;
        padding: 0.4rem 0.75rem !important;
        font-weight: 600;
    }

    .stAlert,
    div[data-testid="stAlert"] {
        border-radius: 10px;
    }
    /* Readable text on yellow warning / red error tints */
    div[data-testid="stAlert"] [data-testid="stMarkdownContainer"],
    div[data-testid="stAlert"] [data-testid="stMarkdownContainer] * {
        color: #1e293b !important;
    }

    .efk-status-bar,
    .efk-info-card {
        background-color: var(--secondary-background-color, rgba(125, 125, 125, 0.06));
        border: 1px solid rgba(125, 125, 125, 0.22);
        color: inherit;
        border-radius: 10px;
    }
    .efk-status-bar {
        display: flex;
        flex-direction: column;
        gap: 0.45rem;
        padding: 0.75rem 1rem;
        margin: 0.35rem 0 0.85rem 0;
        font-size: 0.92rem;
        line-height: 1.5;
    }
    .efk-status-bar .efk-status-row {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 0.3rem 0.65rem;
    }
    .efk-status-bar .efk-status-sep {
        opacity: 0.55;
        user-select: none;
    }
    .efk-status-bar code,
    .efk-days-found code,
    .efk-info-card code {
        background-color: rgba(125, 125, 125, 0.15);
        color: inherit;
        padding: 0.1rem 0.35rem;
        border-radius: 4px;
    }

    .efk-days-found {
        opacity: 0.85;
        font-size: 0.88rem;
        line-height: 1.55;
        margin: 0.15rem 0 0.55rem 0;
    }
    .efk-days-found div + div {
        margin-top: 0.2rem;
    }

    div[data-testid="stExpander"] {
        border: 1px solid rgba(125, 125, 125, 0.22);
        border-radius: 10px;
    }

    .conflict-banner {
        background: linear-gradient(90deg, #7f1d1d 0%, #991b1b 55%, #b45309 100%);
        color: #fff7ed;
        padding: 0.85rem 1.1rem;
        border-radius: 10px;
        font-weight: 600;
        margin-bottom: 0.75rem;
        border: 1px solid #fca5a5;
    }

    .efk-info-card {
        padding: 1.15rem 1.35rem;
        margin: 0.35rem 0 1.1rem 0;
        line-height: 1.55;
        border-radius: 12px;
    }
    .efk-info-card h3 {
        margin: 0 0 0.75rem 0;
        font-size: 1.15rem;
    }
    .efk-info-card dl {
        margin: 0;
        display: grid;
        grid-template-columns: minmax(140px, 200px) 1fr;
        gap: 0.45rem 1rem;
    }
    .efk-info-card dt {
        opacity: 0.75;
        font-weight: 600;
    }
    .efk-info-card dd {
        margin: 0;
    }

    div[data-testid="stHorizontalBlock"] button[kind="primary"] {
        min-height: 2.6rem;
        font-weight: 600;
    }

    .efk-theme-row {
        display: flex;
        justify-content: flex-end;
        align-items: center;
        padding-top: 0.55rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def _is_complete_run(path: Path) -> bool:
    return path.is_dir() and all((path / name).is_file() for name in REQUIRED_FILES)


def _day_from_folder(name: str) -> str | None:
    match = DAY_FOLDER_RE.match(name)
    return match.group(1) if match else None


def _infer_day_from_report(run_dir: Path) -> str | None:
    meta_path = run_dir / "mapping_comparison.json"
    if not meta_path.is_file():
        return None
    try:
        with meta_path.open(encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if meta.get("index_date"):
        return str(meta["index_date"])[:10].replace(".", "-")
    for entry in meta.get("results") or []:
        name = str(entry_index_name(entry) or "")
        m = re.search(r"(\d{4})\.(\d{2})\.(\d{2})", name)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def _folder_has_indices_for_day(run_dir: Path, day: str) -> bool:
    """True if the report resolved at least one index for ``day``."""
    meta_path = run_dir / "mapping_comparison.json"
    if not meta_path.is_file():
        return False
    try:
        with meta_path.open(encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return False
    suffix = day.replace("-", ".")
    for entry in meta.get("results") or []:
        idx = entry_index_name(entry)
        if idx and suffix in str(idx):
            return True
    return False


def _normalize_csv_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename legacy stage_* / ecs CSV columns to current names when present."""
    rename = {
        old: new
        for old, new in _LEGACY_CSV_COLUMNS.items()
        if old in df.columns and new not in df.columns
    }
    if rename:
        df = df.rename(columns=rename)
    return df


@st.cache_data(show_spinner=False)
def discover_days(results_root: str) -> dict[str, str]:
    """
    Map index-day ``YYYY-MM-DD`` → best results folder name.

    Prefers exact ``YYYY-MM-DD`` folders over timestamped legacy runs.
    """
    root = Path(results_root)
    if not root.exists():
        return {}

    by_day: dict[str, list[tuple[int, float, str]]] = {}
    for child in root.iterdir():
        if child.name in {"latest", "index_mappings"} or child.is_symlink():
            continue
        if not _is_complete_run(child):
            continue
        day = _day_from_folder(child.name) or _infer_day_from_report(child)
        if not day:
            continue
        if not _folder_has_indices_for_day(child, day):
            continue
        rank = 0 if child.name == day else 1
        try:
            mtime = (child / "mapping_comparison.json").stat().st_mtime
        except OSError:
            mtime = child.stat().st_mtime
        by_day.setdefault(day, []).append((rank, -mtime, child.name))

    chosen: dict[str, str] = {}
    for day, candidates in by_day.items():
        candidates.sort()
        chosen[day] = candidates[0][2]
    return dict(sorted(chosen.items(), reverse=True))


def _run_path(run_id: str) -> Path:
    if run_id == "legacy":
        return RESULTS_ROOT
    return RESULTS_ROOT / run_id


def _boolify(df: pd.DataFrame, cols: tuple[str, ...]) -> pd.DataFrame:
    for col in cols:
        if col in df.columns:
            df[col] = (
                df[col]
                .astype(str)
                .str.strip()
                .str.upper()
                .isin({"TRUE", "1", "YES"})
            )
    return df


@st.cache_data(show_spinner=False)
def list_es_index_days() -> list[str]:
    """Ask Elasticsearch which daily index dates exist (for the date picker)."""
    try:
        from compare_es_mappings import (  # local import keeps Streamlit startup light
            list_available_index_dates,
            load_service_prefixes,
        )

        es = build_es_client()
        days = list_available_index_dates(es, load_service_prefixes())
        return [d.isoformat() for d in days]
    except Exception:  # noqa: BLE001
        return []


def fetch_index_day(
    day: str,
    prefix_filter: str = "",
) -> tuple[int, str]:
    """Run index analysis for a pinned index day; returns (exit_code, log_tail)."""
    import contextlib
    import io

    from compare_es_mappings import main as compare_main

    prev = os.environ.get("PREFIX_FILTER")
    if prefix_filter:
        os.environ["PREFIX_FILTER"] = prefix_filter
    elif "PREFIX_FILTER" in os.environ:
        del os.environ["PREFIX_FILTER"]

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = compare_main(index_date=day)
    finally:
        if prev is None:
            os.environ.pop("PREFIX_FILTER", None)
        else:
            os.environ["PREFIX_FILTER"] = prev
    return code, buf.getvalue()[-4000:]


def _recent_index_days(es_days: list[str], n: int = 3) -> list[str]:
    """Newest ``n`` days from ES listing, else last ``n`` calendar days."""
    if es_days:
        return list(es_days[:n])
    today = date_cls.today()
    return [(today - timedelta(days=i)).isoformat() for i in range(n)]


def _ensure_days_cached(
    days: list[str],
    *,
    cached: dict[str, str],
    prefix_filter: str = "",
    attempted: set[str] | None = None,
) -> tuple[dict[str, str], list[tuple[str, int, str]]]:
    """
    Fetch any of ``days`` that are missing from ``cached``.

    Skips days already in ``attempted`` (failed previously this session).
    Returns updated cache map and list of (day, exit_code, log_tail) for fetches.
    """
    attempted = attempted if attempted is not None else set()
    results: list[tuple[str, int, str]] = []
    updated = dict(cached)

    for day in days:
        if not day or day in updated or day in attempted:
            continue
        code, log_tail = fetch_index_day(day, prefix_filter=prefix_filter)
        results.append((day, code, log_tail))
        attempted.add(day)
        discover_days.clear()
        load_data.clear()
        list_es_index_days.clear()
        updated = discover_days(str(RESULTS_ROOT))

    return updated, results


@st.cache_data(show_spinner=False)
def load_team_options() -> list[str]:
    """Team namespaces from prefixes.json (+ fallbacks from service names)."""
    path = Path(__file__).resolve().parent / "prefixes.json"
    teams: set[str] = set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        for t in data.get("core_domain_prefixes") or []:
            if str(t).strip():
                teams.add(str(t).strip().lower())
        for p in data.get("service_prefixes") or []:
            name = str(p).strip().lower()
            if name:
                teams.add(name.split("-", 1)[0].split(".", 1)[0])
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return sorted(teams)


def _core_team(prefix: str) -> str:
    p = str(prefix or "").strip().lower()
    if not p:
        return ""
    return p.split("-", 1)[0].split(".", 1)[0]


def apply_project_filter(
    readiness: pd.DataFrame,
    mappings: pd.DataFrame,
    comparison: dict[str, Any],
    field_counts: pd.DataFrame | None,
    team: str,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any], pd.DataFrame | None]:
    """Keep only rows/projects for team namespace, or everything when team is All."""
    if not team or team == "All":
        return readiness, mappings, comparison, field_counts

    team_l = team.lower()
    ready = readiness
    if "core_team" in readiness.columns:
        ready = readiness[
            readiness["core_team"].astype(str).str.lower() == team_l
        ].copy()
    elif "project_prefix" in readiness.columns:
        ready = readiness[
            readiness["project_prefix"].map(_core_team) == team_l
        ].copy()

    maps = mappings
    if "project_prefix" in mappings.columns:
        maps = mappings[
            mappings["project_prefix"].map(_core_team) == team_l
        ].copy()

    counts = field_counts
    if field_counts is not None:
        if "team" in field_counts.columns:
            counts = field_counts[
                field_counts["team"].astype(str).str.lower() == team_l
            ].copy()
        elif "service" in field_counts.columns:
            counts = field_counts[
                field_counts["service"].map(_core_team) == team_l
            ].copy()

    filtered_results = [
        entry
        for entry in (comparison.get("results") or [])
        if _core_team(entry.get("prefix", "")) == team_l
    ]
    filtered_comparison = dict(comparison)
    filtered_comparison["results"] = filtered_results
    filtered_comparison["prefixes_total"] = len(filtered_results)
    filtered_comparison["prefixes_with_schema_drift"] = sum(
        1 for r in filtered_results if r.get("has_schema_drift")
    )
    filtered_comparison["prefixes_not_ecs_ready"] = sum(
        1
        for r in filtered_results
        if entry_index_name(r) and not normalize_ecs(r.get("ecs")).get("ecs_ready", False)
    )
    filtered_comparison["prefixes_with_ecs_gaps"] = filtered_comparison[
        "prefixes_not_ecs_ready"
    ]
    return ready, maps, filtered_comparison, counts


@st.cache_data(show_spinner="Loading migration reports…")
def load_data(
    run_id: str,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any], pd.DataFrame | None]:
    run_dir = _run_path(run_id)
    readiness_csv = run_dir / "central_format_readiness.csv"
    mappings_csv = run_dir / "all_index_mappings.csv"
    comparison_json = run_dir / "mapping_comparison.json"
    field_counts_csv = run_dir / "index_field_counts.csv"

    missing = [
        p.name
        for p in (readiness_csv, mappings_csv, comparison_json)
        if not p.exists()
    ]
    if missing:
        raise FileNotFoundError(
            "Missing required result file(s): "
            + ", ".join(missing)
            + f". Expected under `{run_dir}`."
        )

    readiness = _normalize_csv_columns(
        _boolify(pd.read_csv(readiness_csv), ("can_centralize_as_is",))
    )
    mappings = _normalize_csv_columns(
        _boolify(pd.read_csv(mappings_csv), ("is_ecs_standard",))
    )
    with comparison_json.open(encoding="utf-8") as fh:
        comparison = json.load(fh)

    # Backfill ecs_core_score from the JSON report when older CSVs lack it.
    if "ecs_core_score" not in readiness.columns and "project_prefix" in readiness.columns:
        score_by_prefix: dict[str, int] = {}
        for entry in comparison.get("results") or []:
            prefix = str(entry.get("prefix") or "")
            ecs_info = normalize_ecs(entry.get("ecs"))
            score_by_prefix[prefix] = int(ecs_info.get("ecs_fields_present") or 0)
        readiness["ecs_core_score"] = (
            readiness["project_prefix"].astype(str).map(score_by_prefix).fillna(0).astype(int)
        )
    if "ecs_total_fields" not in readiness.columns and "ecs_compliant_fields_count" in readiness.columns:
        readiness["ecs_total_fields"] = readiness["ecs_compliant_fields_count"]

    field_counts = None
    if field_counts_csv.is_file():
        field_counts = _normalize_csv_columns(pd.read_csv(field_counts_csv))

    return readiness, mappings, comparison, field_counts


def _prefix_lookup(comparison: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        entry.get("prefix"): entry
        for entry in comparison.get("results", [])
        if entry.get("prefix")
    }


def _fields_to_yaml(fields: dict[str, Any] | None) -> str:
    return yaml.dump(
        fields or {},
        sort_keys=True,
        default_flow_style=False,
        allow_unicode=True,
    )


def _size_label(size: dict[str, Any] | None, key: str = "store_size") -> str:
    if not size:
        return "—"
    human = size.get(key) or size.get("store_size")
    docs = size.get("docs_count")
    avg = size.get("avg_log_size")
    parts = []
    if human:
        parts.append(str(human))
    if docs is not None:
        parts.append(f"{docs:,} docs" if isinstance(docs, int) else f"{docs} docs")
    if avg:
        parts.append(f"avg {avg}")
    return " · ".join(parts) if parts else "—"


def _ecs_checklist_df(ecs_side: dict[str, Any] | None) -> pd.DataFrame:
    fields = (ecs_side or {}).get("fields") or {}
    rows = []
    for name, detail in fields.items():
        detail = detail or {}
        legacy = detail.get("legacy_alternatives_found") or []
        rows.append(
            {
                "ecs_field": name,
                "present": bool(detail.get("present")),
                "status": detail.get("status")
                or ("ecs" if detail.get("present") else "missing"),
                "mapped_type": detail.get("type") or "—",
                "legacy_alternatives": ", ".join(legacy) if legacy else "",
            }
        )
    return pd.DataFrame(rows)


CORE_ECS_FIELD_NAMES = (
    "@timestamp",
    "log.level",
    "message",
    "service.name",
    "host.name",
)


def _ecs_matrix_cell(detail: dict[str, Any]) -> str:
    """
    Render one core-ECS matrix cell.

    - ecs     → ✅ ecs
    - legacy  → ⚠️ <actual emitted field name(s) from the index mapping>
    - missing → ❌ missing
    """
    status = (detail or {}).get("status") or "missing"
    if status == "ecs":
        return "✅ ecs"
    if status == "missing":
        return "❌ missing"

    legacy = list((detail or {}).get("legacy_alternatives_found") or [])
    if legacy:
        return "⚠️ " + ", ".join(str(name) for name in legacy)
    return "⚠️ legacy"


def _ecs_matrix_df(comparison: dict[str, Any]) -> pd.DataFrame:
    """One row per project: status of each core ECS field (with emitted names)."""
    rows = []
    for entry in comparison.get("results") or []:
        ecs_info = normalize_ecs(entry.get("ecs"))
        details = (ecs_info.get("fields") or {}) if ecs_info else {}
        mapped_fields = entry_fields(entry)
        row: dict[str, Any] = {
            "project_prefix": entry.get("prefix"),
            "index_name": entry_index_name(entry) or "",
            "ecs_ready": bool(ecs_info.get("ecs_ready")),
            "ecs_score": (
                f"{ecs_info.get('ecs_fields_present', 0)}/"
                f"{ecs_info.get('ecs_fields_total', 5)}"
                if ecs_info
                else "0/5"
            ),
        }
        for field in CORE_ECS_FIELD_NAMES:
            detail = details.get(field) or {}
            # Recover emitted names from the mapping when not stored on the report.
            if (
                detail.get("status") == "legacy"
                and not detail.get("legacy_alternatives_found")
                and mapped_fields
            ):
                found = [a for a in (ECS_FIELDS.get(field) or []) if a in mapped_fields]
                if found:
                    detail = {**detail, "legacy_alternatives_found": found}
            row[field] = _ecs_matrix_cell(detail)
        rows.append(row)
    return pd.DataFrame(rows)


def _schema_issue_reason(entry: dict[str, Any]) -> str:
    """Human-readable reason a project is flagged (or clean)."""
    idx = entry_index_name(entry)
    status = str(entry.get("status") or "")
    if (
        not idx
        or idx in ("MISSING", "DISABLED")
        or status in {"missing_index", "missing_stage"}
    ):
        return "No index found on Elasticsearch"

    ecs_info = normalize_ecs(entry.get("ecs"))
    issues = entry.get("issues") or {}
    missing = list(issues.get("missing_ecs_fields") or [])
    if missing:
        return "Missing core ECS: " + ", ".join(missing)
    if not ecs_info.get("ecs_ready", False):
        return "Core ECS score < 5"
    return "Clean (Compliant)"


def _project_status_table(comparison: dict[str, Any]) -> pd.DataFrame:
    rows = []
    for entry in comparison.get("results") or []:
        ecs_info = normalize_ecs(entry.get("ecs"))
        issues = entry.get("issues") or {}
        rows.append(
            {
                "project_prefix": entry.get("prefix"),
                "status": entry.get("status"),
                "error": entry.get("error") or "",
                "index_name": entry_index_name(entry) or "",
                "has_schema_drift": bool(entry.get("has_schema_drift")),
                "schema_issue_reason": _schema_issue_reason(entry),
                "ecs_ready": bool(ecs_info.get("ecs_ready")),
                "ecs_score": (
                    f"{ecs_info.get('ecs_fields_present', 0)}/"
                    f"{ecs_info.get('ecs_fields_total', 5)}"
                    if ecs_info
                    else "—"
                ),
                "missing_ecs_fields": len(issues.get("missing_ecs_fields") or []),
                "index_size": _size_label(entry_index_size(entry)),
            }
        )
    return pd.DataFrame(rows)


def _kpi(readiness: pd.DataFrame, comparison: dict[str, Any]) -> dict[str, int]:
    missing_ecs_col = (
        readiness["missing_ecs_fields_count"]
        if "missing_ecs_fields_count" in readiness.columns
        else None
    )
    core_col = None
    if "ecs_core_score" in readiness.columns:
        core_col = pd.to_numeric(readiness["ecs_core_score"], errors="coerce")
    return {
        "total_projects": int(
            comparison.get("prefixes_total") or len(readiness)
        ),
        "ecs_ready": int((core_col >= 5).sum())
        if core_col is not None
        else int(comparison.get("prefixes_total", 0))
        - int(comparison.get("prefixes_not_ecs_ready") or 0),
        "not_ecs_ready": int(
            comparison.get("prefixes_not_ecs_ready")
            or ((core_col < 5).sum() if core_col is not None else 0)
        ),
        "legacy_workers": int(
            readiness["target_log_format"]
            .astype(str)
            .str.contains("Format 3", na=False)
            .sum()
        )
        if "target_log_format" in readiness.columns
        else 0,
        "schema_drift": int(comparison.get("prefixes_with_schema_drift") or 0),
        "ecs_gap_projects": int(
            comparison.get("prefixes_with_ecs_gaps")
            or comparison.get("prefixes_not_ecs_ready")
            or (
                (missing_ecs_col > 0).sum()
                if missing_ecs_col is not None
                else 0
            )
        ),
        "missing_ecs_fields": int(
            missing_ecs_col.fillna(0).sum() if missing_ecs_col is not None else 0
        ),
    }


def _diff_readiness(baseline: pd.DataFrame, current: pd.DataFrame) -> pd.DataFrame:
    keys = ["project_prefix"]
    cols = [
        "total_fields",
        "docs_count",
        "index_size",
        "avg_log_size",
    ]
    left = baseline[keys + [c for c in cols if c in baseline.columns]].copy()
    right = current[keys + [c for c in cols if c in current.columns]].copy()
    left = left.rename(columns={c: f"{c}_old" for c in cols if c in left.columns})
    right = right.rename(columns={c: f"{c}_new" for c in cols if c in right.columns})
    merged = left.merge(right, on="project_prefix", how="outer", indicator=True)

    def _num(col: str) -> pd.Series:
        if col not in merged.columns:
            return pd.Series([pd.NA] * len(merged))
        return pd.to_numeric(merged[col], errors="coerce")

    def _text(col: str) -> pd.Series:
        if col not in merged.columns:
            return pd.Series([""] * len(merged))
        return merged[col].astype(str).replace({"nan": "", "None": "", "<NA>": ""})

    def _delta(old_col: str, new_col: str) -> pd.Series:
        return _num(new_col) - _num(old_col)

    change = (
        merged["_merge"]
        .astype(str)
        .map(
            {
                "both": "changed",
                "left_only": "removed",
                "right_only": "added",
            }
        )
        .fillna("changed")
    )

    out = pd.DataFrame(
        {
            "project_prefix": merged["project_prefix"],
            "change": change.values,
            "fields_baseline": _num("total_fields_old"),
            "fields_current": _num("total_fields_new"),
            "fields_Δ": _delta("total_fields_old", "total_fields_new"),
            "docs_baseline": _num("docs_count_old"),
            "docs_current": _num("docs_count_new"),
            "docs_Δ": _delta("docs_count_old", "docs_count_new"),
            "size_baseline": _text("index_size_old"),
            "size_current": _text("index_size_new"),
            "avg_doc_baseline": _text("avg_log_size_old"),
            "avg_doc_current": _text("avg_log_size_new"),
        }
    )

    both = out["change"] == "changed"
    unchanged = both & (
        out["fields_Δ"].fillna(0).eq(0)
        & out["docs_Δ"].fillna(0).eq(0)
        & (out["size_baseline"].fillna("") == out["size_current"].fillna(""))
        & (out["avg_doc_baseline"].fillna("") == out["avg_doc_current"].fillna(""))
    )
    out.loc[unchanged, "change"] = "unchanged"
    change_order = {"added": 0, "removed": 1, "changed": 2, "unchanged": 3}
    out["_ord"] = out["change"].map(change_order).fillna(9)
    return out.sort_values(by=["_ord", "project_prefix"]).drop(columns=["_ord"])


def _render_list_table(title: str, items: list[Any], empty: str) -> None:
    st.markdown(f"**{title}** ({len(items)})")
    if not items:
        st.caption(empty)
        return
    st.dataframe(
        pd.DataFrame({"field": items}),
        width="stretch",
        hide_index=True,
        height=min(280, 38 + 35 * min(len(items), 8)),
    )


# ---------------------------------------------------------------------------
# Bootstrap — pick an index day, fetch from ES if needed
# ---------------------------------------------------------------------------
if "fetch_attempted_days" not in st.session_state:
    st.session_state.fetch_attempted_days = set()

cached_days = discover_days(str(RESULTS_ROOT))
es_days = list_es_index_days()
all_day_options = sorted(set(cached_days) | set(es_days), reverse=True)
recent_days = _recent_index_days(es_days, n=3)

default_day = date_cls.today()
if recent_days:
    default_day = date_cls.fromisoformat(recent_days[0])
elif cached_days:
    default_day = date_cls.fromisoformat(next(iter(cached_days)))
elif es_days:
    default_day = date_cls.fromisoformat(es_days[0])

title_col, theme_col = st.columns([8, 1.4], gap="small")
with title_col:
    st.title("🔎 EFK Schema Analyzer")
with theme_col:
    st.markdown('<div class="efk-theme-row"></div>', unsafe_allow_html=True)
    toggle_label = "☀️ Light mode" if _IS_DARK else "🌙 Dark mode"
    if st.button(
        toggle_label,
        key="ui_theme_toggle",
        width="stretch",
        help="Switch Streamlit Light/Dark theme (tables & inputs included). Default is dark.",
    ):
        new_theme = "light" if _IS_DARK else "dark"
        st.session_state.ui_theme = new_theme
        st.query_params["theme"] = new_theme
        st.rerun()

team_options = ["All"] + load_team_options()

col1, col2, col3 = st.columns([1, 1, 1], gap="medium")
with col1:
    selected_day_date = st.date_input(
        "Index day",
        value=default_day,
        help="Resolves daily indices for this calendar day "
        "(e.g. mic-ava-logs-2026.07.28).",
    )
    selected_day = selected_day_date.isoformat()
with col2:
    baseline_options = ["(none)"] + [d for d in all_day_options if d != selected_day]
    if "baseline_day_select" not in st.session_state:
        preferred = next(
            (
                d
                for d in recent_days
                if d != selected_day and d in baseline_options
            ),
            "(none)",
        )
        st.session_state.baseline_day_select = preferred
    # Drop stale selection if options changed (e.g. same as index day).
    if st.session_state.baseline_day_select not in baseline_options:
        st.session_state.baseline_day_select = "(none)"
    baseline_day = st.selectbox(
        "Compare against day",
        options=baseline_options,
        key="baseline_day_select",
        help="Auto-fetches this day from Elasticsearch when not cached.",
    )
with col3:
    project_filter = st.selectbox(
        "Projects filter",
        options=team_options,
        index=0,
        help="All = every service in prefixes.json. "
        "Pick a team (e.g. mic, ams) to limit view and Fetch.",
    )

prefix_filter_arg = "" if project_filter == "All" else project_filter

fetch_clicked = st.button(
    "⬇ Fetch from Elasticsearch",
    type="primary",
    width="stretch",
    help="Query Elasticsearch for this day's indices. Uses Projects filter "
    "(All or a single team like mic).",
)

if es_days:
    days_list = ", ".join(f"<code>{d}</code>" for d in es_days[:14])
    if len(es_days) > 14:
        days_list += " …"
    recent_list = ", ".join(f"<code>{d}</code>" for d in recent_days)
    st.markdown(
        f'<div class="efk-days-found">'
        f"<div>📅 Days found on Elasticsearch: {days_list}</div>"
        f"<div>⚡ Auto-fetch last {len(recent_days)}: {recent_list}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )
else:
    st.caption(
        "Could not list days from Elasticsearch (check `.env` / network). "
        "You can still pick a day and fetch."
    )

# Manual fetch for the selected index day
if fetch_clicked:
    with st.spinner(f"Fetching mappings for index day {selected_day}…"):
        discover_days.clear()
        load_data.clear()
        list_es_index_days.clear()
        code, log_tail = fetch_index_day(
            selected_day,
            prefix_filter=prefix_filter_arg,
        )
        st.session_state.fetch_attempted_days.add(selected_day)
    if code in (0, 2):
        st.success(
            f"Loaded index day `{selected_day}` "
            f"(exit {code}; 2 means schema/ECS issues found)."
        )
        if log_tail.strip():
            with st.expander("Fetch log", expanded=False):
                st.code(log_tail)
        cached_days = discover_days(str(RESULTS_ROOT))
        st.rerun()
    else:
        st.error(
            f"Fetch aborted for `{selected_day}` (exit {code}). "
            "No results folder was created because Elasticsearch has no daily indices "
            "for that day."
        )
        st.code(log_tail or "(no output)")
        discover_days.clear()
        cached_days = discover_days(str(RESULTS_ROOT))
        st.stop()

# Startup: auto-resolve the last 3 index days when missing from cache
missing_recent = [
    d
    for d in recent_days
    if d not in cached_days and d not in st.session_state.fetch_attempted_days
]
if missing_recent:
    with st.spinner(
        f"Auto-fetching last {len(recent_days)} days from Elasticsearch: "
        + ", ".join(missing_recent)
        + "…"
    ):
        cached_days, fetch_results = _ensure_days_cached(
            missing_recent,
            cached=cached_days,
            prefix_filter=prefix_filter_arg,
            attempted=st.session_state.fetch_attempted_days,
        )
    ok = [d for d, code, _ in fetch_results if code in (0, 2)]
    bad = [(d, code) for d, code, _ in fetch_results if code not in (0, 2)]
    if ok:
        st.success("Auto-fetched: " + ", ".join(f"`{d}`" for d in ok))
    if bad:
        st.warning(
            "Could not auto-fetch: "
            + ", ".join(f"`{d}` (exit {c})" for d, c in bad)
        )
    if fetch_results:
        st.rerun()

# Auto-fetch selected index day if still missing
if (
    selected_day not in cached_days
    and selected_day not in st.session_state.fetch_attempted_days
):
    with st.spinner(f"Auto-fetching index day `{selected_day}`…"):
        cached_days, fetch_results = _ensure_days_cached(
            [selected_day],
            cached=cached_days,
            prefix_filter=prefix_filter_arg,
            attempted=st.session_state.fetch_attempted_days,
        )
    if fetch_results:
        day, code, log_tail = fetch_results[0]
        if code in (0, 2):
            st.success(f"Auto-fetched `{day}` (exit {code}).")
            st.rerun()
        else:
            st.error(
                f"Auto-fetch failed for `{day}` (exit {code}). "
                "No daily indices for that day on Elasticsearch."
            )
            if log_tail.strip():
                st.code(log_tail)

# Auto-fetch compare-against (baseline) day when selected and missing
if (
    baseline_day != "(none)"
    and baseline_day not in cached_days
    and baseline_day not in st.session_state.fetch_attempted_days
):
    with st.spinner(f"Auto-fetching compare-against day `{baseline_day}`…"):
        cached_days, fetch_results = _ensure_days_cached(
            [baseline_day],
            cached=cached_days,
            prefix_filter=prefix_filter_arg,
            attempted=st.session_state.fetch_attempted_days,
        )
    if fetch_results:
        day, code, log_tail = fetch_results[0]
        if code in (0, 2):
            st.success(f"Auto-fetched compare day `{day}` (exit {code}).")
            st.rerun()
        else:
            st.warning(
                f"Could not fetch compare day `{day}` (exit {code}). "
                "Pick another day or check Elasticsearch."
            )
            if log_tail.strip():
                with st.expander("Fetch log", expanded=False):
                    st.code(log_tail)

# Refresh option lists after any auto-fetch path
all_day_options = sorted(set(cached_days) | set(es_days), reverse=True)

selected_run = cached_days.get(selected_day)
if not selected_run:
    st.warning(
        f"No cached results for **{selected_day}**. "
        "Click **Fetch from Elasticsearch** to pull that day's indices "
        f"(`*-{selected_day.replace('-', '.')}`)."
    )
    st.stop()

try:
    readiness_df, mappings_df, comparison_data, field_counts_df = load_data(
        selected_run
    )
except FileNotFoundError as exc:
    st.error(f"⚠️ {exc}")
    st.stop()
except Exception as exc:  # noqa: BLE001
    st.error(f"⚠️ Failed to load migration data: {exc}")
    st.stop()

readiness_df, mappings_df, comparison_data, field_counts_df = apply_project_filter(
    readiness_df,
    mappings_df,
    comparison_data,
    field_counts_df,
    project_filter,
)

if readiness_df.empty and project_filter != "All":
    st.warning(
        f"No projects for team **{project_filter}** in this day's results. "
        "Choose **All**, or Fetch with this filter."
    )
    st.stop()

prefix_index = _prefix_lookup(comparison_data)
kpis = _kpi(readiness_df, comparison_data)
status_df = _project_status_table(comparison_data)

day_suffix = selected_day.replace("-", ".")
index_hits = 0
wrong_day_count = 0
for entry in comparison_data.get("results") or []:
    idx = entry_index_name(entry)
    if idx and idx not in ("DISABLED", "MISSING"):
        if day_suffix in str(idx):
            index_hits += 1
        else:
            wrong_day_count += 1

st.markdown(
    f'<div class="efk-status-bar">'
    f'<div class="efk-status-row">'
    f"Index day: <code>{comparison_data.get('index_date', selected_day)}</code>"
    f'<span class="efk-status-sep">|</span>'
    f"Folder: <code>{selected_run}</code>"
    f'<span class="efk-status-sep">|</span>'
    f"Mode: <code>{comparison_data.get('mode', 'cluster')}</code>"
    f"</div>"
    f'<div class="efk-status-row">'
    f"Filter: <code>{project_filter}</code>"
    f'<span class="efk-status-sep">|</span>'
    f"Indexes: <strong>{index_hits}</strong>"
    f'<span class="efk-status-sep">|</span>'
    f"Generated: <code>{comparison_data.get('generated_at', '—')}</code>"
    f"</div>"
    f"</div>",
    unsafe_allow_html=True,
)

if index_hits == 0:
    st.error(
        f"No daily indices for **{selected_day}** "
        f"(`*-{day_suffix}` / `*-logs-{day_suffix}`). "
        "This cached folder is invalid for index analysis."
    )
    if st.button(f"🗑 Delete invalid cache `{selected_run}`"):
        import shutil

        shutil.rmtree(_run_path(selected_run), ignore_errors=True)
        discover_days.clear()
        load_data.clear()
        st.rerun()
    st.stop()
elif wrong_day_count:
    st.warning(
        f"⚠️ **{wrong_day_count}** microservices emitted no logs on "
        f"**{selected_day}** (showing last known index mappings)."
    )

MAIN_TAB_OPTIONS = [
    "🚀 Readiness",
    "📦 Inventory",
    "🗂️ Schema",
    "✅ ECS Checklist",
    "📋 Fields",
    "⚠️ Conflicts",
    "📉 Compare days",
    "ℹ️ Info",
]
if "main_dashboard_tab" not in st.session_state:
    st.session_state["main_dashboard_tab"] = MAIN_TAB_OPTIONS[0]

selected_tab = st.radio(
    "Dashboard section",
    MAIN_TAB_OPTIONS,
    key="main_dashboard_tab",
    horizontal=True,
    label_visibility="collapsed",
)

# ---------------------------------------------------------------------------
# Tab — Readiness
# ---------------------------------------------------------------------------
if selected_tab == "🚀 Readiness":
    kpi_cols = st.columns(5, gap="small")
    with kpi_cols[0]:
        st.metric("Total Projects", kpis["total_projects"])
    with kpi_cols[1]:
        st.metric("ECS Ready", kpis["ecs_ready"])
    with kpi_cols[2]:
        st.metric("Not ECS Ready", kpis["not_ecs_ready"])
    with kpi_cols[3]:
        st.metric("Legacy Workers (F3)", kpis["legacy_workers"])
    with kpi_cols[4]:
        st.metric("Schema Issues", kpis["schema_drift"])

    st.markdown("##### Central format readiness")
    st.dataframe(
        readiness_df,
        width="stretch",
        hide_index=True,
        column_config={
            "project_prefix": st.column_config.TextColumn(
                "Project Prefix", width="medium"
            ),
            "core_team": st.column_config.TextColumn("Core Team", width="small"),
            "target_log_format": st.column_config.TextColumn(
                "Target Log Format", width="large"
            ),
            "ecs_core_score": st.column_config.ProgressColumn(
                "ECS Core (0–5)",
                min_value=0,
                max_value=5,
                format="%d",
            ),
            "ecs_total_fields": st.column_config.NumberColumn(
                "ECS Total Fields", format="%d"
            ),
            "can_centralize_as_is": st.column_config.CheckboxColumn(
                "Can Centralize As-Is", width="small"
            ),
            "missing_ecs_fields_count": st.column_config.NumberColumn(
                "Missing ECS Core", format="%d"
            ),
            "total_fields": st.column_config.NumberColumn("Fields", format="%d"),
            "docs_count": st.column_config.NumberColumn("Docs", format="%d"),
            "index_size": st.column_config.TextColumn("Size"),
            "avg_log_size": st.column_config.TextColumn("Avg Log"),
        },
    )

    st.markdown("##### Project status (from analysis report)")
    st.caption(
        "Includes resolve errors, ECS scores, schema issues, "
        "and missing required ECS field counts."
    )
    st.dataframe(
        status_df,
        width="stretch",
        hide_index=True,
        column_config={
            "has_schema_drift": st.column_config.CheckboxColumn("Schema Issues"),
            "schema_issue_reason": st.column_config.TextColumn(
                "Issue Reason", width="large"
            ),
            "ecs_ready": st.column_config.CheckboxColumn("ECS Ready"),
            "error": st.column_config.TextColumn("Error", width="large"),
        },
        column_order=[
            "project_prefix",
            "index_name",
            "status",
            "has_schema_drift",
            "schema_issue_reason",
            "ecs_ready",
            "ecs_score",
            "missing_ecs_fields",
            "index_size",
            "error",
        ],
    )

# ---------------------------------------------------------------------------
# Tab — Inventory
# ---------------------------------------------------------------------------
elif selected_tab == "📦 Inventory":
    if field_counts_df is None or field_counts_df.empty:
        st.warning(
            "`index_field_counts.csv` is missing for this run. "
            "Re-run `compare_es_mappings.py` to generate it."
        )
    else:
        st.markdown("##### Index inventory")
        st.caption(
            "Resolved daily indexes, field counts, docs, store size, and avg log size."
        )
        st.dataframe(
            field_counts_df,
            width="stretch",
            hide_index=True,
            column_config={
                "service": st.column_config.TextColumn("Service", width="medium"),
                "team": st.column_config.TextColumn("Team", width="small"),
                "log_format": st.column_config.TextColumn("Log Format", width="large"),
                "index_name": st.column_config.TextColumn("Index", width="medium"),
                "field_count": st.column_config.NumberColumn("Fields", format="%d"),
                "docs_count": st.column_config.NumberColumn("Docs", format="%d"),
            },
        )

        missing_index = field_counts_df[
            field_counts_df["index_name"].astype(str).str.strip().eq("")
            | field_counts_df["index_name"].isna()
        ]
        st.markdown(
            f"**Index missing:** {len(missing_index)} / {len(field_counts_df)} services"
        )
        if not missing_index.empty:
            cols = [
                c
                for c in ("service", "team", "log_format", "field_count", "docs_count")
                if c in missing_index.columns
            ]
            st.dataframe(
                missing_index[cols],
                width="stretch",
                hide_index=True,
            )

# ---------------------------------------------------------------------------
# Tab — Schema
# ---------------------------------------------------------------------------
elif selected_tab == "🗂️ Schema":
    prefixes = sorted(prefix_index.keys()) or sorted(
        readiness_df["project_prefix"].dropna().astype(str).unique()
    )
    selected = st.selectbox(
        "Choose a project prefix",
        options=prefixes,
        index=0 if prefixes else None,
        placeholder="Select a service…",
        key="schema_prefix",
    )

    if not selected:
        st.info("No project prefixes available in the loaded report.")
    else:
        entry = prefix_index.get(selected, {})
        mapped_fields = entry_fields(entry)
        issues = entry.get("issues") or {}
        ecs_info = normalize_ecs(entry.get("ecs"))
        idx_name = entry_index_name(entry)

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Index", idx_name or "MISSING")
        m2.metric("Total Fields", len(mapped_fields))
        m3.metric(
            "ECS Score",
            (
                f"{ecs_info.get('ecs_fields_present', 0)}/"
                f"{ecs_info.get('ecs_fields_total', 5)}"
                if ecs_info
                else "—"
            ),
        )
        m4.metric("Status", entry.get("status") or "—")

        if not idx_name:
            st.warning(
                f"Index missing for `{selected}` on `{selected_day}` "
                f"(`*-{day_suffix}`)."
            )

        if entry.get("error"):
            st.error(entry["error"])

        f1, f2 = st.columns(2)
        f1.metric("Schema Issues", "Yes" if entry.get("has_schema_drift") else "No")
        f2.metric(
            "ECS Gaps",
            "Yes"
            if (
                entry.get("has_ecs_gaps")
                or issues.get("missing_ecs_fields")
                or not ecs_info.get("ecs_ready", False)
            )
            else "No",
        )

        st.info(f"**Size:** {_size_label(entry_index_size(entry))}")

        st.markdown("##### ECS checklist")
        st.caption(
            "Core ECS fields vs legacy alternatives found in the mapping "
            "(legacy names do not raise the ECS score until remapped)."
        )
        st.markdown(f"**ECS ready:** `{bool(ecs_info.get('ecs_ready'))}`")
        st.dataframe(
            _ecs_checklist_df(ecs_info),
            width="stretch",
            hide_index=True,
            column_config={
                "present": st.column_config.CheckboxColumn("Present"),
                "legacy_alternatives": st.column_config.TextColumn(
                    "Legacy Alternatives", width="large"
                ),
            },
        )

        st.markdown("##### ECS gaps")
        g1, g2 = st.columns(2)
        with g1:
            _render_list_table(
                "Missing required ECS fields",
                list(issues.get("missing_ecs_fields") or []),
                "None — all core ECS fields present",
            )
        with g2:
            legacy_rows = list(issues.get("legacy_ecs_alternatives") or [])
            st.markdown(f"**Legacy ECS alternatives** ({len(legacy_rows)})")
            if not legacy_rows:
                st.caption("None")
            else:
                st.dataframe(
                    pd.DataFrame(legacy_rows),
                    width="stretch",
                    hide_index=True,
                )

        st.subheader("Index mapping")
        st.caption(f"{len(mapped_fields)} mapped fields")
        with st.expander("View Full YAML Mapping", expanded=False):
            st.code(_fields_to_yaml(mapped_fields), language="yaml")

# ---------------------------------------------------------------------------
# Tab — ECS Checklist
# ---------------------------------------------------------------------------
elif selected_tab == "✅ ECS Checklist":
    ecs_matrix = _ecs_matrix_df(comparison_data)
    ready_n = int(ecs_matrix["ecs_ready"].sum()) if not ecs_matrix.empty else 0
    total_n = len(ecs_matrix)

    e1, e2, e3 = st.columns(3)
    e1.metric("Projects", total_n)
    e2.metric("ECS Ready", ready_n)
    e3.metric("Not ECS Ready", total_n - ready_n)

    st.markdown("##### Core ECS field matrix")
    st.caption(
        "Per-project status for the five required ECS fields. "
        "`✅ ecs` = compliant · `⚠️ <emitted field>` = legacy alias found · "
        "`❌ missing` = absent."
    )

    readiness_filter = st.selectbox(
        "ECS readiness",
        options=["All", "Ready only", "Not ready only"],
        index=0,
        key="ecs_tab_ready_filter",
    )
    matrix_view = ecs_matrix
    if readiness_filter == "Ready only":
        matrix_view = matrix_view[matrix_view["ecs_ready"]]
    elif readiness_filter == "Not ready only":
        matrix_view = matrix_view[~matrix_view["ecs_ready"]]

    st.dataframe(
        matrix_view,
        width="stretch",
        hide_index=True,
        column_config={
            "project_prefix": st.column_config.TextColumn("Project", width="medium"),
            "index_name": st.column_config.TextColumn("Index", width="medium"),
            "ecs_ready": st.column_config.CheckboxColumn("ECS Ready"),
            "ecs_score": st.column_config.TextColumn("Score", width="small"),
            "@timestamp": st.column_config.TextColumn("@timestamp"),
            "log.level": st.column_config.TextColumn("log.level"),
            "message": st.column_config.TextColumn("message"),
            "service.name": st.column_config.TextColumn("service.name"),
            "host.name": st.column_config.TextColumn("host.name"),
        },
    )

    st.markdown("##### Per-project checklist")
    ecs_prefixes = sorted(prefix_index.keys()) or sorted(
        readiness_df["project_prefix"].dropna().astype(str).unique()
    )
    ecs_selected = st.selectbox(
        "Choose a project prefix",
        options=ecs_prefixes,
        index=0 if ecs_prefixes else None,
        placeholder="Select a service…",
        key="ecs_checklist_prefix",
    )
    if not ecs_selected:
        st.info("No project prefixes available in the loaded report.")
    else:
        ecs_entry = prefix_index.get(ecs_selected, {})
        ecs_info = normalize_ecs(ecs_entry.get("ecs"))
        issues = ecs_entry.get("issues") or {}
        st.markdown(
            f"**{ecs_selected}** — ready: `{bool(ecs_info.get('ecs_ready'))}` · "
            f"score: "
            f"`{ecs_info.get('ecs_fields_present', 0)}/"
            f"{ecs_info.get('ecs_fields_total', 5)}`"
        )
        st.dataframe(
            _ecs_checklist_df(ecs_info),
            width="stretch",
            hide_index=True,
            column_config={
                "present": st.column_config.CheckboxColumn("Present"),
                "legacy_alternatives": st.column_config.TextColumn(
                    "Legacy Alternatives", width="large"
                ),
            },
        )
        g1, g2 = st.columns(2)
        with g1:
            _render_list_table(
                "Missing required ECS fields",
                list(issues.get("missing_ecs_fields") or []),
                "None — all core ECS fields present",
            )
        with g2:
            legacy_rows = list(issues.get("legacy_ecs_alternatives") or [])
            st.markdown(f"**Legacy ECS alternatives** ({len(legacy_rows)})")
            if not legacy_rows:
                st.caption("None")
            else:
                st.dataframe(
                    pd.DataFrame(legacy_rows),
                    width="stretch",
                    hide_index=True,
                )

# ---------------------------------------------------------------------------
# Tab — Fields
# ---------------------------------------------------------------------------
elif selected_tab == "📋 Fields":
    st.markdown("##### All mapped fields")
    st.caption(
        "Full `all_index_mappings.csv` inventory — filter by project or ECS standard."
    )

    fc1, fc2 = st.columns(2)
    with fc1:
        fields_project_filter = st.multiselect(
            "Projects",
            options=sorted(mappings_df["project_prefix"].dropna().unique()),
            default=[],
        )
    with fc2:
        ecs_filter = st.selectbox(
            "ECS standard",
            options=["All", "ECS only", "Non-ECS only"],
            index=0,
        )

    search = st.text_input("Search field name", placeholder="e.g. Properties.elapsed")

    view = mappings_df
    if fields_project_filter:
        view = view[view["project_prefix"].isin(fields_project_filter)]
    if ecs_filter == "ECS only":
        view = view[view["is_ecs_standard"]]
    elif ecs_filter == "Non-ECS only":
        view = view[~view["is_ecs_standard"]]
    if search.strip():
        view = view[
            view["field_name"]
            .astype(str)
            .str.contains(search.strip(), case=False, na=False)
        ]

    st.caption(f"Showing {len(view):,} / {len(mappings_df):,} field rows")
    st.dataframe(
        view,
        width="stretch",
        hide_index=True,
        column_config={
            "project_prefix": st.column_config.TextColumn("Project", width="medium"),
            "field_name": st.column_config.TextColumn("Field", width="large"),
            "resolved_index": st.column_config.TextColumn("Index"),
            "data_type": st.column_config.TextColumn("Type", width="small"),
            "is_ecs_standard": st.column_config.CheckboxColumn("ECS"),
        },
    )

# ---------------------------------------------------------------------------
# Tab — Conflicts (schema / ECS issues)
# ---------------------------------------------------------------------------
elif selected_tab == "⚠️ Conflicts":
    ecs_gap_rows = []
    missing_index_rows = []
    wrong_day_rows = []
    for entry in comparison_data.get("results") or []:
        prefix = entry.get("prefix")
        ecs_info = normalize_ecs(entry.get("ecs"))
        issues = entry.get("issues") or {}
        missing_ecs = list(issues.get("missing_ecs_fields") or [])
        idx_name = entry_index_name(entry)
        if not idx_name:
            missing_index_rows.append(
                {
                    "project_prefix": prefix,
                    "status": entry.get("status"),
                    "error": entry.get("error") or "",
                }
            )
        else:
            if (
                idx_name not in ("DISABLED", "MISSING")
                and day_suffix not in str(idx_name)
            ):
                wrong_day_rows.append(
                    {
                        "project_prefix": prefix,
                        "resolved_index": idx_name,
                        "status": entry.get("status"),
                    }
                )
            if missing_ecs or not ecs_info.get("ecs_ready", False):
                for field in missing_ecs or ["(not ecs_ready)"]:
                    detail = ((ecs_info.get("fields") or {}).get(field) or {})
                    ecs_gap_rows.append(
                        {
                            "project_prefix": prefix,
                            "index_name": idx_name,
                            "missing_ecs_field": field,
                            "status": detail.get("status") or "missing",
                            "legacy_alternatives": ", ".join(
                                detail.get("legacy_alternatives_found") or []
                            ),
                            "ecs_score": (
                                f"{ecs_info.get('ecs_fields_present', 0)}/"
                                f"{ecs_info.get('ecs_fields_total', 5)}"
                            ),
                        }
                    )

    ecs_gaps_df = pd.DataFrame(ecs_gap_rows)
    missing_index_df = pd.DataFrame(missing_index_rows)
    wrong_day_df = pd.DataFrame(wrong_day_rows)

    non_ecs_fields = mappings_df[~mappings_df["is_ecs_standard"]].copy()

    kpi_c1, kpi_c2, kpi_c3, kpi_c4 = st.columns(4, gap="small")
    with kpi_c1:
        st.metric(
            "🔴 Core ECS Violations",
            f"{len(ecs_gaps_df):,}",
            help="Missing baseline ECS field rows",
        )
    with kpi_c2:
        st.metric(
            "🟡 Stale Indices",
            f"{len(wrong_day_df):,}",
            help="Date mismatches vs selected index day",
        )
    with kpi_c3:
        st.metric(
            "🟠 Offline Services",
            f"{len(missing_index_df):,}",
            help="Services with no resolved index",
        )
    with kpi_c4:
        st.metric(
            "⚪ Non-Standard Fields",
            f"{len(non_ecs_fields):,}",
            help="Custom / non-ECS mapped fields",
        )

    st.markdown(
        f"##### 🟡 Date Mismatched Indices ({len(wrong_day_df)})"
    )
    if wrong_day_df.empty:
        st.caption("All active indices match the selected index day.")
    else:
        st.dataframe(
            wrong_day_df[
                ["project_prefix", "resolved_index", "status"]
            ],
            width="stretch",
            hide_index=True,
            column_config={
                "project_prefix": st.column_config.TextColumn(
                    "Service", width="medium"
                ),
                "resolved_index": st.column_config.TextColumn(
                    "Active Index Name", width="large"
                ),
                "status": st.column_config.TextColumn("Status", width="small"),
            },
        )
        st.info(
            f"💡 Service generated no logs on **{selected_day}**. "
            "Analyzer fell back to the latest active index."
        )

    st.markdown("##### Missing required ECS fields")
    if ecs_gaps_df.empty:
        st.success("All indexes have the core ECS fields present.")
    else:
        st.dataframe(ecs_gaps_df, width="stretch", hide_index=True)

    st.markdown("##### Services missing index")
    if missing_index_df.empty:
        st.caption("None for this filter/day.")
    else:
        st.dataframe(missing_index_df, width="stretch", hide_index=True)

    st.markdown("##### Non-ECS field inventory")
    st.caption(
        "Mapped fields that are not ECS-standard paths — candidates for "
        "Fluentd mutate / remapping."
    )
    if non_ecs_fields.empty:
        st.caption("No non-ECS fields in the current filter.")
    else:
        st.dataframe(
            non_ecs_fields.sort_values(
                by=["project_prefix", "field_name"],
            ),
            width="stretch",
            hide_index=True,
            column_config={
                "project_prefix": st.column_config.TextColumn(
                    "Project", width="medium"
                ),
                "field_name": st.column_config.TextColumn("Field", width="large"),
                "data_type": st.column_config.TextColumn("Type", width="small"),
                "is_ecs_standard": st.column_config.CheckboxColumn("ECS"),
            },
        )

    st.markdown("##### Blocking ECS fields (grouped)")
    if ecs_gaps_df.empty:
        st.caption("No missing ECS fields to group.")
    else:
        grouped = (
            ecs_gaps_df.groupby("missing_ecs_field", as_index=False)
            .agg(
                projects=("project_prefix", lambda s: ", ".join(sorted(set(s)))),
                project_count=("project_prefix", "nunique"),
                statuses=("status", lambda s: ", ".join(sorted(set(map(str, s))))),
                gap_rows=("missing_ecs_field", "size"),
            )
            .sort_values(["project_count", "missing_ecs_field"], ascending=[False, True])
        )
        st.dataframe(grouped, width="stretch", hide_index=True)

# ---------------------------------------------------------------------------
# Tab — Compare days
# ---------------------------------------------------------------------------
elif selected_tab == "📉 Compare days":
    if baseline_day == "(none)":
        st.info(
            "Select a **baseline day** in the header to diff against the active index day. "
            "Fetch both days from Elasticsearch first if they are not cached."
        )
    else:
        baseline_run = cached_days.get(baseline_day)
        if not baseline_run:
            st.warning(
                f"No cached results for baseline day `{baseline_day}`. "
                "Switch to that day and click **Fetch from Elasticsearch**."
            )
        else:
            try:
                base_ready, _, base_meta, _ = load_data(baseline_run)
            except Exception as exc:  # noqa: BLE001
                st.error(f"⚠️ Failed to load baseline day `{baseline_day}`: {exc}")
                st.stop()

            base_ready, _, base_meta, _ = apply_project_filter(
                base_ready,
                pd.DataFrame(),
                base_meta,
                None,
                project_filter,
            )
            base_kpis = _kpi(base_ready, base_meta)
            cur_kpis = kpis

            st.markdown(
                f"**Baseline** `{baseline_day}` "
                f"({base_meta.get('generated_at', '—')}) → "
                f"**Current** `{selected_day}` "
                f"({comparison_data.get('generated_at', '—')})"
            )

            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric(
                "Total Projects",
                cur_kpis["total_projects"],
                delta=cur_kpis["total_projects"] - base_kpis["total_projects"],
            )
            c2.metric(
                "ECS Ready",
                cur_kpis["ecs_ready"],
                delta=cur_kpis["ecs_ready"] - base_kpis["ecs_ready"],
            )
            c3.metric(
                "Not ECS Ready",
                cur_kpis["not_ecs_ready"],
                delta=cur_kpis["not_ecs_ready"] - base_kpis["not_ecs_ready"],
                delta_color="inverse",
            )
            c4.metric(
                "Schema Issues",
                cur_kpis["schema_drift"],
                delta=cur_kpis["schema_drift"] - base_kpis["schema_drift"],
                delta_color="inverse",
            )
            c5.metric(
                "ECS Gap Projects",
                cur_kpis["ecs_gap_projects"],
                delta=cur_kpis["ecs_gap_projects"] - base_kpis["ecs_gap_projects"],
                delta_color="inverse",
            )

            diff_df = _diff_readiness(base_ready, readiness_df)

            st.markdown("##### Per-project comparison")
            st.caption(
                f"Fields, docs, index size, and avg doc size for "
                f"`{baseline_day}` vs `{selected_day}`."
            )
            st.dataframe(
                diff_df,
                width="stretch",
                hide_index=True,
                column_config={
                    "project_prefix": st.column_config.TextColumn(
                        "Project", width="medium"
                    ),
                    "change": st.column_config.TextColumn("Change", width="small"),
                    "fields_baseline": st.column_config.NumberColumn(
                        f"Fields ({baseline_day})", format="%d"
                    ),
                    "fields_current": st.column_config.NumberColumn(
                        f"Fields ({selected_day})", format="%d"
                    ),
                    "fields_Δ": st.column_config.NumberColumn(
                        "Fields Δ", format="%+d"
                    ),
                    "docs_baseline": st.column_config.NumberColumn(
                        f"Docs ({baseline_day})", format="%d"
                    ),
                    "docs_current": st.column_config.NumberColumn(
                        f"Docs ({selected_day})", format="%d"
                    ),
                    "docs_Δ": st.column_config.NumberColumn("Docs Δ", format="%+d"),
                    "size_baseline": st.column_config.TextColumn(
                        f"Size ({baseline_day})"
                    ),
                    "size_current": st.column_config.TextColumn(
                        f"Size ({selected_day})"
                    ),
                    "avg_doc_baseline": st.column_config.TextColumn(
                        f"Avg Doc ({baseline_day})"
                    ),
                    "avg_doc_current": st.column_config.TextColumn(
                        f"Avg Doc ({selected_day})"
                    ),
                },
            )

# ---------------------------------------------------------------------------
# Tab — Info & Guide
# ---------------------------------------------------------------------------
elif selected_tab == "ℹ️ Info":
    st.markdown(
        """
        <div class="efk-info-card">
          <h3>EFK Schema Migration &amp; Standardizer</h3>
          <dl>
            <dt>Author / Lead</dt>
            <dd>Backend &amp; DevOps/SRE Engineer</dd>
            <dt>Project</dt>
            <dd>EFK Schema Migration &amp; Standardizer</dd>
            <dt>Scope</dt>
            <dd>~40+ microservices standardization on Elastic Common Schema (ECS)</dd>
            <dt>Target architecture</dt>
            <dd>Fluentd → Kafka → Elasticsearch (EFK pipeline)</dd>
            <dt>Primary goal</dt>
            <dd>Eliminate mapping drifts, type collisions, and unmapped dynamic
                fields across daily indices</dd>
          </dl>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown("### Log Format Archetypes")
    st.caption(
        "Each service is tagged into one of three Fluentd central-format targets "
        "based on its Elasticsearch mapping and core ECS readiness."
    )

    st.markdown(
        """
#### Format 1 — ECS Native

| | |
|---|---|
| **Definition** | Microservices emitting structured JSON logs that natively match the **5 core ECS fields** (`@timestamp`, `log.level`, `message`, `service.name`, `host.name`). |
| **Fluentd handling** | Pass-through routing directly to Elasticsearch without heavy field transformation. |
| **Action required** | None. Fully compliant. |

#### Format 2 — Web API (Standard Mutate)

| | |
|---|---|
| **Definition** | Web APIs emitting structured logs that cover core concepts under **legacy key names** (e.g. `level` instead of `log.level`, `msg` instead of `message`, `StatusCode` instead of `http.response.status_code`). |
| **Fluentd handling** | Lightweight `record_transformer` / mutate filter to remap standard legacy keys into ECS paths. |
| **Action required** | Apply the standard Fluentd remapping config template. |

#### Format 3 — Legacy Worker (Heavy Mutate)

| | |
|---|---|
| **Definition** | Background workers, legacy jobs, or unmapped services emitting unstructured text or sparse/nested custom payloads (`Properties.*`) missing baseline core concepts. |
| **Fluentd handling** | Deep field unpacking, dynamic property extraction, or fallback label mapping (`labels.*`) to prevent Elasticsearch mapping collisions. |
| **Action required** | Refactor the logger to output structured JSON, or apply heavy Fluentd parsing rules before centralizing. |
        """
    )

    st.markdown("### Core 0–5 ECS Baseline vs Extended ECS Fields")
    st.markdown(
        """
**Core ECS baseline (0–5)** — the five mandatory anchor fields required for
basic cross-service filtering and readiness:

1. `@timestamp`
2. `log.level`
3. `message`
4. `service.name`
5. `host.name`

Legacy aliases (`level`, `msg`, `service`, `hostname`, …) are detected but
**do not** raise the core score until remapped to the ECS paths above.
Ready = **5/5**.

**Extended ECS standard fields** — broader ECS paths and namespaces used to
judge overall schema quality (examples: `trace.id`, `http.request.method`,
`error.message`, `container.id`, `event.action`, `host.ip`). These feed
**ECS Total Fields** in the Readiness table and do **not** change the 0–5
core progress bar.
        """
    )

    st.divider()
    st.markdown("### 📚 System Documentation & Operations Manual")

    st.markdown(
        """
#### 1. Executive Summary & Pipeline Architecture

```text
Microservices  →  Fluentd Collectors  →  Kafka Bus  →  Elasticsearch Cluster
```

This dashboard inventories daily Elasticsearch mappings, scores ECS readiness,
and tags each service into a Fluentd central-format archetype so logs can be
normalized before indexing.

**Primary objectives**

- Standardize **40+** microservice log schemas onto Elastic Common Schema (ECS)
- Eliminate mapping collisions, type drifts, and unmapped dynamic fields
- Collapse **40+** ad-hoc Fluentd configs into **3** core archetypes
  (ECS Native · Web API Mutate · Legacy Worker Heavy Mutate)

---

#### 2. Core 0–5 ECS Score vs Extended ECS Fields

**Core ECS baseline (0–5)** — mandatory anchor fields for readiness and
cross-service filtering:

| # | ECS path | Typical legacy aliases |
|---|----------|------------------------|
| 1 | `@timestamp` | `time`, `timestamp`, `date` |
| 2 | `log.level` | `level`, `log_level`, `severity` |
| 3 | `message` | `msg`, `log`, `log_message` |
| 4 | `service.name` | `service`, `app`, `application` |
| 5 | `host.name` | `hostname`, `host`, `host_name` |

Legacy names are **detected** but do **not** raise the core score until remapped
to the ECS paths. Ready = **5/5**. Shown as the **ECS Core (0–5)** progress bar.

**Matrix cell status indicators** (✅ ECS Checklist — Core ECS Matrix):

| Indicator | Meaning |
|-----------|---------|
| `✅ ecs` | The field natively uses the standard Elastic Common Schema path (e.g., `@timestamp`, `log.level`). |
| `⚠️ <actual_emitted_field_name>` | A legacy field alias was detected in the index mapping (e.g., `⚠️ level` means the field mapped in Elasticsearch is literally named `level`). This is the exact key name to remap to its standard ECS counterpart in Fluentd. Multiple aliases are comma-joined (e.g., `⚠️ level, log_level`). |
| `❌ missing` | The field is completely absent from the index mapping. |

**Extended ECS standard fields** — broader ECS paths / namespaces
(`http.*`, `trace.*`, `error.*`, `user.*`, `container.*`, `event.*`, …).
These populate **ECS Total Fields** (e.g. 81) for schema-quality tracking and
do **not** change the 0–5 progress bar.

---

#### 3. Glossary & Status Definitions

| Term | Meaning |
|------|---------|
| **Can Centralize As-Is** | `TRUE` when a daily index exists, has valid mappings, and reaches **5/5** Core ECS score. |
| **ECS Gap** | Any of the 5 core ECS baseline fields is missing (status ≠ `ecs`). |
| **Schema Issues / Drift** | Index is missing **or** unresolved core ECS gaps remain. |
| **Non-ECS Field** | Custom application field outside standard ECS namespaces — candidate for Fluentd `mutate` or `labels.*`. |
| **Format 1 / 2 / 3** | Target Fluentd archetype (see Log Format Archetypes above). |
| **Fields Δ / Docs Δ** | Day-over-day change in field count or document count (Compare days). |

---

#### 4. Dashboard Tabs Overview & Triage Guide

| Tab | Purpose |
|-----|---------|
| **🚀 Readiness** | High-level KPIs and centralization readiness scores. |
| **📦 Inventory** | Index names, document counts, total store sizes, and average log sizes. |
| **🗂️ Schema** | Deep-dive field mapping inspection and YAML viewer. |
| **✅ ECS Checklist** | Core 0–5 status matrix per service displaying exact emitted field names for legacy aliases (e.g. `✅ ecs`, `⚠️ level`, `❌ missing`). |
| **📋 Fields** | Complete inventory of all mapped fields across the cluster. |
| **⚠️ Conflicts** | Primary triage center for missing required ECS fields and unmapped custom fields. |
| **📉 Compare days** | Day-over-day diff engine comparing schema metrics and doc counts. |
| **ℹ️ Info** | Project metadata, archetype descriptions, and system documentation. |

**Suggested triage flow:** Readiness KPIs → Conflicts (ECS gaps / non-ECS fields)
→ ECS Checklist (per-field status) → Schema (YAML mapping) → Compare days
(regression after a change).
        """
    )
