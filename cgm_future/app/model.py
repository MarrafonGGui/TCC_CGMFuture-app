from __future__ import annotations
from pathlib import Path
from datetime import datetime
from typing import Any
import math
import joblib
import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression, SGDRegressor
from sklearn.ensemble import RandomForestRegressor, GradientBoostingRegressor
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

from .cgm import make_supervised

BASE_DIR = Path(__file__).resolve().parent.parent
MODELS_STORE_DIR = BASE_DIR / "models_store"
MODELS_STORE_DIR.mkdir(parents=True, exist_ok=True)

GLOBAL_FEATURES = [
    "glucose_lag_1",
    "glucose_lag_2",
    "glucose",
    "roc_mg_dl_min",
    "mean_30m",
    "std_30m",
]

PATIENT_ENRICHED_FEATURES = [
    "glucose_lag_1",
    "glucose_lag_2",
    "glucose",
    "roc_mg_dl_min",
    "accel_mg_dl",
    "mean_30m",
    "std_30m",
    "hour_sin",
    "hour_cos",
    "meal_window_score",
    "metabolic_excursion",
    "delta_target",
]

# Horários base de refeição em horas decimais (07:30, 12:30, 16:00 e 19:30)
DEFAULT_MEAL_HOURS = (7.5, 12.5, 16.0, 19.5)


def build_models() -> dict[str, Any]:
    return {
        "Regressão Linear": LinearRegression(),
        "Random Forest": RandomForestRegressor(
            n_estimators=250,
            max_depth=8,
            min_samples_leaf=3,
            random_state=42,
            n_jobs=-1,
        ),
        "Gradient Boosting": GradientBoostingRegressor(
            n_estimators=200,
            learning_rate=0.04,
            max_depth=2,
            random_state=42,
            loss="huber",
        ),
    }


def get_model_hyperparameters(model: Any) -> dict[str, Any]:
    if model is None or not hasattr(model, "get_params"):
        return {}
    raw = model.get_params(deep=False)
    clean: dict[str, Any] = {}
    for k, v in raw.items():
        if isinstance(v, (int, float, str, bool)) or v is None:
            clean[k] = v
    return clean


def run_models(data: pd.DataFrame, features: list[str], target_col: str = "target_glucose_30m", test_fraction: float = 0.30):
    cut = max(1, int(len(data) * (1 - test_fraction)))
    if cut >= len(data):
        cut = max(1, len(data) - 1)
    train, test = data.iloc[:cut], data.iloc[cut:]
    Xtr, ytr = train[features], train[target_col]
    Xte, yte = test[features], test[target_col]

    rows, fitted, preds = [], {}, {}
    for name, m in build_models().items():
        m.fit(Xtr, ytr)
        p = m.predict(Xte)
        fitted[name] = m
        preds[name] = p
        mse = mean_squared_error(yte, p)
        rows.append({
            "model": name,
            "MAE": float(mean_absolute_error(yte, p)),
            "RMSE": float(np.sqrt(mse)),
            "R2": float(r2_score(yte, p)) if len(yte) > 1 else 0.0,
            "hyperparameters": get_model_hyperparameters(m),
        })
    metrics = pd.DataFrame(rows).sort_values("MAE").reset_index(drop=True)
    best = str(metrics.iloc[0].model)
    best_model = fitted[best]
    vals = best_model.feature_importances_ if hasattr(best_model, "feature_importances_") else np.abs(best_model.coef_)
    imp = sorted(
        [{"feature": f, "importance": float(v)} for f, v in zip(features, vals)],
        key=lambda x: x["importance"],
        reverse=True,
    )
    test_out = test[["timestamp", "glucose", target_col]].copy()
    test_out["prediction"] = preds[best]
    return metrics, best, best_model, imp, test_out, train, test


def train_horizon_models(data: pd.DataFrame, features_by_horizon: dict[int, list[str]]) -> dict[int, dict[str, Any]]:
    """Treina os modelos globais para +15 e +30 min mantendo corte cronológico 70/30."""
    result: dict[int, dict[str, Any]] = {}
    for horizon, target in [(15, "target_glucose_15m"), (30, "target_glucose_30m")]:
        if target not in data.columns:
            continue
        feats = features_by_horizon[horizon]
        sub = data.dropna(subset=feats + [target]).copy()
        if len(sub) < 10:
            continue
        metrics, best, model, imp, test, train, _ = run_models(sub, feats, target_col=target)
        result[horizon] = {
            "metrics": metrics,
            "best": best,
            "model": model,
            "hyperparameters": get_model_hyperparameters(model),
            "importance": imp,
            "test": test,
            "train": train,
            "features": feats,
        }
    return result


# Persistência do modelo global em disco

def global_model_file() -> Path:
    return MODELS_STORE_DIR / "global_models.joblib"


def save_global_models_artifact(global_state: dict[str, Any]) -> None:
    try:
        payload = {
            "15": {
                "best": global_state["15"]["best"],
                "model": global_state["15"]["model"],
                "hyperparameters": global_state["15"].get("hyperparameters", {}),
                "features": global_state["15"]["features"],
                "metrics": global_state["15"]["metrics"],
                "importance": global_state["15"]["importance"],
            } if global_state.get("15") else None,
            "30": {
                "best": global_state["30"]["best"],
                "model": global_state["30"]["model"],
                "hyperparameters": global_state["30"].get("hyperparameters", {}),
                "features": global_state["30"]["features"],
                "metrics": global_state["30"]["metrics"],
                "importance": global_state["30"]["importance"],
            } if global_state.get("30") else None,
            "best": global_state.get("best"),
            "model_rows": global_state.get("model_rows", 0),
            "trained_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        joblib.dump(payload, global_model_file())
    except Exception as e:
        print(f"[AVISO save_global_models_artifact]: {e}")


def load_global_models_artifact() -> dict[str, Any] | None:
    path = global_model_file()
    if not path.exists():
        return None
    try:
        return joblib.load(path)
    except Exception:
        return None


# Variáveis contextuais e metabólicas individuais do paciente

def _meal_window_proximity(hours_float: np.ndarray | float, meal_hours: tuple[float, ...] = DEFAULT_MEAL_HOURS) -> np.ndarray:
    """Proximidade gaussiana aos horários de refeição (pico cerca de 35 min após comer)."""
    h_arr = np.asarray(hours_float, dtype=float)
    best = np.zeros_like(h_arr, dtype=float)
    for mh in meal_hours:
        postprandial_center = (mh + 0.6) % 24.0
        diff = np.minimum(np.abs(h_arr - postprandial_center), 24.0 - np.abs(h_arr - postprandial_center))
        score = np.exp(-0.5 * (diff / 1.1) ** 2)
        best = np.maximum(best, score)
    return best


def build_global_feature_row(current: float, glucose_15: float, glucose_30: float) -> pd.DataFrame:
    values = [float(glucose_30), float(glucose_15), float(current)]
    roc = (values[-1] - values[-2]) / 15.0
    mean_30 = sum(values) / 3.0
    std_30 = float(pd.Series(values).std(ddof=1)) if len(values) > 1 else 0.0
    return pd.DataFrame([{
        "glucose_lag_1": values[1],
        "glucose_lag_2": values[0],
        "glucose": values[2],
        "roc_mg_dl_min": roc,
        "mean_30m": mean_30,
        "std_30m": std_30,
    }])[GLOBAL_FEATURES]


def enrich_patient_dataframe(df_sup: pd.DataFrame, metabolic_profile: dict | None = None) -> pd.DataFrame:
    """Inclui ciclo circadiano, janela pós-prandial e relação ISF/ICR do paciente."""
    out = df_sup.copy()
    icr = float((metabolic_profile or {}).get("icr") or 8.0)
    isf = float((metabolic_profile or {}).get("isf") or 40.0)
    target = float((metabolic_profile or {}).get("target_glucose") or 100.0)
    excursion = isf / max(icr, 0.5)

    ts = pd.to_datetime(out["timestamp"], errors="coerce")
    hours_dec = ts.dt.hour.fillna(12).astype(float) + ts.dt.minute.fillna(0).astype(float) / 60.0

    out["accel_mg_dl"] = (out["glucose"] - out["glucose_lag_1"]) - (out["glucose_lag_1"] - out["glucose_lag_2"])
    out["hour_sin"] = np.sin(2.0 * math.pi * hours_dec / 24.0)
    out["hour_cos"] = np.cos(2.0 * math.pi * hours_dec / 24.0)
    out["meal_window_score"] = _meal_window_proximity(hours_dec.to_numpy())
    out["metabolic_excursion"] = excursion
    out["delta_target"] = out["glucose"] - target
    out["std_30m"] = out["std_30m"].fillna(0.0)
    return out


def build_patient_feature_row(
    current: float,
    glucose_15: float,
    glucose_30: float,
    timestamp_val: str | datetime | None = None,
    metabolic_profile: dict | None = None,
) -> pd.DataFrame:
    base_df = build_global_feature_row(current, glucose_15, glucose_30)
    dt = pd.to_datetime(timestamp_val) if timestamp_val is not None else datetime.now()
    base_df["timestamp"] = dt
    enriched = enrich_patient_dataframe(base_df, metabolic_profile=metabolic_profile)
    return enriched[PATIENT_ENRICHED_FEATURES]


# Modelo personalizado por paciente (aprendizado contínuo)

def patient_model_path(user_id: int) -> Path:
    return MODELS_STORE_DIR / f"patient_{int(user_id)}.joblib"


def load_patient_model_bundle(user_id: int) -> dict[str, Any] | None:
    p = patient_model_path(user_id)
    if not p.exists():
        return None
    try:
        return joblib.load(p)
    except Exception as e:
        print(f"[AVISO load_patient_model_bundle user={user_id}]: {e}")
        return None


def delete_patient_model_bundle(user_id: int) -> None:
    p = patient_model_path(user_id)
    if p.exists():
        p.unlink(missing_ok=True)


def _create_empty_patient_bundle(user_id: int) -> dict[str, Any]:
    return {
        "user_id": int(user_id),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "incremental_steps": 0,
        "n_local_samples": 0,
        "scaler": StandardScaler(),
        "scaler_fitted": False,
        "sgd_15": SGDRegressor(
            loss="huber",
            epsilon=5.0,
            penalty="l2",
            alpha=0.02,
            learning_rate="invscaling",
            eta0=0.015,
            power_t=0.25,
            random_state=42,
        ),
        "sgd_30": SGDRegressor(
            loss="huber",
            epsilon=5.0,
            penalty="l2",
            alpha=0.02,
            learning_rate="invscaling",
            eta0=0.015,
            power_t=0.25,
            random_state=42,
        ),
        "sgd_15_fitted": False,
        "sgd_30_fitted": False,
        "gbr_15": None,
        "gbr_30": None,
        "blend_15": {"w_sgd": 0.0, "w_gbr": 0.0},
        "blend_30": {"w_sgd": 0.0, "w_gbr": 0.0},
        "metrics_summary": {
            "global_mae_15m": None,
            "personalized_mae_15m": None,
            "global_mae_30m": None,
            "personalized_mae_30m": None,
            "personalized_rmse_30m": None,
            "mae_improvement_30m_pct": 0.0,
        },
    }


def _extract_validated_log_pairs(
    df_user: pd.DataFrame,
    prediction_logs: list[dict] | None,
    metabolic_profile: dict | None,
) -> tuple[pd.DataFrame, np.ndarray]:
    """Transforma previsões já avaliadas em amostras extras, dando mais peso às confirmadas manualmente."""
    if not prediction_logs:
        return pd.DataFrame(), np.array([])

    rows = []
    weights = []
    df_sorted = df_user.sort_values("timestamp") if not df_user.empty else pd.DataFrame()

    for log in prediction_logs:
        act15 = log.get("actual_15m")
        act30 = log.get("actual_30m")
        if act15 is None and act30 is None:
            continue
        try:
            ts = pd.to_datetime(log["timestamp"])
            g_cur = float(log["current_glucose"])
        except Exception:
            continue

        # Recupera as duas leituras anteriores mais próximas no histórico do paciente
        g_15, g_30 = g_cur, g_cur
        if not df_sorted.empty:
            prior = df_sorted[df_sorted["timestamp"] < ts]
            if len(prior) >= 1:
                g_15 = float(prior.iloc[-1]["glucose"])
            if len(prior) >= 2:
                g_30 = float(prior.iloc[-2]["glucose"])

        roc = (g_cur - g_15) / 15.0
        vals = [g_30, g_15, g_cur]
        mean_30 = sum(vals) / 3.0
        std_30 = float(pd.Series(vals).std(ddof=1))

        rows.append({
            "timestamp": ts,
            "glucose_lag_1": g_15,
            "glucose_lag_2": g_30,
            "glucose": g_cur,
            "roc_mg_dl_min": roc,
            "mean_30m": mean_30,
            "std_30m": std_30,
            "target_glucose_15m": float(act15) if act15 is not None else np.nan,
            "target_glucose_30m": float(act30) if act30 is not None else np.nan,
        })
        is_conf = bool(log.get("confirmed_15m")) or bool(log.get("confirmed_30m"))
        weights.append(2.2 if is_conf else 1.35)

    if not rows:
        return pd.DataFrame(), np.array([])

    sup_logs = enrich_patient_dataframe(pd.DataFrame(rows), metabolic_profile=metabolic_profile)
    return sup_logs, np.array(weights, dtype=float)


def fine_tune_patient_model(
    user_id: int,
    df_user: pd.DataFrame,
    metabolic_profile: dict | None,
    global_models: dict[str, Any],
    prediction_logs: list[dict] | None = None,
) -> dict[str, Any] | None:
    """Ajusta os corretores locais (SGD incremental + GBR) sobre os resíduos recentes do paciente."""
    if global_models.get("15") is None or global_models.get("30") is None:
        return None

    bundle = load_patient_model_bundle(user_id) or _create_empty_patient_bundle(user_id)

    # Limita às 900 leituras mais recentes para manter o treino leve e focado no padrão atual
    recent_df = df_user.tail(900).copy() if not df_user.empty else pd.DataFrame()
    sup_df = pd.DataFrame()
    if len(recent_df) >= 6:
        sup_raw, _ = make_supervised(recent_df, window_minutes=30, horizon_minutes=30)
        if not sup_raw.empty:
            sup_df = enrich_patient_dataframe(sup_raw, metabolic_profile=metabolic_profile)

    log_df, log_weights = _extract_validated_log_pairs(recent_df, prediction_logs, metabolic_profile)

    parts = []
    sample_weights_list = []
    if not sup_df.empty:
        parts.append(sup_df)
        # Peso gradualmente maior para as observações mais recentes
        recency_w = np.linspace(0.85, 1.35, len(sup_df))
        sample_weights_list.append(recency_w)
    if not log_df.empty:
        parts.append(log_df)
        sample_weights_list.append(log_weights)

    if not parts:
        # Salva o arquivo inicial mesmo antes de completar a primeira janela
        bundle["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        joblib.dump(bundle, patient_model_path(user_id))
        return get_patient_model_summary(user_id, bundle)

    combined = pd.concat(parts, ignore_index=True)
    all_weights = np.concatenate(sample_weights_list)

    g15_model = global_models["15"]["model"]
    g30_model = global_models["30"]["model"]

    valid_15_mask = combined[PATIENT_ENRICHED_FEATURES + ["target_glucose_15m"]].notna().all(axis=1)
    valid_30_mask = combined[PATIENT_ENRICHED_FEATURES + ["target_glucose_30m"]].notna().all(axis=1)

    X_enrich_all = combined[PATIENT_ENRICHED_FEATURES].fillna(0.0)
    tail_n = min(64, len(X_enrich_all))
    bundle["scaler"].partial_fit(X_enrich_all.iloc[-tail_n:])
    bundle["scaler_fitted"] = True

    # Horizonte +15 min
    if valid_15_mask.sum() >= 1:
        sub15 = combined.loc[valid_15_mask]
        w15 = all_weights[valid_15_mask.to_numpy()]
        X15_glob = sub15[GLOBAL_FEATURES]
        X15_pat_scaled = bundle["scaler"].transform(sub15[PATIENT_ENRICHED_FEATURES])
        y15 = sub15["target_glucose_15m"].to_numpy(dtype=float)
        base_pred_15 = g15_model.predict(X15_glob)
        res_15 = np.clip(y15 - base_pred_15, -120.0, 120.0)

        idx_recent = slice(max(0, len(sub15) - 48), len(sub15))
        bundle["sgd_15"].partial_fit(
            X15_pat_scaled[idx_recent],
            res_15[idx_recent],
            sample_weight=w15[idx_recent],
        )
        bundle["sgd_15_fitted"] = True

        if len(sub15) >= 18:
            gbr15 = GradientBoostingRegressor(
                n_estimators=80,
                learning_rate=0.035,
                max_depth=2,
                subsample=0.85,
                loss="huber",
                random_state=42,
            )
            # Com volume suficiente, valida os pesos da mistura no trecho final sem vazamento
            if len(sub15) >= 35:
                cut = int(len(sub15) * 0.75)
                gbr15.fit(sub15[PATIENT_ENRICHED_FEATURES].iloc[:cut], y15[:cut], sample_weight=w15[:cut])
                val_glob = base_pred_15[cut:]
                val_sgd = val_glob + np.clip(bundle["sgd_15"].predict(X15_pat_scaled[cut:]), -60.0, 60.0)
                val_gbr = gbr15.predict(sub15[PATIENT_ENRICHED_FEATURES].iloc[cut:])
                y_val = y15[cut:]

                mae_glob_15 = float(mean_absolute_error(y_val, val_glob))
                best_mae_15 = mae_glob_15
                best_b15 = {"w_sgd": 0.15, "w_gbr": 0.0}
                confidence_cap = min(0.80, len(sub15) / (len(sub15) + 25.0))
                for w_gbr in (0.0, 0.25, 0.45, 0.65):
                    for w_sgd in (0.0, 0.20, 0.35):
                        if w_gbr + w_sgd > confidence_cap + 0.15:
                            continue
                        cand = val_glob + w_sgd * (val_sgd - val_glob) + w_gbr * (val_gbr - val_glob)
                        c_mae = float(mean_absolute_error(y_val, cand))
                        if c_mae <= best_mae_15:
                            best_mae_15 = c_mae
                            best_b15 = {"w_sgd": round(w_sgd, 2), "w_gbr": round(w_gbr, 2)}

                bundle["blend_15"] = best_b15
                bundle["metrics_summary"]["global_mae_15m"] = round(mae_glob_15, 2)
                bundle["metrics_summary"]["personalized_mae_15m"] = round(best_mae_15, 2)
                # Reajusta no histórico completo do paciente depois de escolher os pesos
                gbr15.fit(sub15[PATIENT_ENRICHED_FEATURES], y15, sample_weight=w15)
            else:
                gbr15.fit(sub15[PATIENT_ENRICHED_FEATURES], y15, sample_weight=w15)
                conf = min(0.55, len(sub15) / (len(sub15) + 25.0))
                bundle["blend_15"] = {"w_sgd": 0.20, "w_gbr": round(conf * 0.6, 2)}
                pred_ens = base_pred_15 + 0.20 * res_15
                bundle["metrics_summary"]["global_mae_15m"] = round(float(mean_absolute_error(y15, base_pred_15)), 2)
                bundle["metrics_summary"]["personalized_mae_15m"] = round(float(mean_absolute_error(y15, pred_ens)), 2)

            bundle["gbr_15"] = gbr15
        else:
            conf = min(0.45, len(sub15) / (len(sub15) + 12.0))
            bundle["blend_15"] = {"w_sgd": round(max(0.15, conf), 2), "w_gbr": 0.0}
            sgd_corr = np.clip(bundle["sgd_15"].predict(X15_pat_scaled), -50.0, 50.0)
            pred_p15 = base_pred_15 + bundle["blend_15"]["w_sgd"] * sgd_corr
            bundle["metrics_summary"]["global_mae_15m"] = round(float(mean_absolute_error(y15, base_pred_15)), 2)
            bundle["metrics_summary"]["personalized_mae_15m"] = round(float(mean_absolute_error(y15, pred_p15)), 2)

    # Horizonte +30 min
    if valid_30_mask.sum() >= 1:
        sub30 = combined.loc[valid_30_mask]
        w30 = all_weights[valid_30_mask.to_numpy()]
        X30_glob = sub30[GLOBAL_FEATURES]
        X30_pat_scaled = bundle["scaler"].transform(sub30[PATIENT_ENRICHED_FEATURES])
        y30 = sub30["target_glucose_30m"].to_numpy(dtype=float)
        base_pred_30 = g30_model.predict(X30_glob)
        res_30 = np.clip(y30 - base_pred_30, -140.0, 140.0)

        idx_recent = slice(max(0, len(sub30) - 48), len(sub30))
        bundle["sgd_30"].partial_fit(
            X30_pat_scaled[idx_recent],
            res_30[idx_recent],
            sample_weight=w30[idx_recent],
        )
        bundle["sgd_30_fitted"] = True

        if len(sub30) >= 18:
            gbr30 = GradientBoostingRegressor(
                n_estimators=90,
                learning_rate=0.035,
                max_depth=2,
                subsample=0.85,
                loss="huber",
                random_state=42,
            )
            if len(sub30) >= 35:
                cut = int(len(sub30) * 0.75)
                gbr30.fit(sub30[PATIENT_ENRICHED_FEATURES].iloc[:cut], y30[:cut], sample_weight=w30[:cut])
                val_glob = base_pred_30[cut:]
                val_sgd = val_glob + np.clip(bundle["sgd_30"].predict(X30_pat_scaled[cut:]), -70.0, 70.0)
                val_gbr = gbr30.predict(sub30[PATIENT_ENRICHED_FEATURES].iloc[cut:])
                y_val = y30[cut:]

                mae_glob_30 = float(mean_absolute_error(y_val, val_glob))
                best_mae_30 = mae_glob_30
                best_rmse_30 = float(np.sqrt(mean_squared_error(y_val, val_glob)))
                best_b30 = {"w_sgd": 0.15, "w_gbr": 0.0}
                confidence_cap = min(0.80, len(sub30) / (len(sub30) + 25.0))
                for w_gbr in (0.0, 0.25, 0.45, 0.65, 0.75):
                    for w_sgd in (0.0, 0.20, 0.35):
                        if w_gbr + w_sgd > confidence_cap + 0.15:
                            continue
                        cand = val_glob + w_sgd * (val_sgd - val_glob) + w_gbr * (val_gbr - val_glob)
                        c_mae = float(mean_absolute_error(y_val, cand))
                        if c_mae <= best_mae_30:
                            best_mae_30 = c_mae
                            best_rmse_30 = float(np.sqrt(mean_squared_error(y_val, cand)))
                            best_b30 = {"w_sgd": round(w_sgd, 2), "w_gbr": round(w_gbr, 2)}

                bundle["blend_30"] = best_b30
                bundle["metrics_summary"]["global_mae_30m"] = round(mae_glob_30, 2)
                bundle["metrics_summary"]["personalized_mae_30m"] = round(best_mae_30, 2)
                bundle["metrics_summary"]["personalized_rmse_30m"] = round(best_rmse_30, 2)
                if mae_glob_30 > 0:
                    imp_pct = max(0.0, (mae_glob_30 - best_mae_30) / mae_glob_30 * 100.0)
                    bundle["metrics_summary"]["mae_improvement_30m_pct"] = round(imp_pct, 1)

                gbr30.fit(sub30[PATIENT_ENRICHED_FEATURES], y30, sample_weight=w30)
            else:
                gbr30.fit(sub30[PATIENT_ENRICHED_FEATURES], y30, sample_weight=w30)
                conf = min(0.55, len(sub30) / (len(sub30) + 25.0))
                bundle["blend_30"] = {"w_sgd": 0.20, "w_gbr": round(conf * 0.6, 2)}
                sgd_corr = np.clip(bundle["sgd_30"].predict(X30_pat_scaled), -60.0, 60.0)
                pred_p30 = base_pred_30 + 0.20 * sgd_corr
                g_mae = float(mean_absolute_error(y30, base_pred_30))
                p_mae = float(mean_absolute_error(y30, pred_p30))
                bundle["metrics_summary"]["global_mae_30m"] = round(g_mae, 2)
                bundle["metrics_summary"]["personalized_mae_30m"] = round(min(g_mae, p_mae), 2)
                bundle["metrics_summary"]["personalized_rmse_30m"] = round(float(np.sqrt(mean_squared_error(y30, pred_p30))), 2)

            bundle["gbr_30"] = gbr30
        else:
            conf = min(0.45, len(sub30) / (len(sub30) + 12.0))
            bundle["blend_30"] = {"w_sgd": round(max(0.15, conf), 2), "w_gbr": 0.0}
            sgd_corr = np.clip(bundle["sgd_30"].predict(X30_pat_scaled), -60.0, 60.0)
            pred_p30 = base_pred_30 + bundle["blend_30"]["w_sgd"] * sgd_corr
            g_mae = float(mean_absolute_error(y30, base_pred_30))
            p_mae = float(mean_absolute_error(y30, pred_p30))
            bundle["metrics_summary"]["global_mae_30m"] = round(g_mae, 2)
            bundle["metrics_summary"]["personalized_mae_30m"] = round(p_mae, 2)
            bundle["metrics_summary"]["personalized_rmse_30m"] = round(float(np.sqrt(mean_squared_error(y30, pred_p30))), 2)

    bundle["n_local_samples"] = int(max(valid_15_mask.sum(), valid_30_mask.sum()))
    bundle["incremental_steps"] = int(bundle.get("incremental_steps", 0)) + 1
    bundle["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    joblib.dump(bundle, patient_model_path(user_id))
    return get_patient_model_summary(user_id, bundle)


def predict_for_patient(
    user_id: int,
    current: float,
    glucose_15: float,
    glucose_30: float,
    global_models: dict[str, Any],
    timestamp_val: str | datetime | None = None,
    metabolic_profile: dict | None = None,
) -> dict[str, Any]:
    """Combina a predição do modelo global com a correção personalizada do paciente."""
    X_glob = build_global_feature_row(current, glucose_15, glucose_30)
    base_15 = float(global_models["15"]["model"].predict(X_glob)[0])
    base_30 = float(global_models["30"]["model"].predict(X_glob)[0])

    bundle = load_patient_model_bundle(user_id)
    if not bundle or not bundle.get("scaler_fitted"):
        return {
            "pred_15m": round(float(np.clip(base_15, 20.0, 600.0)), 1),
            "pred_30m": round(float(np.clip(base_30, 20.0, 600.0)), 1),
            "base_global_15m": round(base_15, 1),
            "base_global_30m": round(base_30, 1),
            "model_label_15": f"{global_models['15']['best']} (Global)",
            "model_label_30": f"{global_models['30']['best']} (Global)",
            "is_personalized": False,
        }

    X_pat = build_patient_feature_row(
        current=current,
        glucose_15=glucose_15,
        glucose_30=glucose_30,
        timestamp_val=timestamp_val,
        metabolic_profile=metabolic_profile,
    )
    X_pat_scaled = bundle["scaler"].transform(X_pat)

    # +15m
    p15 = base_15
    b15 = bundle.get("blend_15", {"w_sgd": 0.0, "w_gbr": 0.0})
    if bundle.get("sgd_15_fitted") and b15.get("w_sgd", 0) > 0:
        res15 = float(np.clip(bundle["sgd_15"].predict(X_pat_scaled)[0], -55.0, 55.0))
        p15 += b15["w_sgd"] * res15
    if bundle.get("gbr_15") is not None and b15.get("w_gbr", 0) > 0:
        gbr15_val = float(bundle["gbr_15"].predict(X_pat)[0])
        p15 += b15["w_gbr"] * (gbr15_val - base_15)

    # +30m
    p30 = base_30
    b30 = bundle.get("blend_30", {"w_sgd": 0.0, "w_gbr": 0.0})
    if bundle.get("sgd_30_fitted") and b30.get("w_sgd", 0) > 0:
        res30 = float(np.clip(bundle["sgd_30"].predict(X_pat_scaled)[0], -75.0, 75.0))
        p30 += b30["w_sgd"] * res30
    if bundle.get("gbr_30") is not None and b30.get("w_gbr", 0) > 0:
        gbr30_val = float(bundle["gbr_30"].predict(X_pat)[0])
        p30 += b30["w_gbr"] * (gbr30_val - base_30)

    p15_final = round(float(np.clip(p15, 20.0, 600.0)), 1)
    p30_final = round(float(np.clip(p30, 20.0, 600.0)), 1)

    steps = bundle.get("incremental_steps", 1)
    return {
        "pred_15m": p15_final,
        "pred_30m": p30_final,
        "base_global_15m": round(base_15, 1),
        "base_global_30m": round(base_30, 1),
        "model_label_15": f"IA Personalizada #{user_id} (v{steps})",
        "model_label_30": f"IA Personalizada #{user_id} (v{steps})",
        "is_personalized": True,
    }


def get_patient_model_summary(user_id: int, bundle: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resumo técnico e pesos do modelo individual para exibição médica e administrativa."""
    b = bundle if bundle is not None else load_patient_model_bundle(user_id)
    path = patient_model_path(user_id)
    if not b:
        return {
            "exists": False,
            "user_id": user_id,
            "model_file": path.name,
            "incremental_steps": 0,
            "n_local_samples": 0,
            "updated_at": None,
            "blend_15": {"w_sgd": 0.0, "w_gbr": 0.0},
            "blend_30": {"w_sgd": 0.0, "w_gbr": 0.0},
            "metrics_summary": {},
            "hyperparameters": {},
            "personalized_feature_weights": [],
        }

    weights_info = []
    if b.get("gbr_30") is not None and hasattr(b["gbr_30"], "feature_importances_"):
        for feat, val in zip(PATIENT_ENRICHED_FEATURES, b["gbr_30"].feature_importances_):
            weights_info.append({"feature": feat, "importance": round(float(val), 4)})
        weights_info.sort(key=lambda x: x["importance"], reverse=True)
    elif b.get("sgd_30_fitted") and hasattr(b["sgd_30"], "coef_"):
        coefs = np.abs(np.ravel(b["sgd_30"].coef_))
        total = float(coefs.sum()) or 1.0
        for feat, val in zip(PATIENT_ENRICHED_FEATURES, coefs / total):
            weights_info.append({"feature": feat, "importance": round(float(val), 4)})
        weights_info.sort(key=lambda x: x["importance"], reverse=True)

    return {
        "exists": path.exists(),
        "user_id": user_id,
        "model_file": path.name,
        "incremental_steps": int(b.get("incremental_steps", 0)),
        "n_local_samples": int(b.get("n_local_samples", 0)),
        "updated_at": b.get("updated_at"),
        "blend_15": b.get("blend_15", {}),
        "blend_30": b.get("blend_30", {}),
        "metrics_summary": b.get("metrics_summary", {}),
        "hyperparameters": {
            "online_sgd_30": get_model_hyperparameters(b.get("sgd_30")),
            "local_gbr_30": get_model_hyperparameters(b.get("gbr_30")) if b.get("gbr_30") else None,
        },
        "personalized_feature_weights": weights_info,
    }
