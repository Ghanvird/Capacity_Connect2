from __future__ import annotations
from typing import Dict, List

import dash
import pandas as pd
from dash import dash_table

from plan_store import get_plan
from cap_store import resolve_settings
from ._grain_cols import day_cols_for_weeks
from ._common import _week_span, _canon_scope, _monday, get_plan_meta, _load_ts_with_fallback, _assemble_voice, _assemble_chat, _assemble_ob, _assemble_bo
from ._calc import _voice_interval_calc, _chat_interval_calc, _ob_interval_calc, _daily_from_intervals, _bo_daily_calc, _fill_tables_fixed


# --- UI helper: keep the first return a DataTable (same as Interval filler expects) ---
def _make_upper_table(df: pd.DataFrame, day_cols_meta: List[dict]):
    if not isinstance(df, pd.DataFrame) or df.empty:
        df = pd.DataFrame({"metric": []})
    return dash_table.DataTable(
        id="tbl-upper",
        data=df.to_dict("records"),
        columns=[{"name": "Metric", "id": "metric", "editable": False}] + day_cols_meta,
        editable=False,
        style_as_list_view=True,
        style_table={"overflowX": "auto"},
        style_header={"whiteSpace": "pre"},
    )


# --- derive day columns from data (no _week_span dependency) ---
def _derive_day_ids_from_plan(plan: dict):
    weeks_span = _week_span(plan.get("start_week"), plan.get("end_week"))
    full_cols, day_ids = day_cols_for_weeks(weeks_span)
    # Drop the leading Metric column since _make_upper_table prepends it
    day_cols_meta = [c for c in full_cols if str(c.get("id")) != "metric"]
    return day_cols_meta, day_ids


def _fill_tables_fixed_daily(ptype, pid, _fw_cols_unused, _tick, whatif=None):
    """
    Daily view (Voice/Chat/Outbound):
      - FW: Forecast/Actual/Tactical Volume + AHT/SUT (daily roll-ups from interval streams)
      - Upper: FTE/PHC/Service Level (daily roll-ups from interval streams via Erlang)
    Notes:
      • Uses consolidated_calcs(...) so headers/dates/interval strings are normalized.
      • Vectorized build (no per-column inserts) → no pandas fragmentation warnings.
      • Returns the same 13-item tuple shape your callbacks expect (DataTable + FW dict + empties).
    """
    if not pid:
        raise dash.exceptions.PreventUpdate

    plan = get_plan(pid) or {}
    ch_name = (plan.get("channel") or plan.get("lob") or "").split(",")[0].strip()
    _settings = resolve_settings(ba=plan.get("vertical"),
                                 subba=plan.get("sub_ba"),
                                 lob=ch_name)
    ivl_min = int(float(_settings.get("interval_minutes", 30) or 30))
    # Read per-plan lower FW options (e.g., Backlog toggle)
    try:
        meta = get_plan_meta(pid) or {}
    except Exception:
        meta = {}
    def _meta_list(val):
        if isinstance(val, str):
            import json
            try:
                return list(json.loads(val))
            except Exception:
                return []
        if isinstance(val, (list, tuple)):
            return list(val)
        return []
    lower_opts = set(_meta_list(meta.get("fw_lower_options")))

    # Build scope key and assemble RAW uploads
    ch = ch_name.lower()
    sk = _canon_scope(plan.get("vertical"),
                      plan.get("sub_ba"),
                      plan.get("channel") or plan.get("lob"),
                      plan.get("site") or plan.get("location") or plan.get("country"))

    is_bo = False
    if ch.startswith("voice"):
        dfF = _assemble_voice(sk, "forecast")
        dfA = _assemble_voice(sk, "actual")
        dfT = _assemble_voice(sk, "tactical")
        weight_col_upload = "volume"
        aht_label = "AHT/SUT"
    elif ch.startswith("chat"):
        dfF = _assemble_chat(sk, "forecast")
        dfA = _assemble_chat(sk, "actual")
        dfT = _assemble_chat(sk, "tactical")
        weight_col_upload = "items"
        aht_label = "AHT/SUT"
    elif ch.startswith("back office") or ch in ("bo","backoffice"):
        # Back Office uses daily items + SUT (no interval calc needed)
        dfF = _assemble_bo(sk, "forecast")
        dfA = _assemble_bo(sk, "actual")
        dfT = _assemble_bo(sk, "tactical")
        weight_col_upload = "items"
        aht_label = "SUT"
        is_bo = True
    else:  # outbound
        dfF = _assemble_ob(sk, "forecast")
        dfA = _assemble_ob(sk, "actual")
        dfT = _assemble_ob(sk, "tactical")
        weight_col_upload = "opc"
        aht_label = "AHT/SUT"

    # --- Build day columns from UI-provided FW columns to ensure alignment ---
    # The caller provides `fw_cols` (weekly/day headers). Use those so our data keys align with the grid.
    try:
        fw_cols = list(_fw_cols_unused or [])
    except Exception:
        fw_cols = []
    day_ids = [str(c.get("id")) for c in fw_cols if str(c.get("id")) != "metric"]
    # Upper table expects column metadata without the leading Metric column
    day_cols_meta = [
        {"name": c.get("name"), "id": c.get("id")}
        for c in fw_cols if str(c.get("id")) != "metric"
    ]

    # -------- helpers --------
    def _daily_sum(df: pd.DataFrame, val_col: str) -> dict:
        if df is None or df.empty or "date" not in df.columns or val_col not in df.columns:
            return {}
        d = df.copy()
        d["date"] = pd.to_datetime(d["date"]).dt.date
        d[val_col] = pd.to_numeric(d[val_col], errors="coerce").fillna(0.0)
        g = d.groupby("date", as_index=False)[val_col].sum()
        return {str(k): float(v) for k, v in zip(g["date"], g[val_col])}

    def _daily_weighted_aht(df: pd.DataFrame, wcol: str, aht_col: str = "aht_sec") -> dict:
        if df is None or df.empty or "date" not in df.columns or wcol not in df.columns or aht_col not in df.columns:
            return {}
        d = df.copy()
        d["date"] = pd.to_datetime(d["date"]).dt.date
        d[wcol] = pd.to_numeric(d[wcol], errors="coerce").fillna(0.0)
        d[aht_col] = pd.to_numeric(d[aht_col], errors="coerce").fillna(0.0)
        out = {}
        for dd, grp in d.groupby("date"):
            w = grp[wcol].sum()
            out[str(dd)] = float((grp[wcol] * grp[aht_col]).sum() / w) if w > 0 else 0.0
        return out

    def _daily_weighted_occ(df: pd.DataFrame) -> dict:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return {}
        if not all(c in df.columns for c in ("date","service_level","staff_seconds","occupancy")):
            return {}
        d = df.copy()
        d["date"] = pd.to_datetime(d["date"], errors="coerce").dt.date
        d["staff_seconds"] = pd.to_numeric(d["staff_seconds"], errors="coerce").fillna(0.0)
        d["occupancy"] = pd.to_numeric(d["occupancy"], errors="coerce").fillna(0.0)
        out = {}
        for dd, grp in d.groupby("date"):
            w = grp["staff_seconds"].sum()
            out[str(dd)] = float((grp["staff_seconds"] * grp["occupancy"]).sum() / w) if w > 0 else 0.0
        return out
    
    # Helper: compute per-interval calcs if interval uploads are present
    def _interval_calc(df: pd.DataFrame, channel: str) -> pd.DataFrame:
        if not (isinstance(df, pd.DataFrame) and not df.empty and "interval" in df.columns):
            return pd.DataFrame()
        if channel.startswith("voice"):
            return _voice_interval_calc(df, _settings, ivl_min)
        elif channel.startswith("chat"):
            return _chat_interval_calc(df, _settings, ivl_min)
        elif channel.startswith("back office") or channel in ("bo","backoffice"):
            # BO daily calcs handled separately (TAT/Erlang on daily totals)
            return pd.DataFrame()
        else:
            x = df.copy()
            if "items" not in x.columns:
                if "opc" in x.columns:
                    x = x.rename(columns={"opc": "items"})
                elif "volume" in x.columns:
                    x = x.rename(columns={"volume": "items"})
            return _ob_interval_calc(x, _settings, ivl_min)

    # Compute interval calcs (if present) and then daily rollups for forecast and actual
    ivl_calc_f = _interval_calc(dfF, ch)
    ivl_calc_a = _interval_calc(dfA, ch)
    ivl_calc_t = _interval_calc(dfT, ch)

    # Pick weight column for daily rollup from intervals
    weight_col_ivl = "volume" if ch.startswith("voice") else "items"

    if isinstance(ivl_calc_f, pd.DataFrame) and not ivl_calc_f.empty:
        day_calc_f = _daily_from_intervals(ivl_calc_f, _settings, weight_col_ivl)
    else:
        # Fallback for channels that may upload daily-only data
        day_calc_f = _bo_daily_calc(dfF, _settings) if (isinstance(dfF, pd.DataFrame) and not dfF.empty and not ch.startswith("voice")) else pd.DataFrame()
    if isinstance(ivl_calc_a, pd.DataFrame) and not ivl_calc_a.empty:
        day_calc_a = _daily_from_intervals(ivl_calc_a, _settings, weight_col_ivl)
    else:
        day_calc_a = _bo_daily_calc(dfA, _settings) if (isinstance(dfA, pd.DataFrame) and not dfA.empty and not ch.startswith("voice")) else pd.DataFrame()
    if isinstance(ivl_calc_t, pd.DataFrame) and not ivl_calc_t.empty:
        day_calc_t = _daily_from_intervals(ivl_calc_t, _settings, weight_col_ivl)
    else:
        day_calc_t = pd.DataFrame()

    # Extract metrics into dicts keyed by date
    m_fte_f = (
        {str(pd.to_datetime(r["date"]).date()): float(r.get("fte_req", 0.0))
         for _, r in day_calc_f.iterrows()}
        if isinstance(day_calc_f, pd.DataFrame) and not day_calc_f.empty else {}
    )
    m_fte_a = (
        {str(pd.to_datetime(r["date"]).date()): float(r.get("fte_req", 0.0))
         for _, r in day_calc_a.iterrows()}
        if isinstance(day_calc_a, pd.DataFrame) and not day_calc_a.empty else {}
    )
    m_fte_t = (
        {str(pd.to_datetime(r["date"]).date()): float(r.get("fte_req", 0.0))
         for _, r in day_calc_t.iterrows()}
        if isinstance(day_calc_t, pd.DataFrame) and not day_calc_t.empty else {}
    )
    src_calc = day_calc_f if (isinstance(day_calc_f, pd.DataFrame) and not day_calc_f.empty) else (
        day_calc_a if (isinstance(day_calc_a, pd.DataFrame) and not day_calc_a.empty) else pd.DataFrame()
    )
    def _safe_float0(x) -> float:
        try:
            v = pd.to_numeric(x, errors="coerce")
            if pd.isna(v):
                return 0.0
            return float(v)
        except Exception:
            return 0.0
    m_phc   = (
        {str(pd.to_datetime(r["date"]).date()): _safe_float0(r.get("phc"))
         for _, r in src_calc.iterrows()}
        if isinstance(src_calc, pd.DataFrame) and not src_calc.empty else {}
    )
    m_sl    = (
        {str(pd.to_datetime(r["date"]).date()): _safe_float0(r.get("service_level"))
         for _, r in src_calc.iterrows()}
        if isinstance(src_calc, pd.DataFrame) and not src_calc.empty else {}
    )

    # Projected Supply HC — derive from weekly upper and distribute by workdays
    m_supply = {d: 0.0 for d in day_ids}
    try:
        weeks = _week_span(plan.get("start_week"), plan.get("end_week"))
        weekly_fw_cols = [{"name": "Metric", "id": "metric", "editable": False}] + [{"name": w, "id": w} for w in weeks]
        weekly = _fill_tables_fixed(ptype, pid, weekly_fw_cols, _tick, whatif=whatif, grain='week')
        (upper_wk, *_rest) = weekly
        upper_df_w = pd.DataFrame(getattr(upper_wk, 'data', None) or [])
        sup_w = {}
        if isinstance(upper_df_w, pd.DataFrame) and not upper_df_w.empty and "metric" in upper_df_w.columns:
            row = upper_df_w[upper_df_w["metric"].astype(str).str.strip().eq("Projected Supply HC")]
            if not row.empty:
                for w in weeks:
                    if w in row.columns:
                        try:
                            sup_w[str(pd.to_datetime(w).date())] = float(pd.to_numeric(row[w], errors='coerce').fillna(0.0).iloc[0])
                        except Exception:
                            sup_w[str(pd.to_datetime(w).date())] = 0.0
        # Distribute per day in the week
        workdays = int(float(_settings.get("workdays_per_week", 7) or 7))
        for d in day_ids:
            w = str(_monday(d))
            m_supply[d] = float(sup_w.get(w, 0.0)) / max(1, workdays)
    except Exception:
        pass

    # For Back Office (daily/TAT), derive PHC and a proxy Service Level from supply and SUT
    if is_bo:
        try:
            hrs = float(_settings.get("bo_hours_per_day", _settings.get("hours_per_fte", 8.0)) or 8.0)
            shrink = float(_settings.get("bo_shrinkage_pct", _settings.get("shrinkage_pct", 0.30)) or 0.30)
            util = float(_settings.get("util_bo", 0.85) or 0.85)
            productive_sec = hrs * 3600.0 * max(1e-6, (1.0 - shrink)) * util
        except Exception:
            productive_sec = 8.0 * 3600.0 * 0.7

        # pick daily SUT from forecast, else actual, else settings default
        def _sut_for_day(d):
            try:
                v = ahtF.get(d, None)
                if v is None or pd.isna(v) or float(v) <= 0:
                    v = ahtA.get(d, None)
                if v is None or pd.isna(v) or float(v) <= 0:
                    v = float(_settings.get("target_sut", _settings.get("budgeted_sut", 600)) or 600.0)
                return float(v)
            except Exception:
                return float(_settings.get("target_sut", _settings.get("budgeted_sut", 600)) or 600.0)

        # Recompute PHC (projected handling capacity) from projected supply
        # PHC = Supply FTE (per-day) * productive seconds per day / SUT_seconds
        for d in day_ids:
            sut = max(1e-6, _sut_for_day(d))
            sup_fte = float(m_supply.get(d, 0.0) or 0.0)
            m_phc[d] = float((sup_fte * productive_sec) / sut)

        # Proxy Service Level as Supply/Required (capped to 100%)
        # Required FTE from forecast first, else actual
        for d in day_ids:
            req = float(m_fte_f.get(d, m_fte_a.get(d, 0.0)) or 0.0)
            sup = float(m_supply.get(d, 0.0) or 0.0)
            if req <= 0:
                m_sl[d] = 100.0 if sup > 0 else 0.0
            else:
                m_sl[d] = float(min(100.0, max(0.0, (sup / req) * 100.0)))

    # Compute variance rows (MTP≈Forecast, Tactical, Budgeted)
    # Budgeted FTE via budget AHT applied to Forecast intervals when possible
    m_fte_b = {}
    try:
        dfB = dfF.copy() if isinstance(dfF, pd.DataFrame) and not dfF.empty else pd.DataFrame()
        if not dfB.empty and "date" in dfB.columns:
            dfB["date"] = pd.to_datetime(dfB["date"], errors="coerce").dt.date.astype(str)
            dfB["aht_sec"] = dfB["date"].map(lambda s: _budget_for_day(s))
            ivl_b = _interval_calc(dfB, ch)
            if isinstance(ivl_b, pd.DataFrame) and not ivl_b.empty:
                day_b = _daily_from_intervals(ivl_b, _settings, weight_col_ivl)
                if isinstance(day_b, pd.DataFrame) and not day_b.empty:
                    m_fte_b = {str(pd.to_datetime(r["date"]).date()): float(r.get("fte_req", 0.0)) for _, r in day_b.iterrows()}
    except Exception:
        m_fte_b = {}

    var_mtp = [ (m_fte_f.get(c, 0.0) - m_fte_a.get(c, 0.0)) for c in day_ids ]
    var_tac = [ (m_fte_t.get(c, 0.0) - m_fte_a.get(c, 0.0)) for c in day_ids ]
    var_bud = [ (m_fte_b.get(c, 0.0) - m_fte_a.get(c, 0.0)) for c in day_ids ]

    # Build the upper DataFrame
    upper_df = pd.DataFrame.from_dict(
        {
            "FTE Required @ Forecast Volume":   [m_fte_f.get(c, 0.0) for c in day_ids],
            "FTE Required @ Actual Volume":     [m_fte_a.get(c, 0.0) for c in day_ids],
            "FTE Over/Under MTP Vs Actual":     var_mtp,
            "FTE Over/Under Tactical Vs Actual":var_tac,
            "FTE Over/Under Budgeted Vs Actual":var_bud,
            "Projected Supply HC":              [m_supply.get(c, 0.0) for c in day_ids],
            "Projected Handling Capacity (#)":  [m_phc.get(c, 0.0) for c in day_ids],
            "Projected Service Level":          [m_sl.get(c, 0.0) for c in day_ids],
        },
        orient="index", columns=day_ids,
    ).reset_index().rename(columns={"index":"metric"}).fillna(0.0)

    # Round to 1 decimal place for display
    def _round1(df: pd.DataFrame) -> pd.DataFrame:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return df if isinstance(df, pd.DataFrame) else pd.DataFrame()
        out = df.copy()
        for c in day_ids:
            if c in out.columns:
                out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0.0).round(1)
        return out

    upper_df = _round1(upper_df[["metric"] + day_ids])
    # Add % sign to percentage metrics (1 decimal)
    try:
        msk = upper_df["metric"].astype(str).str.strip().eq("Projected Service Level")
        if msk.any():
            # ensure object dtype before assigning string values
            for c in day_ids:
                if c in upper_df.columns:
                    upper_df[c] = upper_df[c].astype(object)
            for c in day_ids:
                if c in upper_df.columns:
                    try:
                        v = float(pd.to_numeric(upper_df.loc[msk, c], errors="coerce").fillna(0.0).iloc[0])
                    except Exception:
                        v = 0.0
                    upper_df.loc[msk, c] = f"{v:.1f}%"
    except Exception:
        pass
    upper_tbl = _make_upper_table(upper_df, day_cols_meta)

        # -------- FW (Forecast/Tactical/Actual Volume + AHT/SUT + Occupancy/Budget) --------
    # Daily roll-ups
    volF = _daily_sum(dfF, weight_col_upload)
    volA = _daily_sum(dfA, weight_col_upload)
    volT = _daily_sum(dfT, weight_col_upload)

    ahtF = _daily_weighted_aht(dfF, weight_col_upload)
    ahtA = _daily_weighted_aht(dfA, weight_col_upload)
    ahtT = _daily_weighted_aht(dfT, weight_col_upload)   # include tactical AHT

    # Occupancy from per-interval forecast calcs (weighted by staff_seconds)
    occ_src = ivl_calc_f if (isinstance(ivl_calc_f, pd.DataFrame) and not ivl_calc_f.empty) else ivl_calc_a
    occF = _daily_weighted_occ(occ_src)
    # Override future dates with settings occupancy (channel-specific)
    def _occ_setting_frac(settings: dict, channel: str) -> float:
        try:
            ch = (channel or '').strip().lower()
            if ch.startswith('voice'):
                base = settings.get('occupancy_cap_voice', settings.get('occupancy', 0.85))
            elif ch.startswith('chat'):
                base = settings.get('util_chat', settings.get('util_bo', 0.85))
            else:
                base = settings.get('util_ob', settings.get('occupancy', 0.85))
            v = float(base if base is not None else 0.85)
            if v > 1.0:
                v = v/100.0
            return max(0.0, min(1.0, v))
        except Exception:
            return 0.85
    occ_setting = _occ_setting_frac(_settings, ch)
    today = pd.Timestamp('today').date()
    for d in list(day_ids):
        try:
            dd = pd.to_datetime(d).date()
            if dd > today:
                occF[str(d)] = float(occ_setting)
        except Exception:
            pass

    # Budgeted AHT/SUT per week → per day
    def _ts_week_dict(df: pd.DataFrame, val_candidates: list[str]) -> dict:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return {}
        d = df.copy()
        if "week" in d.columns:
            d["week"] = pd.to_datetime(d["week"], errors="coerce").dt.date.astype(str)
        elif "date" in d.columns:
            d["week"] = pd.to_datetime(d["date"], errors="coerce").dt.date.astype(str)
        else:
            return {}
        low = {c.lower(): c for c in d.columns}
        vcol = None
        for c in val_candidates:
            vcol = low.get(c.lower())
            if vcol:
                break
        if not vcol:
            return {}
        d[vcol] = pd.to_numeric(d[vcol], errors="coerce")
        return d.dropna(subset=["week", vcol]).set_index("week")[vcol].astype(float).to_dict()

    if ch.startswith("voice"):
        planned_df = _load_ts_with_fallback("voice_planned_aht", sk)
        if (not isinstance(planned_df, pd.DataFrame)) or planned_df.empty:
            tmp = _load_ts_with_fallback("voice_budget", sk)
            if isinstance(tmp, pd.DataFrame) and not tmp.empty and "budget_aht_sec" in tmp.columns:
                planned_df = tmp.rename(columns={"budget_aht_sec": "aht_sec"})[
                    [c for c in tmp.columns if c in ("date","week","aht_sec")]
                ]
        wk_budget = _ts_week_dict(planned_df, ["aht_sec", "aht", "avg_aht"]) if isinstance(planned_df, pd.DataFrame) else {}
    else:
        planned_df = _load_ts_with_fallback("bo_planned_sut", sk)
        if (not isinstance(planned_df, pd.DataFrame)) or planned_df.empty:
            tmp = _load_ts_with_fallback("bo_budget", sk)
            if isinstance(tmp, pd.DataFrame) and not tmp.empty and "budget_sut_sec" in tmp.columns:
                planned_df = tmp.rename(columns={"budget_sut_sec": "sut_sec"})[
                    [c for c in tmp.columns if c in ("date","week","sut_sec")]
                ]
        wk_budget = _ts_week_dict(planned_df, ["sut_sec", "aht_sec", "sut", "avg_sut"]) if isinstance(planned_df, pd.DataFrame) else {}

    def _budget_for_day(d: str) -> float:
        try:
            w = str(_monday(d))
            return float(wk_budget.get(w, 0.0))
        except Exception:
            return 0.0

    # Build FW DataFrame (conditionally include Backlog)
    fw_data = {
        "Forecast Volume":       [volF.get(c, 0.0) for c in day_ids],
        "Tactical Volume":       [volT.get(c, 0.0) for c in day_ids],
        "Actual Volume":         [volA.get(c, 0.0) for c in day_ids],
        "Budgeted AHT/SUT":      [_budget_for_day(c) for c in day_ids],
        f"Forecast {aht_label}": [ahtF.get(c, 0.0) for c in day_ids],
        f"Tactical {aht_label}": [ahtT.get(c, 0.0) for c in day_ids],
        f"Actual {aht_label}":   [ahtA.get(c, 0.0) for c in day_ids],
        "Occupancy":             [occF.get(c, 0.0) for c in day_ids],
        "Overtime Hours (#)":    [0.0 for _ in day_ids],
    }

    # Include Backlog only if plan options selected
    if "backlog" in lower_opts:
        backlog_vals = [max(0.0, float(volA.get(c, 0.0)) - float(volF.get(c, 0.0))) for c in day_ids]
        fw_data["Backlog (Items)"] = backlog_vals

    fw_df = pd.DataFrame.from_dict(fw_data, orient="index", columns=day_ids) \
                        .reset_index().rename(columns={"index":"metric"}).fillna(0.0)
    fw_df = _round1(fw_df[["metric"] + day_ids])
    # Occupancy is a fraction → format as percentage string with 1 decimal and %
    try:
        msk = fw_df["metric"].astype(str).str.strip().eq("Occupancy")
        if msk.any():
            # ensure object dtype before assigning string values
            for c in day_ids:
                if c in fw_df.columns:
                    fw_df[c] = fw_df[c].astype(object)
            for c in day_ids:
                pct = float(occF.get(c, 0.0) * 100.0)
                fw_df.loc[msk, c] = f"{pct:.1f}%"
    except Exception:
        pass

    # -------- Return 13-item tuple --------
    empty = []
    return (
        upper_tbl,
        fw_df.to_dict("records"),
        empty, empty, empty, empty, empty, empty, empty, empty,
        empty, empty, empty,
    )
