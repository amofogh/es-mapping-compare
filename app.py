"""EFK Migration & Schema Analyzer — Streamlit dashboard."""

from __future__ import annotations

import json
import os
import re
from datetime import date as date_cls
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
import yaml

RESULTS_ROOT = Path(__file__).resolve().parent / "results"
REQUIRED_FILES = (
    "central_format_readiness.csv",
    "all_index_mappings.csv",
    "mapping_comparison.json",
)
DAY_FOLDER_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:_\d{6})?$")

st.set_page_config(
    page_title="EFK Schema Migration",
    layout="wide",
    initial_sidebar_state="collapsed",
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

    div[data-testid="stMetric"] {
        background: linear-gradient(145deg, #0f172a 0%, #1e293b 100%);
        border: 1px solid #334155;
        border-radius: 12px;
        padding: 1rem 1.1rem;
        box-shadow: 0 4px 14px rgba(15, 23, 42, 0.25);
        height: 100%;
        min-height: 110px;
        display: flex;
        flex-direction: column;
        justify-content: center;
        box-sizing: border-box;
    }

    /* Equal-width KPI columns */
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
        color: #94a3b8 !important;
        min-height: 2.4rem;
        display: flex;
        align-items: flex-end;
        line-height: 1.2;
    }

    div[data-testid="stMetric"] [data-testid="stMetricValue"] {
        color: #f8fafc !important;
        font-weight: 700;
        font-size: 1.75rem !important;
    }

    .stTabs [data-baseweb="tab-list"] {
        gap: 0.35rem;
        border-bottom: 1px solid #334155;
        flex-wrap: wrap;
    }

    .stTabs [data-baseweb="tab"] {
        border-radius: 8px 8px 0 0;
        padding: 0.55rem 0.9rem;
        font-weight: 600;
    }

    .stAlert { border-radius: 10px; }

    /* Sleek dashboard status bar */
    .efk-status-bar {
        background: #0f172a;
        border: 1px solid #1e293b;
        color: #cbd5e1;
        border-radius: 10px;
        padding: 0.75rem 1rem;
        margin: 0.35rem 0 0.85rem 0;
        font-size: 0.92rem;
        line-height: 1.45;
    }
    .efk-status-bar code {
        color: #e2e8f0;
        background: #1e293b;
        padding: 0.1rem 0.35rem;
        border-radius: 4px;
    }

    div[data-testid="stExpander"] {
        border: 1px solid #334155;
        border-radius: 10px;
        background: #0f172a;
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

    /* Align action-row primary button height */
    div[data-testid="stHorizontalBlock"] button[kind="primary"] {
        min-height: 2.6rem;
        font-weight: 600;
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
        for key in ("stage_index", "beta_index"):
            name = str(entry.get(key) or "")
            m = re.search(r"(\d{4})\.(\d{2})\.(\d{2})", name)
            if m:
                return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def _folder_has_stage_for_day(run_dir: Path, day: str) -> bool:
    """True if the report resolved at least one Stage index for ``day``."""
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
        idx = entry.get("stage_index")
        if idx and suffix in str(idx):
            return True
    return False


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
        # Skip caches with zero Stage daily indexes for that day.
        if not _folder_has_stage_for_day(child, day):
            continue
        # Prefer plain YYYY-MM-DD (rank 0) over YYYY-MM-DD_HHMMSS (rank 1)
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
    """Ask Stage ES which daily index dates exist (for the date picker)."""
    try:
        from compare_es_mappings import (  # local import keeps Streamlit startup light
            build_es_client,
            list_available_index_dates,
            load_service_prefixes,
        )

        stage = build_es_client(
            "STAGE_ES_URL", "STAGE_ES_USER", "STAGE_ES_PASSWORD", "Stage"
        )
        days = list_available_index_dates(stage, load_service_prefixes())
        return [d.isoformat() for d in days]
    except Exception:  # noqa: BLE001
        return []


def fetch_index_day(
    day: str,
    enable_beta: bool | None = None,
    prefix_filter: str = "",
) -> tuple[int, str]:
    """Run compare for a pinned index day; returns (exit_code, log_tail)."""
    import contextlib
    import io

    from compare_es_mappings import main as compare_main

    # PREFIX_FILTER is read inside filter_prefixes() / main().
    prev = os.environ.get("PREFIX_FILTER")
    if prefix_filter:
        os.environ["PREFIX_FILTER"] = prefix_filter
    elif "PREFIX_FILTER" in os.environ:
        del os.environ["PREFIX_FILTER"]

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = compare_main(index_date=day, enable_beta=enable_beta)
    finally:
        if prev is None:
            os.environ.pop("PREFIX_FILTER", None)
        else:
            os.environ["PREFIX_FILTER"] = prev
    return code, buf.getvalue()[-4000:]


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
    filtered_comparison["prefixes_with_type_mismatches"] = sum(
        1 for r in filtered_results if r.get("has_type_mismatch")
    )
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

    readiness = _boolify(pd.read_csv(readiness_csv), ("can_centralize_as_is",))
    mappings = _boolify(
        pd.read_csv(mappings_csv),
        ("is_type_mismatch", "exists_in_both_envs", "is_ecs_standard"),
    )
    with comparison_json.open(encoding="utf-8") as fh:
        comparison = json.load(fh)

    field_counts = None
    if field_counts_csv.is_file():
        field_counts = pd.read_csv(field_counts_csv)

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
                "status": detail.get("status") or ("ecs" if detail.get("present") else "missing"),
                "mapped_type": detail.get("type") or "—",
                "legacy_alternatives": ", ".join(legacy) if legacy else "",
            }
        )
    return pd.DataFrame(rows)


def _project_status_table(comparison: dict[str, Any]) -> pd.DataFrame:
    rows = []
    for entry in comparison.get("results") or []:
        stage_ecs = ((entry.get("ecs") or {}).get("stage") or {})
        beta_ecs = ((entry.get("ecs") or {}).get("beta") or {})
        cmp = entry.get("comparison") or {}
        rows.append(
            {
                "project_prefix": entry.get("prefix"),
                "status": entry.get("status"),
                "error": entry.get("error") or "",
                "beta_prefix": entry.get("beta_prefix") or "",
                "stage_index": entry.get("stage_index") or "",
                "beta_index": entry.get("beta_index") or "",
                "has_schema_drift": bool(entry.get("has_schema_drift")),
                "has_type_mismatch": bool(entry.get("has_type_mismatch")),
                "stage_ecs_ready": bool(stage_ecs.get("ecs_ready")),
                "stage_ecs_score": (
                    f"{stage_ecs.get('ecs_fields_present', 0)}/"
                    f"{stage_ecs.get('ecs_fields_total', 5)}"
                    if stage_ecs
                    else "—"
                ),
                "beta_ecs_ready": bool(beta_ecs.get("ecs_ready")) if beta_ecs else False,
                "beta_ecs_score": (
                    f"{beta_ecs.get('ecs_fields_present', 0)}/"
                    f"{beta_ecs.get('ecs_fields_total', 5)}"
                    if beta_ecs
                    else "—"
                ),
                "missing_in_stage": len(cmp.get("missing_in_stage") or []),
                "missing_in_beta": len(cmp.get("missing_in_beta") or []),
                "type_mismatches": len(cmp.get("type_mismatches") or []),
                "stage_size": _size_label(entry.get("stage_size")),
                "beta_size": _size_label(entry.get("beta_size")),
            }
        )
    return pd.DataFrame(rows)


def _kpi(readiness: pd.DataFrame, comparison: dict[str, Any]) -> dict[str, int]:
    return {
        "total_projects": int(
            comparison.get("prefixes_total") or len(readiness)
        ),
        "ecs_ready": int((readiness["ecs_compliant_fields_count"] >= 5).sum()),
        "not_ecs_ready": int(
            comparison.get("prefixes_not_ecs_ready")
            or (readiness["ecs_compliant_fields_count"] < 5).sum()
        ),
        "legacy_workers": int(
            readiness["target_log_format"]
            .astype(str)
            .str.contains("Format 3", na=False)
            .sum()
        ),
        "schema_drift": int(comparison.get("prefixes_with_schema_drift") or 0),
        "type_conflict_projects": int(
            comparison.get("prefixes_with_type_mismatches") or 0
        ),
        "type_conflicts": int(readiness["type_mismatches_count"].fillna(0).sum()),
    }


def _diff_readiness(baseline: pd.DataFrame, current: pd.DataFrame) -> pd.DataFrame:
    keys = ["project_prefix"]
    cols = [
        "total_fields_stage",
        "total_fields_beta",
        "ecs_compliant_fields_count",
        "type_mismatches_count",
        "target_log_format",
        "can_centralize_as_is",
        "stage_docs",
        "beta_docs",
    ]
    left = baseline[keys + [c for c in cols if c in baseline.columns]].copy()
    right = current[keys + [c for c in cols if c in current.columns]].copy()
    left = left.rename(columns={c: f"{c}_old" for c in cols if c in left.columns})
    right = right.rename(columns={c: f"{c}_new" for c in cols if c in right.columns})
    merged = left.merge(right, on="project_prefix", how="outer", indicator=True)

    def _delta(old_col: str, new_col: str) -> pd.Series:
        if old_col not in merged.columns or new_col not in merged.columns:
            return pd.Series([pd.NA] * len(merged))
        return pd.to_numeric(merged[new_col], errors="coerce") - pd.to_numeric(
            merged[old_col], errors="coerce"
        )

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
            "stage_fields_Δ": _delta("total_fields_stage_old", "total_fields_stage_new"),
            "beta_fields_Δ": _delta("total_fields_beta_old", "total_fields_beta_new"),
            "ecs_fields_Δ": _delta(
                "ecs_compliant_fields_count_old", "ecs_compliant_fields_count_new"
            ),
            "type_mismatches_Δ": _delta(
                "type_mismatches_count_old", "type_mismatches_count_new"
            ),
            "stage_docs_Δ": _delta("stage_docs_old", "stage_docs_new"),
            "beta_docs_Δ": _delta("beta_docs_old", "beta_docs_new"),
            "format_old": merged.get("target_log_format_old"),
            "format_new": merged.get("target_log_format_new"),
        }
    )

    both = out["change"] == "changed"
    unchanged = both & (
        out["stage_fields_Δ"].fillna(0).eq(0)
        & out["beta_fields_Δ"].fillna(0).eq(0)
        & out["ecs_fields_Δ"].fillna(0).eq(0)
        & out["type_mismatches_Δ"].fillna(0).eq(0)
        & (
            out["format_old"].astype(str).fillna("")
            == out["format_new"].astype(str).fillna("")
        )
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
        use_container_width=True,
        hide_index=True,
        height=min(280, 38 + 35 * min(len(items), 8)),
    )


# ---------------------------------------------------------------------------
# Bootstrap — pick an index day, fetch from ES if needed
# ---------------------------------------------------------------------------
cached_days = discover_days(str(RESULTS_ROOT))  # day -> folder
es_days = list_es_index_days()
all_day_options = sorted(set(cached_days) | set(es_days), reverse=True)

default_day = date_cls.today()
if cached_days:
    default_day = date_cls.fromisoformat(next(iter(cached_days)))
elif es_days:
    default_day = date_cls.fromisoformat(es_days[0])

st.title("🔎 EFK Migration & Schema Analyzer")

team_options = ["All"] + load_team_options()

# Row 1 — Date inputs & comparison setup
col1, col2, col3 = st.columns([1, 1, 1], gap="medium")
with col1:
    selected_day_date = st.date_input(
        "Index day",
        value=default_day,
        help="Resolves Stage/Beta indices for this calendar day "
        "(e.g. mic-ava-logs-2026.07.28).",
    )
    selected_day = selected_day_date.isoformat()
with col2:
    baseline_options = ["(none)"] + [d for d in all_day_options if d != selected_day]
    baseline_day = st.selectbox(
        "Compare against day",
        options=baseline_options,
        index=0,
    )
with col3:
    project_filter = st.selectbox(
        "Projects filter",
        options=team_options,
        index=0,
        help="All = every service in prefixes.json. "
        "Pick a team (e.g. mic, ams) to limit view and Fetch.",
    )

# Row 2 — Actions & toggles (pad toggle so it centers vs primary button)
col_toggle, col_btn = st.columns([1, 3], gap="medium")
with col_toggle:
    st.markdown(
        "<div style='padding-top: 1.8rem;'></div>",
        unsafe_allow_html=True,
    )
    enable_beta_ui = st.toggle(
        "Include Beta",
        value=os.environ.get("ENABLE_BETA", "false").lower() in ("1", "true", "yes"),
    )
with col_btn:
    fetch_clicked = st.button(
        "⬇ Fetch from Elasticsearch",
        type="primary",
        use_container_width=True,
        help="Query ES for this day's indices. Uses Projects filter "
        "(All or a single team like mic).",
    )

# Status — available Stage days
if es_days:
    st.caption(
        "Days found on Stage ES: "
        + ", ".join(f"`{d}`" for d in es_days[:14])
        + (" …" if len(es_days) > 14 else "")
    )
else:
    st.caption(
        "Could not list days from Elasticsearch (check `.env` / network). "
        "You can still pick a day and fetch."
    )

if fetch_clicked:
    with st.spinner(f"Fetching mappings for index day {selected_day}…"):
        # Bust caches after write
        discover_days.clear()
        load_data.clear()
        list_es_index_days.clear()
        code, log_tail = fetch_index_day(
            selected_day,
            enable_beta=enable_beta_ui,
            prefix_filter="" if project_filter == "All" else project_filter,
        )
    if code in (0, 2):
        st.success(
            f"Loaded index day `{selected_day}` "
            f"(exit {code}; 2 means drift/mismatches found)."
        )
        if log_tail.strip():
            with st.expander("Fetch log", expanded=False):
                st.code(log_tail)
        cached_days = discover_days(str(RESULTS_ROOT))
        st.rerun()
    else:
        st.error(
            f"Fetch aborted for `{selected_day}` (exit {code}). "
            "No results folder was created because Stage has no daily indices "
            "for that day."
        )
        st.code(log_tail or "(no output)")
        discover_days.clear()
        cached_days = discover_days(str(RESULTS_ROOT))
        st.stop()

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

# Apply team filter to everything shown in the panel.
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
stage_hits = 0
beta_hits = 0
stage_wrong_day = 0
beta_wrong_day = 0
for entry in comparison_data.get("results") or []:
    stage_idx = entry.get("stage_index")
    beta_idx = entry.get("beta_index")
    if stage_idx and stage_idx not in ("DISABLED", "MISSING"):
        if day_suffix in str(stage_idx):
            stage_hits += 1
        else:
            stage_wrong_day += 1
    if beta_idx and beta_idx not in ("DISABLED", "MISSING", None):
        if day_suffix in str(beta_idx):
            beta_hits += 1
        else:
            beta_wrong_day += 1

st.markdown(
    f'<div class="efk-status-bar">'
    f"Index day: <code>{comparison_data.get('index_date', selected_day)}</code> · "
    f"Folder: <code>{selected_run}</code> · "
    f"Filter: <code>{project_filter}</code> · "
    f"Generated: <code>{comparison_data.get('generated_at', '—')}</code> · "
    f"Mode: <code>{comparison_data.get('mode', '—')}</code> · "
    f"Beta: <code>{comparison_data.get('enable_beta', False)}</code> · "
    f"Stage indexes: <strong>{stage_hits}</strong> · "
    f"Beta indexes: <strong>{beta_hits}</strong>"
    f"</div>",
    unsafe_allow_html=True,
)

if stage_hits == 0:
    st.error(
        f"No Stage daily indices for **{selected_day}** "
        f"(`*-{day_suffix}` / `*-logs-{day_suffix}`). "
        "This cached folder is invalid for Stage analysis."
    )
    if st.button(f"🗑 Delete invalid cache `{selected_run}`"):
        import shutil

        shutil.rmtree(_run_path(selected_run), ignore_errors=True)
        discover_days.clear()
        load_data.clear()
        st.rerun()
    st.stop()
elif stage_wrong_day or beta_wrong_day:
    st.warning(
        f"Some resolved indexes do not contain `{day_suffix}` "
        f"(Stage wrong-day: {stage_wrong_day}, Beta wrong-day: {beta_wrong_day})."
    )

(
    tab_readiness,
    tab_inventory,
    tab_schema,
    tab_fields,
    tab_conflicts,
    tab_compare,
) = st.tabs(
    [
        "🚀 Readiness",
        "📦 Inventory",
        "🗂️ Schema",
        "📋 Fields",
        "⚠️ Conflicts",
        "📉 Compare days",
    ]
)

# ---------------------------------------------------------------------------
# Tab — Readiness
# ---------------------------------------------------------------------------
with tab_readiness:
    kpi_cols = st.columns(6, gap="small")
    with kpi_cols[0]:
        st.metric("Total Projects", kpis["total_projects"])
    with kpi_cols[1]:
        st.metric("ECS Ready", kpis["ecs_ready"])
    with kpi_cols[2]:
        st.metric("Not ECS Ready", kpis["not_ecs_ready"])
    with kpi_cols[3]:
        st.metric("Legacy Workers (F3)", kpis["legacy_workers"])
    with kpi_cols[4]:
        st.metric("Schema Drift Projects", kpis["schema_drift"])
    with kpi_cols[5]:
        st.metric(
            "Type Conflict Projects",
            kpis["type_conflict_projects"],
            help=f"{kpis['type_conflicts']} mismatched field rows total",
        )

    st.markdown("##### Central format readiness")
    st.dataframe(
        readiness_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "project_prefix": st.column_config.TextColumn(
                "Project Prefix", width="medium"
            ),
            "core_team": st.column_config.TextColumn("Core Team", width="small"),
            "target_log_format": st.column_config.TextColumn(
                "Target Log Format", width="large"
            ),
            "ecs_compliant_fields_count": st.column_config.ProgressColumn(
                "ECS Fields (0–5)",
                min_value=0,
                max_value=5,
                format="%d",
            ),
            "can_centralize_as_is": st.column_config.CheckboxColumn(
                "Can Centralize As-Is", width="small"
            ),
            "type_mismatches_count": st.column_config.NumberColumn(
                "Type Mismatches", format="%d"
            ),
            "total_fields_stage": st.column_config.NumberColumn(
                "Stage Fields", format="%d"
            ),
            "total_fields_beta": st.column_config.NumberColumn(
                "Beta Fields", format="%d"
            ),
            "common_fields_count": st.column_config.NumberColumn(
                "Common Fields", format="%d"
            ),
            "stage_docs": st.column_config.NumberColumn("Stage Docs", format="%d"),
            "beta_docs": st.column_config.NumberColumn("Beta Docs", format="%d"),
            "stage_index_size": st.column_config.TextColumn("Stage Size"),
            "beta_index_size": st.column_config.TextColumn("Beta Size"),
            "stage_avg_log_size": st.column_config.TextColumn("Stage Avg Log"),
            "beta_avg_log_size": st.column_config.TextColumn("Beta Avg Log"),
        },
    )

    st.markdown("##### Project status (from comparison report)")
    st.caption(
        "Includes resolve errors, Beta prefix aliases, ECS scores, schema drift, "
        "and Stage↔Beta missing-field / type-mismatch counts."
    )
    st.dataframe(
        status_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "has_schema_drift": st.column_config.CheckboxColumn("Schema Drift"),
            "has_type_mismatch": st.column_config.CheckboxColumn("Type Mismatch"),
            "stage_ecs_ready": st.column_config.CheckboxColumn("Stage ECS Ready"),
            "beta_ecs_ready": st.column_config.CheckboxColumn("Beta ECS Ready"),
            "error": st.column_config.TextColumn("Error", width="large"),
        },
    )

# ---------------------------------------------------------------------------
# Tab — Inventory
# ---------------------------------------------------------------------------
with tab_inventory:
    if field_counts_df is None or field_counts_df.empty:
        st.warning(
            "`index_field_counts.csv` is missing for this run. "
            "Re-run `compare_es_mappings.py` to generate it."
        )
    else:
        st.markdown("##### Stage / Beta index inventory")
        st.caption(
            "Resolved daily indexes, field counts, docs, store size, avg log size, "
            "Beta prefix alias, and Beta resolve status."
        )
        st.dataframe(
            field_counts_df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "service": st.column_config.TextColumn("Service", width="medium"),
                "team": st.column_config.TextColumn("Team", width="small"),
                "log_format": st.column_config.TextColumn("Log Format", width="large"),
                "stage_index": st.column_config.TextColumn("Stage Index", width="medium"),
                "beta_index": st.column_config.TextColumn("Beta Index", width="medium"),
                "beta_prefix": st.column_config.TextColumn("Beta Prefix"),
                "beta_status": st.column_config.TextColumn("Beta Status"),
                "stage_fields": st.column_config.NumberColumn("Stage Fields", format="%d"),
                "beta_fields": st.column_config.NumberColumn("Beta Fields", format="%d"),
                "stage_docs": st.column_config.NumberColumn("Stage Docs", format="%d"),
                "beta_docs": st.column_config.NumberColumn("Beta Docs", format="%d"),
            },
        )

        if "beta_status" in field_counts_df.columns:
            missing_beta = field_counts_df[
                field_counts_df["beta_status"].astype(str).str.lower().eq("missing")
            ]
            st.markdown(
                f"**Beta missing:** {len(missing_beta)} / {len(field_counts_df)} services"
            )
            if not missing_beta.empty:
                st.dataframe(
                    missing_beta[["service", "stage_index", "stage_fields", "stage_docs"]],
                    use_container_width=True,
                    hide_index=True,
                )

# ---------------------------------------------------------------------------
# Tab — Schema
# ---------------------------------------------------------------------------
with tab_schema:
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
        stage_fields = entry.get("stage_fields") or {}
        beta_fields = entry.get("beta_fields") or {}
        cmp = entry.get("comparison") or {}
        stage_ecs = ((entry.get("ecs") or {}).get("stage") or {})
        beta_ecs = ((entry.get("ecs") or {}).get("beta") or {})

        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Stage Index", entry.get("stage_index") or "MISSING")
        m2.metric("Beta Index", entry.get("beta_index") or "MISSING")
        m3.metric("Beta Prefix", entry.get("beta_prefix") or "—")
        m4.metric("Total Fields", len(set(stage_fields) | set(beta_fields)))
        m5.metric(
            "Status",
            entry.get("status") or "—",
        )

        if not entry.get("stage_index"):
            st.warning(
                f"Stage index missing for `{selected}` on `{selected_day}` "
                f"(`*-{day_suffix}`). Showing Beta-only mapping if present."
            )

        if entry.get("error"):
            st.error(entry["error"])

        f1, f2, f3, f4 = st.columns(4)
        f1.metric("Schema Drift", "Yes" if entry.get("has_schema_drift") else "No")
        f2.metric("Type Mismatch", "Yes" if entry.get("has_type_mismatch") else "No")
        f3.metric(
            "Stage ECS",
            (
                f"{stage_ecs.get('ecs_fields_present', 0)}/"
                f"{stage_ecs.get('ecs_fields_total', 5)}"
                if stage_ecs
                else "—"
            ),
        )
        f4.metric(
            "Beta ECS",
            (
                f"{beta_ecs.get('ecs_fields_present', 0)}/"
                f"{beta_ecs.get('ecs_fields_total', 5)}"
                if beta_ecs
                else "—"
            ),
        )

        s1, s2 = st.columns(2)
        s1.info(f"**Stage size:** {_size_label(entry.get('stage_size'))}")
        s2.info(f"**Beta size:** {_size_label(entry.get('beta_size'))}")

        st.markdown("##### ECS checklist")
        st.caption(
            "Core ECS fields vs legacy alternatives found in the mapping "
            "(legacy names do not raise the ECS score until remapped)."
        )
        ecs_left, ecs_right = st.columns(2)
        with ecs_left:
            st.markdown(
                f"**Stage** — ready: `{bool(stage_ecs.get('ecs_ready'))}`"
            )
            st.dataframe(
                _ecs_checklist_df(stage_ecs),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "present": st.column_config.CheckboxColumn("Present"),
                    "legacy_alternatives": st.column_config.TextColumn(
                        "Legacy Alternatives", width="large"
                    ),
                },
            )
        with ecs_right:
            st.markdown(
                f"**Beta** — ready: `{bool(beta_ecs.get('ecs_ready')) if beta_ecs else False}`"
            )
            st.dataframe(
                _ecs_checklist_df(beta_ecs),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "present": st.column_config.CheckboxColumn("Present"),
                    "legacy_alternatives": st.column_config.TextColumn(
                        "Legacy Alternatives", width="large"
                    ),
                },
            )

        st.markdown("##### Stage ↔ Beta field diff")
        d1, d2, d3 = st.columns(3)
        with d1:
            _render_list_table(
                "Missing in Stage (present in Beta)",
                list(cmp.get("missing_in_stage") or []),
                "None",
            )
        with d2:
            _render_list_table(
                "Missing in Beta (present in Stage)",
                list(cmp.get("missing_in_beta") or []),
                "None",
            )
        with d3:
            mismatches = list(cmp.get("type_mismatches") or [])
            st.markdown(f"**Type mismatches** ({len(mismatches)})")
            if not mismatches:
                st.caption("None")
            else:
                st.dataframe(
                    pd.DataFrame(mismatches),
                    use_container_width=True,
                    hide_index=True,
                )

        left, right = st.columns(2)
        with left:
            st.subheader("Stage mapping")
            st.caption(f"{len(stage_fields)} mapped fields")
            with st.expander("View Full YAML Mapping", expanded=False):
                st.code(_fields_to_yaml(stage_fields), language="yaml")
        with right:
            st.subheader("Beta mapping")
            st.caption(f"{len(beta_fields)} mapped fields")
            with st.expander("View Full YAML Mapping", expanded=False):
                st.code(_fields_to_yaml(beta_fields), language="yaml")

# ---------------------------------------------------------------------------
# Tab — Fields
# ---------------------------------------------------------------------------
with tab_fields:
    st.markdown("##### All mapped fields")
    st.caption(
        "Full `all_index_mappings.csv` inventory — filter by project, ECS, "
        "type mismatch, or presence in both environments."
    )

    fc1, fc2, fc3, fc4 = st.columns(4)
    with fc1:
        project_filter = st.multiselect(
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
    with fc3:
        mismatch_filter = st.selectbox(
            "Type mismatch",
            options=["All", "Mismatches only", "No mismatches"],
            index=0,
        )
    with fc4:
        both_filter = st.selectbox(
            "Environment presence",
            options=["All", "In both envs", "Missing in one env"],
            index=0,
        )

    search = st.text_input("Search field name", placeholder="e.g. Properties.elapsed")

    view = mappings_df
    if project_filter:
        view = view[view["project_prefix"].isin(project_filter)]
    if ecs_filter == "ECS only":
        view = view[view["is_ecs_standard"]]
    elif ecs_filter == "Non-ECS only":
        view = view[~view["is_ecs_standard"]]
    if mismatch_filter == "Mismatches only":
        view = view[view["is_type_mismatch"]]
    elif mismatch_filter == "No mismatches":
        view = view[~view["is_type_mismatch"]]
    if both_filter == "In both envs":
        view = view[view["exists_in_both_envs"]]
    elif both_filter == "Missing in one env":
        view = view[~view["exists_in_both_envs"]]
    if search.strip():
        view = view[
            view["field_name"].astype(str).str.contains(search.strip(), case=False, na=False)
        ]

    st.caption(f"Showing {len(view):,} / {len(mappings_df):,} field rows")
    st.dataframe(
        view,
        use_container_width=True,
        hide_index=True,
        column_config={
            "project_prefix": st.column_config.TextColumn("Project", width="medium"),
            "field_name": st.column_config.TextColumn("Field", width="large"),
            "beta_resolved_index": st.column_config.TextColumn("Beta Index"),
            "stage_resolved_index": st.column_config.TextColumn("Stage Index"),
            "beta_data_type": st.column_config.TextColumn("Beta Type", width="small"),
            "stage_data_type": st.column_config.TextColumn("Stage Type", width="small"),
            "is_ecs_standard": st.column_config.CheckboxColumn("ECS"),
            "is_type_mismatch": st.column_config.CheckboxColumn("Type Mismatch"),
            "exists_in_both_envs": st.column_config.CheckboxColumn("In Both Envs"),
        },
    )

# ---------------------------------------------------------------------------
# Tab — Conflicts
# ---------------------------------------------------------------------------
with tab_conflicts:
    conflicts_df = mappings_df[
        mappings_df["is_type_mismatch"] | ~mappings_df["exists_in_both_envs"]
    ].copy()
    type_only = conflicts_df[conflicts_df["is_type_mismatch"]]
    missing_env = conflicts_df[~conflicts_df["exists_in_both_envs"]]

    st.markdown(
        f'<div class="conflict-banner">'
        f"⚠️ High-priority schema conflicts: "
        f"<strong>{len(conflicts_df):,}</strong> field rows "
        f"({len(type_only):,} type mismatches · "
        f"{len(missing_env):,} missing across environments)"
        f"</div>",
        unsafe_allow_html=True,
    )

    # Structured mismatches from JSON (cleaner than CSV for blockers)
    structured_rows = []
    for entry in comparison_data.get("results") or []:
        for item in (entry.get("comparison") or {}).get("type_mismatches") or []:
            structured_rows.append(
                {
                    "project_prefix": entry.get("prefix"),
                    "field": item.get("field"),
                    "stage_type": item.get("stage_type"),
                    "beta_type": item.get("beta_type"),
                    "stage_index": entry.get("stage_index"),
                    "beta_index": entry.get("beta_index"),
                    "beta_prefix": entry.get("beta_prefix"),
                }
            )
    structured_df = pd.DataFrame(structured_rows)

    st.markdown("##### Structured type mismatches (from comparison JSON)")
    if structured_df.empty:
        st.success("No cross-environment type mismatches detected.")
    else:
        st.dataframe(structured_df, use_container_width=True, hide_index=True)

    st.markdown("##### Conflict field inventory (CSV)")
    st.dataframe(
        conflicts_df.sort_values(
            by=["is_type_mismatch", "field_name", "project_prefix"],
            ascending=[False, True, True],
        ),
        use_container_width=True,
        hide_index=True,
        column_config={
            "project_prefix": st.column_config.TextColumn("Project", width="medium"),
            "field_name": st.column_config.TextColumn("Field", width="large"),
            "beta_data_type": st.column_config.TextColumn("Beta Type", width="small"),
            "stage_data_type": st.column_config.TextColumn("Stage Type", width="small"),
            "is_type_mismatch": st.column_config.CheckboxColumn("Type Mismatch"),
            "exists_in_both_envs": st.column_config.CheckboxColumn("In Both Envs"),
            "is_ecs_standard": st.column_config.CheckboxColumn("ECS"),
        },
    )

    st.markdown("##### Blocking fields (grouped by field name)")
    if type_only.empty:
        st.caption("No type-mismatch rows to group.")
    else:
        grouped = (
            type_only.groupby("field_name", as_index=False)
            .agg(
                projects=("project_prefix", lambda s: ", ".join(sorted(set(s)))),
                project_count=("project_prefix", "nunique"),
                beta_types=(
                    "beta_data_type",
                    lambda s: ", ".join(sorted(set(map(str, s)))),
                ),
                stage_types=(
                    "stage_data_type",
                    lambda s: ", ".join(sorted(set(map(str, s)))),
                ),
                mismatch_rows=("field_name", "size"),
            )
            .sort_values(["project_count", "field_name"], ascending=[False, True])
        )
        st.dataframe(grouped, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------------
# Tab — Compare days
# ---------------------------------------------------------------------------
with tab_compare:
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
                "Schema Drift",
                cur_kpis["schema_drift"],
                delta=cur_kpis["schema_drift"] - base_kpis["schema_drift"],
                delta_color="inverse",
            )
            c5.metric(
                "Type Conflicts",
                cur_kpis["type_conflicts"],
                delta=cur_kpis["type_conflicts"] - base_kpis["type_conflicts"],
                delta_color="inverse",
            )

            diff_df = _diff_readiness(base_ready, readiness_df)
            only_changes = st.checkbox(
                "Show only added / removed / changed", value=True
            )
            view = (
                diff_df[diff_df["change"] != "unchanged"] if only_changes else diff_df
            )

            st.markdown("##### Per-project deltas")
            st.dataframe(
                view,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "project_prefix": st.column_config.TextColumn(
                        "Project", width="medium"
                    ),
                    "change": st.column_config.TextColumn("Change", width="small"),
                    "stage_fields_Δ": st.column_config.NumberColumn(
                        "Stage Fields Δ", format="%+d"
                    ),
                    "beta_fields_Δ": st.column_config.NumberColumn(
                        "Beta Fields Δ", format="%+d"
                    ),
                    "ecs_fields_Δ": st.column_config.NumberColumn(
                        "ECS Fields Δ", format="%+d"
                    ),
                    "type_mismatches_Δ": st.column_config.NumberColumn(
                        "Type Mismatches Δ", format="%+d"
                    ),
                    "stage_docs_Δ": st.column_config.NumberColumn(
                        "Stage Docs Δ", format="%+d"
                    ),
                    "beta_docs_Δ": st.column_config.NumberColumn(
                        "Beta Docs Δ", format="%+d"
                    ),
                    "format_old": st.column_config.TextColumn("Format (baseline)"),
                    "format_new": st.column_config.TextColumn("Format (current)"),
                },
            )
