from __future__ import annotations
import numpy as np
import pandas as pd

GLUCOSE_COL = "Histórico de glicose mg/dL"
TIME_COL = "Carimbo de data/hora do dispositivo"


def read_libre_csv(path):
    """Lê tanto o CSV original do FreeStyle Libre (cabeçalho na 2ª linha) quanto CSVs já limpos."""
    try:
        raw = pd.read_csv(path, skiprows=1)
        if GLUCOSE_COL in raw.columns and TIME_COL in raw.columns:
            d = pd.DataFrame({
                "timestamp": pd.to_datetime(raw[TIME_COL], dayfirst=True, errors="coerce"),
                "glucose": pd.to_numeric(raw[GLUCOSE_COL], errors="coerce"),
            }).dropna()
            d = d[(d.glucose >= 20) & (d.glucose <= 600)].sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
            return d, raw
    except Exception:
        pass
    raw = pd.read_csv(path)
    if "timestamp" in raw.columns and "glucose" in raw.columns:
        d = pd.DataFrame({
            "timestamp": pd.to_datetime(raw["timestamp"], errors="coerce"),
            "glucose": pd.to_numeric(raw["glucose"], errors="coerce"),
        }).dropna()
        d = d[(d.glucose >= 20) & (d.glucose <= 600)].sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
        return d, raw
    if GLUCOSE_COL in raw.columns and TIME_COL in raw.columns:
        d = pd.DataFrame({
            "timestamp": pd.to_datetime(raw[TIME_COL], dayfirst=True, errors="coerce"),
            "glucose": pd.to_numeric(raw[GLUCOSE_COL], errors="coerce"),
        }).dropna()
        d = d[(d.glucose >= 20) & (d.glucose <= 600)].sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
        return d, raw
    raise ValueError(f"CSV incompatível: são esperadas as colunas '{TIME_COL}' e '{GLUCOSE_COL}' (ou 'timestamp' e 'glucose').")


def segment_sessions(df, gap_hours=2):
    # Abre uma nova sessão sempre que o intervalo entre leituras passa de 2 horas
    d = df.copy().sort_values("timestamp").reset_index(drop=True)
    gap = d.timestamp.diff().dt.total_seconds().div(60)
    d["gap_min"] = gap
    d["session_id"] = (gap > gap_hours * 60).cumsum() + 1
    return d


def quality_report(df, raw):
    d = segment_sessions(df)
    valid = d.gap_min.dropna()
    gaps = valid[valid > 22.5]
    sessions = d.session_id.nunique()
    return {
        "raw_rows": int(len(raw)), "glucose_rows": int(len(d)),
        "rows_without_glucose": int(len(raw) - len(d)),
        "start": d.timestamp.min().isoformat(), "end": d.timestamp.max().isoformat(),
        "span_days": float((d.timestamp.max() - d.timestamp.min()).total_seconds() / 86400),
        "median_interval_min": float(valid.median()), "mean_interval_min": float(valid.mean()),
        "p25_interval_min": float(valid.quantile(.25)), "p75_interval_min": float(valid.quantile(.75)),
        "n_gaps_gt_22_5min": int(len(gaps)), "max_gap_min": float(valid.max()),
        "n_sessions": int(sessions), "session_gap_threshold_hours": 2.0,
    }


def clinical_metrics(df, low=70, high=180):
    g = df.glucose.dropna()
    mean_g = float(g.mean())
    sd = float(g.std(ddof=1))
    cv = sd / mean_g * 100
    # GMI é uma estimativa baseada na glicose média, não substitui a HbA1c de laboratório
    gmi = 3.31 + 0.02392 * mean_g
    return {
        "n": len(g), "mean": mean_g, "median": float(g.median()), "sd": sd, "cv": float(cv),
        "min": float(g.min()), "max": float(g.max()), "tir": float(((g >= low) & (g <= high)).mean() * 100),
        "tar": float((g > high).mean() * 100), "tbr": float((g < low).mean() * 100), "gmi": float(gmi),
        "span_days": float((df.timestamp.max() - df.timestamp.min()).total_seconds() / 86400),
    }


def session_summary(df):
    d = segment_sessions(df)
    rows = []
    for sid, s in d.groupby("session_id"):
        if len(s) < 2:
            continue
        ints = s.timestamp.diff().dt.total_seconds().div(60).dropna()
        duration = (s.timestamp.iloc[-1] - s.timestamp.iloc[0]).total_seconds() / 86400
        expected = duration * 96 + 1
        rows.append({
            "session_id": int(sid), "start": str(s.timestamp.iloc[0]), "end": str(s.timestamp.iloc[-1]),
            "duration_days": duration, "n_readings": len(s), "median_interval_min": float(ints.median()),
            "coverage_pct": float(min(100, 100 * len(s) / expected)),
        })
    return pd.DataFrame(rows)


def _make_supervised_single(df, window_minutes=30, horizon_minutes=30, grid_minutes=15, max_interpolation_gap_minutes=30):
    d = segment_sessions(df)
    n_hist = round(window_minutes / grid_minutes)
    n_future = round(horizon_minutes / grid_minutes)
    parts = []
    for sid, s in d.groupby("session_id"):
        if len(s) < (n_hist + n_future + 3):
            continue
        s = s.set_index("timestamp")["glucose"].sort_index()
        r = s.resample(f"{grid_minutes}min").mean()
        # Interpola apenas buracos internos curtos; nunca cria dados entre sessões distintas
        r = r.interpolate(method="time", limit=max(1, round(max_interpolation_gap_minutes / grid_minutes)), limit_area="inside")
        # Descarta pontos cuja leitura real mais próxima esteja a mais de 30 minutos
        oi = s.index.view("int64")
        ri = r.index.view("int64")
        pos = np.searchsorted(oi, ri)
        p0 = np.clip(pos - 1, 0, len(oi) - 1)
        p1 = np.clip(pos, 0, len(oi) - 1)
        dist = np.minimum(abs(ri - oi[p0]), abs(ri - oi[p1])) / 1e9 / 60
        r.loc[dist > max_interpolation_gap_minutes] = np.nan
        z = pd.DataFrame(index=r.index)
        z["glucose"] = r
        z["roc_mg_dl_min"] = r.diff() / grid_minutes
        for i in range(1, n_hist + 1):
            z[f"glucose_lag_{i}"] = r.shift(i)
        z["mean_30m"] = r.rolling(n_hist + 1).mean()
        z["std_30m"] = r.rolling(n_hist + 1).std()
        z["target_glucose_15m"] = r.shift(-round(15 / grid_minutes))
        z["target_glucose_30m"] = r.shift(-n_future)
        z["session_id"] = sid
        features = [f"glucose_lag_{i}" for i in range(1, n_hist + 1)] + ["glucose", "roc_mg_dl_min", "mean_30m", "std_30m"]
        parts.append(z.dropna(subset=features + ["target_glucose_30m"]).reset_index())
    if not parts:
        return pd.DataFrame(), []
    return pd.concat(parts, ignore_index=True).sort_values("timestamp").reset_index(drop=True), features


def make_supervised(df, window_minutes=30, horizon_minutes=30, grid_minutes=15, max_interpolation_gap_minutes=30):
    # Monta as janelas supervisionadas separando cada paciente para não misturar séries
    if df.empty:
        return pd.DataFrame(), []
    if "user_id" in df.columns and df["user_id"].nunique() > 1:
        all_parts = []
        features_list = []
        for uid, user_df in df.groupby("user_id"):
            u_clean = user_df.sort_values("timestamp").reset_index(drop=True)
            u_data, feats = _make_supervised_single(u_clean, window_minutes, horizon_minutes, grid_minutes, max_interpolation_gap_minutes)
            if not u_data.empty:
                u_data["user_id"] = uid
                all_parts.append(u_data)
                features_list = feats
        if not all_parts:
            return pd.DataFrame(), []
        return pd.concat(all_parts, ignore_index=True).sort_values("timestamp").reset_index(drop=True), features_list

    return _make_supervised_single(df, window_minutes, horizon_minutes, grid_minutes, max_interpolation_gap_minutes)


def daily_15min_summary(df, low=70, high=180, max_interpolation_gap_minutes=30):
    # Agrupa o histórico em grades diárias de 96 intervalos (15 min) para o gráfico diário
    if df.empty or len(df) < 2:
        return {}
    d = segment_sessions(df)
    parts = []
    for sid, s in d.groupby("session_id"):
        if len(s) < 2:
            continue
        s = s.set_index("timestamp")["glucose"].sort_index()
        r = s.resample("15min").mean()
        r = r.interpolate(method="time", limit=max(1, round(max_interpolation_gap_minutes / 15)), limit_area="inside")
        oi = s.index.view("int64")
        ri = r.index.view("int64")
        if len(oi) > 0 and len(ri) > 0:
            pos = np.searchsorted(oi, ri)
            p0 = np.clip(pos - 1, 0, len(oi) - 1)
            p1 = np.clip(pos, 0, len(oi) - 1)
            dist = np.minimum(abs(ri - oi[p0]), abs(ri - oi[p1])) / 1e9 / 60
            r.loc[dist > max_interpolation_gap_minutes] = np.nan
        parts.append(pd.DataFrame({"timestamp": r.index, "glucose": r.values, "session_id": sid}))
    if not parts:
        return {}
    res = pd.concat(parts, ignore_index=True).sort_values("timestamp").reset_index(drop=True)
    res["date"] = res.timestamp.dt.strftime("%Y-%m-%d")
    res["time"] = res.timestamp.dt.strftime("%H:%M")
    slots = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 15, 30, 45)]

    daily_data = {}
    for d_str, grp in res.groupby("date"):
        mapping = dict(zip(grp["time"], grp["glucose"]))
        values = [round(float(mapping[s]), 1) if s in mapping and pd.notna(mapping[s]) else None for s in slots]
        valid = [v for v in values if v is not None]
        if not valid:
            continue
        n_valid = len(valid)
        daily_data[d_str] = {
            "date": d_str,
            "values": values,
            "readings_count": n_valid,
            "coverage_pct": round(n_valid / 96 * 100, 1),
            "mean": round(float(np.mean(valid)), 1),
            "min": round(float(min(valid)), 1),
            "max": round(float(max(valid)), 1),
            "tir": round(float(sum(low <= v <= high for v in valid) / n_valid * 100), 1),
            "tar": round(float(sum(v > high for v in valid) / n_valid * 100), 1),
            "tbr": round(float(sum(v < low for v in valid) / n_valid * 100), 1),
        }
    return daily_data
