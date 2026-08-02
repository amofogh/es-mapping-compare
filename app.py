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
        max-width: 1400px;
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

    .stAlert {
        border-radius: 10px;
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

    # Pre-dated layout: artifacts directly under results/
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


@st.cache_data(show_spinner="Loading migration reports…")
def load_data(run_id: str) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    run_dir = _run_path(run_id)
    readiness_csv = run_dir / "central_format_readiness.csv"
    mappings_csv = run_dir / "all_index_mappings.csv"
    comparison_json = run_dir / "mapping_comparison.json"

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

    readiness = pd.read_csv(readiness_csv)
    mappings = pd.read_csv(mappings_csv)
    with comparison_json.open(encoding="utf-8") as fh:
        comparison = json.load(fh)

    for col in ("can_centralize_as_is",):
        if col in readiness.columns:
            readiness[col] = (
                readiness[col]
                .astype(str)
                .str.strip()
                .str.upper()
                .isin({"TRUE", "1", "YES"})
            )

    for col in ("is_type_mismatch", "exists_in_both_envs", "is_ecs_standard"):
        if col in mappings.columns:
            mappings[col] = (
                mappings[col]
                .astype(str)
                .str.strip()
                .str.upper()
                .isin({"TRUE", "1", "YES"})
            )

    return readiness, mappings, comparison


def _prefix_lookup(comparison: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        entry.get("prefix"): entry
        for entry in comparison.get("results", [])
        if entry.get("prefix")
    }


def _fields_to_yaml(fields: dict[str, Any] | None) -> str:
    payload = fields or {}
    return yaml.dump(payload, sort_keys=True, default_flow_style=False, allow_unicode=True)


def _kpi(readiness: pd.DataFrame) -> dict[str, int]:
    return {
        "total_projects": len(readiness),
        "ecs_ready": int((readiness["ecs_compliant_fields_count"] >= 5).sum()),
        "legacy_workers": int(
            readiness["target_log_format"]
            .astype(str)
            .str.contains("Format 3", na=False)
            .sum()
        ),
        "type_conflicts": int(readiness["type_mismatches_count"].fillna(0).sum()),
    }


def _diff_readiness(baseline: pd.DataFrame, current: pd.DataFrame) -> pd.DataFrame:
    """Per-project delta between two readiness snapshots."""
    keys = ["project_prefix"]
    cols = [
        "total_fields_stage",
        "total_fields_beta",
        "ecs_compliant_fields_count",
        "type_mismatches_count",
        "target_log_format",
        "can_centralize_as_is",
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
    readiness_df, mappings_df, comparison_data = load_data(selected_run)
except FileNotFoundError as exc:
    st.error(f"⚠️ {exc}")
    st.stop()
except Exception as exc:  # noqa: BLE001 — surface load errors cleanly in UI
    st.error(f"⚠️ Failed to load migration data: {exc}")
    st.stop()

prefix_index = _prefix_lookup(comparison_data)
kpis = _kpi(readiness_df)

st.title("🔎 EFK Migration & Schema Analyzer")
st.caption(
    f"Run `{selected_run}` · Generated {comparison_data.get('generated_at', '—')} · "
    f"Mode: `{comparison_data.get('mode', '—')}`"
)

tabs = st.tabs(
    [
        "🚀 Readiness Dashboard",
        "🗂️ Schema Inspector",
        "⚠️ Conflict Resolution",
        "📉 Run Comparison",
    ]
)
tab_readiness, tab_schema, tab_conflicts, tab_compare = tabs

# ---------------------------------------------------------------------------
# Tab 1 — Readiness Dashboard
# ---------------------------------------------------------------------------
with tab_readiness:
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Total Projects", kpis["total_projects"])
    k2.metric("ECS Ready", kpis["ecs_ready"])
    k3.metric("Legacy Workers (Format 3)", kpis["legacy_workers"])
    k4.metric("Type Conflicts", kpis["type_conflicts"])

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
            "target_log_format": st.column_config.SelectboxColumn(
                "Target Log Format",
                help="Fluentd central log format archetype",
                width="large",
                options=sorted(
                    readiness_df["target_log_format"].dropna().astype(str).unique()
                ),
                required=True,
            ),
            "ecs_compliant_fields_count": st.column_config.ProgressColumn(
                "ECS Fields (0–5)",
                help="Count of core ECS fields present in Stage mapping",
                min_value=0,
                max_value=5,
                format="%d",
            ),
            "can_centralize_as_is": st.column_config.CheckboxColumn(
                "Can Centralize As-Is",
                help="Ready for central format without heavy mutate",
                width="small",
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
        },
    )

# ---------------------------------------------------------------------------
# Tab 2 — Schema Inspector (lag-free)
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
    )

    if not selected:
        st.info("No project prefixes available in the loaded report.")
    else:
        entry = prefix_index.get(selected, {})
        stage_fields = entry.get("stage_fields") or {}
        beta_fields = entry.get("beta_fields") or {}
        stage_index = entry.get("stage_index") or "—"
        beta_index = entry.get("beta_index") or "—"
        total_fields = len(set(stage_fields) | set(beta_fields))

        m1, m2, m3 = st.columns(3)
        m1.metric("Beta Index Name", beta_index if beta_index else "—")
        m2.metric("Stage Index Name", stage_index if stage_index else "—")
        m3.metric("Total Fields", total_fields)

        status = entry.get("status")
        if status:
            st.caption(f"Compare status: `{status}`")

        left, right = st.columns(2)

        with left:
            st.subheader("Stage")
            st.caption(f"{len(stage_fields)} mapped fields")
            with st.expander("View Full YAML Mapping", expanded=False):
                st.code(_fields_to_yaml(stage_fields), language="yaml")

        with right:
            st.subheader("Beta")
            st.caption(f"{len(beta_fields)} mapped fields")
            with st.expander("View Full YAML Mapping", expanded=False):
                st.code(_fields_to_yaml(beta_fields), language="yaml")

# ---------------------------------------------------------------------------
# Tab 3 — Conflict Resolution
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

    st.markdown("##### Conflict field inventory")
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
            "is_type_mismatch": st.column_config.CheckboxColumn(
                "Type Mismatch", width="small"
            ),
            "exists_in_both_envs": st.column_config.CheckboxColumn(
                "In Both Envs", width="small"
            ),
            "is_ecs_standard": st.column_config.CheckboxColumn("ECS", width="small"),
        },
    )

    st.markdown("##### Blocking fields (grouped)")
    st.caption(
        "Fields that block a shared central log format — especially conflicting "
        "types such as `long` vs `text` across projects."
    )

    if type_only.empty:
        st.success("No cross-environment type mismatches detected.")
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

        st.dataframe(
            grouped,
            use_container_width=True,
            hide_index=True,
            column_config={
                "field_name": st.column_config.TextColumn("Field Name", width="large"),
                "projects": st.column_config.TextColumn("Projects", width="large"),
                "project_count": st.column_config.NumberColumn(
                    "Projects Affected", format="%d"
                ),
                "beta_types": st.column_config.TextColumn("Beta Type(s)"),
                "stage_types": st.column_config.TextColumn("Stage Type(s)"),
                "mismatch_rows": st.column_config.NumberColumn("Rows", format="%d"),
            },
        )

# ---------------------------------------------------------------------------
# Tab 4 — Run Comparison
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
            base_ready, _, base_meta = load_data(baseline_run)
        except Exception as exc:  # noqa: BLE001
            st.error(f"⚠️ Failed to load baseline run `{baseline_run}`: {exc}")
            st.stop()

        base_kpis = _kpi(base_ready)
        cur_kpis = kpis

        st.markdown(
            f"**Baseline** `{baseline_run}` "
            f"({base_meta.get('generated_at', '—')}) → "
            f"**Current** `{selected_run}` "
            f"({comparison_data.get('generated_at', '—')})"
        )

        c1, c2, c3, c4 = st.columns(4)
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
            "Legacy Workers",
            cur_kpis["legacy_workers"],
            delta=cur_kpis["legacy_workers"] - base_kpis["legacy_workers"],
        )
        c4.metric(
            "Type Conflicts",
            cur_kpis["type_conflicts"],
            delta=cur_kpis["type_conflicts"] - base_kpis["type_conflicts"],
            delta_color="inverse",
        )

        diff_df = _diff_readiness(base_ready, readiness_df)
        only_changes = st.checkbox("Show only added / removed / changed", value=True)
        view = (
            diff_df[diff_df["change"] != "unchanged"] if only_changes else diff_df
        )

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
                "format_old": st.column_config.TextColumn("Format (baseline)"),
                "format_new": st.column_config.TextColumn("Format (current)"),
            },
        )
