"""EFK Migration & Schema Analyzer — Streamlit dashboard."""

from __future__ import annotations

import json
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
OPTIONAL_FILES = ("index_field_counts.csv",)

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
    }

    div[data-testid="stMetric"] label {
        color: #94a3b8 !important;
    }

    div[data-testid="stMetric"] [data-testid="stMetricValue"] {
        color: #f8fafc !important;
        font-weight: 700;
    }

    .stTabs [data-baseweb="tab-list"] {
        gap: 0.5rem;
        border-bottom: 1px solid #334155;
    }

    .stTabs [data-baseweb="tab"] {
        border-radius: 8px 8px 0 0;
        padding: 0.6rem 1.1rem;
        font-weight: 600;
    }

    .stAlert { border-radius: 10px; }

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

    .ecs-pill {
        display: inline-block;
        padding: 0.15rem 0.55rem;
        border-radius: 999px;
        font-size: 0.8rem;
        font-weight: 600;
        margin-right: 0.35rem;
    }
    .ecs-ok { background: #14532d; color: #bbf7d0; }
    .ecs-miss { background: #7f1d1d; color: #fecaca; }
    .ecs-legacy { background: #713f12; color: #fde68a; }
    </style>
    """,
    unsafe_allow_html=True,
)


def _is_complete_run(path: Path) -> bool:
    return path.is_dir() and all((path / name).is_file() for name in REQUIRED_FILES)


@st.cache_data(show_spinner=False)
def discover_runs(results_root: str) -> list[str]:
    """Return run folder names newest-first (plus legacy flat root as ``legacy``)."""
    root = Path(results_root)
    if not root.exists():
        return []

    runs: list[tuple[float, str]] = []
    for child in root.iterdir():
        if child.name in {"latest", "index_mappings"}:
            continue
        if child.is_symlink():
            continue
        if _is_complete_run(child):
            try:
                mtime = (child / "mapping_comparison.json").stat().st_mtime
            except OSError:
                mtime = child.stat().st_mtime
            runs.append((mtime, child.name))

    if all((root / name).is_file() for name in REQUIRED_FILES):
        try:
            mtime = (root / "mapping_comparison.json").stat().st_mtime
        except OSError:
            mtime = 0.0
        runs.append((mtime, "legacy"))

    runs.sort(key=lambda item: item[0], reverse=True)
    return [name for _, name in runs]


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
# Bootstrap
# ---------------------------------------------------------------------------
available_runs = discover_runs(str(RESULTS_ROOT))
if not available_runs:
    st.error(
        f"⚠️ No dated result runs found under `{RESULTS_ROOT}`. "
        "Run `python compare_es_mappings.py` first."
    )
    st.stop()

latest_link = RESULTS_ROOT / "latest"
default_run = available_runs[0]
if latest_link.is_symlink() or latest_link.is_dir():
    try:
        resolved = latest_link.resolve().name
        if resolved in available_runs:
            default_run = resolved
    except OSError:
        pass

pick_col, compare_col = st.columns([2, 2])
with pick_col:
    selected_run = st.selectbox(
        "📅 Result run",
        options=available_runs,
        index=available_runs.index(default_run),
        help="Each compare execution writes results/<YYYY-MM-DD_HHMMSS>/",
    )
with compare_col:
    compare_options = ["(none)"] + [r for r in available_runs if r != selected_run]
    baseline_run = st.selectbox(
        "Compare against (baseline)",
        options=compare_options,
        index=0,
        help="Pick an older run to diff field counts, ECS score, and conflicts.",
    )

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

prefix_index = _prefix_lookup(comparison_data)
kpis = _kpi(readiness_df, comparison_data)
status_df = _project_status_table(comparison_data)

st.title("🔎 EFK Migration & Schema Analyzer")
st.caption(
    f"Run `{selected_run}` · Generated {comparison_data.get('generated_at', '—')} · "
    f"Mode: `{comparison_data.get('mode', '—')}` · "
    f"Beta enabled: `{comparison_data.get('enable_beta', False)}`"
)

tabs = st.tabs(
    [
        "🚀 Readiness Dashboard",
        "📦 Index Inventory",
        "🗂️ Schema Inspector",
        "📋 Field Browser",
        "⚠️ Conflict Resolution",
        "📉 Run Comparison",
    ]
)
(
    tab_readiness,
    tab_inventory,
    tab_schema,
    tab_fields,
    tab_conflicts,
    tab_compare,
) = tabs

# ---------------------------------------------------------------------------
# Tab 1 — Readiness Dashboard
# ---------------------------------------------------------------------------
with tab_readiness:
    r1, r2, r3, r4, r5, r6 = st.columns(6)
    r1.metric("Total Projects", kpis["total_projects"])
    r2.metric("ECS Ready", kpis["ecs_ready"])
    r3.metric("Not ECS Ready", kpis["not_ecs_ready"])
    r4.metric("Legacy Workers (F3)", kpis["legacy_workers"])
    r5.metric("Schema Drift Projects", kpis["schema_drift"])
    r6.metric(
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
# Tab 2 — Index Inventory
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
# Tab 3 — Schema Inspector
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
        m1.metric("Stage Index", entry.get("stage_index") or "—")
        m2.metric("Beta Index", entry.get("beta_index") or "—")
        m3.metric("Beta Prefix", entry.get("beta_prefix") or "—")
        m4.metric("Total Fields", len(set(stage_fields) | set(beta_fields)))
        m5.metric(
            "Status",
            entry.get("status") or "—",
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
# Tab 4 — Field Browser
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
# Tab 5 — Conflict Resolution
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
# Tab 6 — Run Comparison
# ---------------------------------------------------------------------------
with tab_compare:
    if baseline_run == "(none)":
        st.info(
            "Select a **baseline** run in the header to diff against the active run. "
            "Each `compare_es_mappings.py` execution writes a new "
            "`results/YYYY-MM-DD_HHMMSS/` folder."
        )
    else:
        try:
            base_ready, _, base_meta, _ = load_data(baseline_run)
        except Exception as exc:  # noqa: BLE001
            st.error(f"⚠️ Failed to load baseline run `{baseline_run}`: {exc}")
            st.stop()

        base_kpis = _kpi(base_ready, base_meta)
        cur_kpis = kpis

        st.markdown(
            f"**Baseline** `{baseline_run}` "
            f"({base_meta.get('generated_at', '—')}) → "
            f"**Current** `{selected_run}` "
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
        only_changes = st.checkbox("Show only added / removed / changed", value=True)
        view = diff_df[diff_df["change"] != "unchanged"] if only_changes else diff_df

        st.markdown("##### Per-project deltas")
        st.dataframe(
            view,
            use_container_width=True,
            hide_index=True,
            column_config={
                "project_prefix": st.column_config.TextColumn("Project", width="medium"),
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
