from __future__ import annotations
from pathlib import Path
import tempfile
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from typing import Any
import pandas as pd
from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException, Depends, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from .cgm import (
    read_libre_csv,
    quality_report,
    clinical_metrics,
    make_supervised,
    session_summary,
    daily_15min_summary,
)
from .model import (
    train_horizon_models,
    save_global_models_artifact,
    load_global_models_artifact,
    fine_tune_patient_model,
    predict_for_patient,
    get_patient_model_summary,
    load_patient_model_bundle,
    delete_patient_model_bundle,
)
from .synthetic import generate
from .database import (
    ROLE_ADM,
    ROLE_MEDICO,
    ROLE_PACIENTE,
    init_db,
    seed_initial_data_if_empty,
    insert_readings_df,
    insert_single_reading,
    get_reading_by_id,
    update_manual_reading,
    get_readings_df,
    get_latest_readings,
    get_manual_readings,
    delete_reading_by_id,
    clear_manual_readings,
    clear_all_data,
    get_database_stats,
    log_prediction,
    get_prediction_by_id,
    confirm_prediction_feedback,
    update_prediction_actuals,
    get_prediction_logs,
    get_db_info,
    get_user_by_username,
    get_user_by_id,
    create_user,
    update_user,
    assign_patient_doctor,
    list_all_users,
    get_doctors_with_portfolios,
    delete_user,
    create_session,
    get_session_user,
    delete_session,
    verify_password,
    get_patient_profile,
    save_patient_profile,
    log_diet_entry,
    get_diet_logs,
    get_diet_entry_by_id,
    review_diet_entry,
    send_direct_message,
    get_messages_between,
    get_unread_counts_for_user,
)
from .gemini_service import analyze_diet_content
from .metabolic_calculator import calculate_total_bolus, simulate_postprandial_curve

BASE = Path(__file__).resolve().parent.parent

# Cache em memória dos modelos globais
GLOBAL_MODELS: dict[str, Any] = {
    "15": None,
    "30": None,
    "metrics": None,
    "metrics_15": None,
    "importance": [],
    "hyperparameters": {},
    "best": None,
    "model_rows": 0,
}


def train_global_models() -> dict[str, Any] | None:
    """Treina os modelos globais (+15 e +30 min) usando todas as séries disponíveis."""
    df_all = get_readings_df(user_id=None)
    if df_all.empty or len(df_all) < 50:
        return None

    try:
        data, features = make_supervised(df_all, 30, 30)
        if len(data) < 50:
            return None

        trained = train_horizon_models(data, {15: features, 30: features})
        if 15 not in trained or 30 not in trained:
            return None

        GLOBAL_MODELS["15"] = trained[15]
        GLOBAL_MODELS["30"] = trained[30]
        GLOBAL_MODELS["metrics"] = trained[30]["metrics"]
        GLOBAL_MODELS["metrics_15"] = trained[15]["metrics"]
        GLOBAL_MODELS["importance"] = trained[30]["importance"][:10]
        GLOBAL_MODELS["hyperparameters"] = {
            "best_15m": trained[15].get("hyperparameters", {}),
            "best_30m": trained[30].get("hyperparameters", {}),
        }
        GLOBAL_MODELS["best"] = str(trained[30]["best"])
        GLOBAL_MODELS["model_rows"] = int(len(data))
        save_global_models_artifact(GLOBAL_MODELS)
        return GLOBAL_MODELS
    except Exception as e:
        print(f"[AVISO no treinamento global]: {e}")
        return None


def ensure_global_models_ready() -> bool:
    if GLOBAL_MODELS["15"] is not None and GLOBAL_MODELS["30"] is not None:
        return True
    cached = load_global_models_artifact()
    if cached and cached.get("15") and cached.get("30"):
        GLOBAL_MODELS["15"] = cached["15"]
        GLOBAL_MODELS["30"] = cached["30"]
        GLOBAL_MODELS["metrics"] = cached["30"]["metrics"]
        GLOBAL_MODELS["metrics_15"] = cached["15"]["metrics"]
        GLOBAL_MODELS["importance"] = cached["30"]["importance"][:10]
        GLOBAL_MODELS["hyperparameters"] = {
            "best_15m": cached["15"].get("hyperparameters", {}),
            "best_30m": cached["30"].get("hyperparameters", {}),
        }
        GLOBAL_MODELS["best"] = cached.get("best") or cached["30"]["best"]
        GLOBAL_MODELS["model_rows"] = cached.get("model_rows", 0)
        return True
    return train_global_models() is not None


def trigger_continuous_learning(user_id: int) -> dict[str, Any] | None:
    """Atualiza o modelo individual do paciente com suas leituras e confirmações recentes."""
    if not ensure_global_models_ready():
        return None
    try:
        df_u = get_readings_df(user_id=user_id)
        prof = get_patient_profile(user_id=user_id)
        logs = get_prediction_logs(user_id=user_id, limit=60)
        return fine_tune_patient_model(
            user_id=user_id,
            df_user=df_u,
            metabolic_profile=prof,
            global_models=GLOBAL_MODELS,
            prediction_logs=logs,
        )
    except Exception as e:
        print(f"[AVISO fine_tune_patient_model user={user_id}]: {e}")
        return None


# Autenticação e controle de acesso por perfil (RBAC)

def get_current_user_optional(request: Request) -> dict | None:
    token = request.cookies.get("cgm_session_token")
    if not token:
        auth_hdr = request.headers.get("Authorization")
        if auth_hdr and auth_hdr.startswith("Bearer "):
            token = auth_hdr.split(" ", 1)[1].strip()
    if token:
        return get_session_user(token)
    return None


def get_current_user(request: Request) -> dict:
    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(status_code=401, detail="Sessão não autenticada ou expirada.")
    return user


def require_admin(request: Request) -> dict:
    user = get_current_user(request)
    if user.get("role") != ROLE_ADM:
        raise HTTPException(status_code=403, detail="Acesso exclusivo ao perfil Administrador (ADM).")
    return user


def require_doctor_or_admin(request: Request) -> dict:
    user = get_current_user(request)
    if user.get("role") not in (ROLE_MEDICO, ROLE_ADM):
        raise HTTPException(status_code=403, detail="Acesso restrito a Médicos e Administradores.")
    return user


def resolve_target_patient(current_user: dict, patient_id: int | None = None) -> dict | None:
    """Resolve o paciente em foco respeitando o escopo de cada perfil (paciente, médico ou adm)."""
    role = current_user["role"]

    if role == ROLE_PACIENTE:
        if patient_id is not None and int(patient_id) != int(current_user["id"]):
            raise HTTPException(status_code=403, detail="Acesso negado: isolamento estrito de dados do paciente.")
        return current_user

    if role == ROLE_MEDICO:
        if patient_id is not None:
            target = get_user_by_id(int(patient_id))
            if not target or target["role"] != ROLE_PACIENTE or target.get("doctor_id") != current_user["id"]:
                raise HTTPException(status_code=403, detail="Este paciente não pertence à sua carteira médica.")
            return target
        # Se nenhum paciente foi informado, abre o primeiro da carteira
        my_patients = list_all_users(role_filter=ROLE_PACIENTE, doctor_id_filter=current_user["id"])
        return my_patients[0] if my_patients else None

    if patient_id is not None:
        target = get_user_by_id(int(patient_id))
        if not target:
            raise HTTPException(status_code=404, detail="Paciente/usuário selecionado não encontrado.")
        return target
    return current_user


def verify_can_manage_reading_or_log(current_user: dict, owner_user_id: int) -> None:
    role = current_user["role"]
    if role == ROLE_ADM:
        return
    if role == ROLE_PACIENTE:
        if int(owner_user_id) != int(current_user["id"]):
            raise HTTPException(status_code=403, detail="Você só pode gerenciar seus próprios registros.")
        return
    if role == ROLE_MEDICO:
        owner = get_user_by_id(int(owner_user_id))
        if not owner or owner.get("doctor_id") != current_user["id"]:
            raise HTTPException(status_code=403, detail="Este registro não pertence a um paciente da sua carteira.")
        return
    raise HTTPException(status_code=403, detail="Permissão insuficiente.")


# Montagem dos dados retornados para a tela principal conforme o perfil logado

def package_for_user(
    target_user: dict,
    viewer_user: dict,
    low: float = 70.0,
    high: float = 180.0,
) -> dict[str, Any]:
    uid = target_user["id"]
    viewer_role = viewer_user["role"]
    is_patient_viewer = (viewer_role == ROLE_PACIENTE)
    is_admin_viewer = (viewer_role == ROLE_ADM)

    df_user = get_readings_df(user_id=uid)
    raw_stats = get_database_stats(user_id=uid)
    manual_history = get_manual_readings(user_id=uid, limit=40)
    sources = [s["filename"] for s in raw_stats.get("sources", [])]
    patient_profile = get_patient_profile(user_id=uid)
    diet_logs = get_diet_logs(user_id=uid, limit=20)

    ensure_global_models_ready()

    # Garantir que o modelo personalizado do paciente esteja calibrado se já houver leituras
    if not df_user.empty and load_patient_model_bundle(uid) is None:
        trigger_continuous_learning(uid)

    pers_summary = get_patient_model_summary(uid)

    # Sanitizar métricas técnicas caso o visualizador seja PACIENTE
    if is_patient_viewer:
        safe_stats = {
            "sync_label": "Sincronizado",
            "total_readings": len(df_user),
        }
        metrics_records = []
        importance_records = []
        hyperparams_out = {}
        pers_summary_out = None
    elif is_admin_viewer:
        safe_stats = raw_stats
        metrics_df = GLOBAL_MODELS.get("metrics")
        metrics_records = []
        if metrics_df is not None:
            for r in metrics_df.to_dict("records"):
                metrics_records.append({
                    "model": r["model"],
                    "MAE": round(float(r["MAE"]), 4),
                    "RMSE": round(float(r["RMSE"]), 4),
                    "R2": round(float(r["R2"]), 4),
                    "hyperparameters": r.get("hyperparameters", {}),
                })
        importance_records = GLOBAL_MODELS.get("importance", [])
        hyperparams_out = GLOBAL_MODELS.get("hyperparameters", {})
        pers_summary_out = pers_summary
    else:
        # Perfil MÉDICO: estatísticas clínicas do paciente sem expor controles de infra de banco
        safe_stats = {
            "sync_label": "Sincronizado",
            "total_readings": raw_stats.get("total_readings", 0),
            "manual_readings": raw_stats.get("manual_readings", 0),
            "sensor_readings": raw_stats.get("sensor_readings", 0),
            "span_days": raw_stats.get("span_days", 0),
            "total_predictions": raw_stats.get("total_predictions", 0),
            "evaluated_predictions": raw_stats.get("evaluated_predictions", 0),
            "confirmed_predictions": raw_stats.get("confirmed_predictions", 0),
        }
        metrics_records = []
        importance_records = []
        hyperparams_out = {}
        pers_summary_out = {
            "exists": pers_summary.get("exists", False),
            "incremental_steps": pers_summary.get("incremental_steps", 0),
            "n_local_samples": pers_summary.get("n_local_samples", 0),
            "updated_at": pers_summary.get("updated_at"),
        }

    prediction_logs = get_prediction_logs(user_id=uid, limit=25)

    model_dict: dict[str, Any] = {
        "best": GLOBAL_MODELS.get("best") or "Modelo Supervisionado",
        "metrics": metrics_records,
        "importance": importance_records,
        "hyperparameters": hyperparams_out,
        "personalized": pers_summary_out,
        "is_personalized_active": bool(pers_summary.get("exists")),
        "latest_glucose": None,
        "prediction_15m": None,
        "prediction_30m": None,
        "model_15_label": "IA Personalizada",
        "model_30_label": "IA Personalizada",
        "active_prediction_log": prediction_logs[0] if prediction_logs else None,
        "model_rows": GLOBAL_MODELS.get("model_rows", 0) if is_admin_viewer else None,
    }

    if df_user.empty:
        return {
            "status": "empty",
            "message": f"Nenhum dado glicêmico registrado ainda para {target_user['full_name'] or target_user['username']}.",
            "viewer": viewer_user,
            "target_user": target_user,
            "metabolic_profile": patient_profile,
            "diet_logs": diet_logs,
            "clinical": None,
            "quality": None,
            "sessions": [],
            "history": [],
            "daily_history": {},
            "available_days": [],
            "low": low,
            "high": high,
            "sources": [],
            "total_accumulated_rows": 0,
            "db_stats": safe_stats,
            "manual_history": manual_history,
            "prediction_logs": prediction_logs,
            "model": model_dict,
        }

    dummy_raw = pd.DataFrame(index=range(len(df_user)))
    quality = None if is_patient_viewer else quality_report(df_user, dummy_raw)
    clinical = clinical_metrics(df_user, low, high)
    sessions_df = session_summary(df_user)
    daily_hist = daily_15min_summary(df_user, low=low, high=high)
    available_days = sorted(list(daily_hist.keys()))
    hist = df_user.tail(500).copy()

    latest_glucose = float(df_user.iloc[-1]["glucose"])
    latest_ts = df_user.iloc[-1]["timestamp"]
    model_dict["latest_glucose"] = latest_glucose

    if GLOBAL_MODELS["15"] is not None and GLOBAL_MODELS["30"] is not None:
        try:
            u_c = latest_glucose
            u_15 = float(df_user.iloc[-2]["glucose"]) if len(df_user) >= 2 else latest_glucose
            u_30 = float(df_user.iloc[-3]["glucose"]) if len(df_user) >= 3 else u_15

            pred_res = predict_for_patient(
                user_id=uid,
                current=u_c,
                glucose_15=u_15,
                glucose_30=u_30,
                global_models=GLOBAL_MODELS,
                timestamp_val=latest_ts,
                metabolic_profile=patient_profile,
            )
            model_dict["prediction_15m"] = pred_res["pred_15m"]
            model_dict["prediction_30m"] = pred_res["pred_30m"]
            model_dict["model_15_label"] = pred_res["model_label_15"]
            model_dict["model_30_label"] = pred_res["model_label_30"]
            model_dict["is_personalized_active"] = pred_res["is_personalized"]

            # Garantir que exista ao menos um registro ativo em predictions_log para interação de feedback nos cards
            if not prediction_logs:
                new_log_id = log_prediction(
                    latest_ts,
                    u_c,
                    pred_res["pred_15m"],
                    pred_res["pred_30m"],
                    user_id=uid,
                    model_15m=pred_res["model_label_15"],
                    model_30m=pred_res["model_label_30"],
                )
                prediction_logs = get_prediction_logs(user_id=uid, limit=25)
                model_dict["active_prediction_log"] = prediction_logs[0] if prediction_logs else {"id": new_log_id}
            else:
                # Exibir nos cards os valores correspondentes ao último log ativo ou atual
                active_log = prediction_logs[0]
                model_dict["active_prediction_log"] = active_log
                model_dict["prediction_15m"] = active_log["pred_15m"]
                model_dict["prediction_30m"] = active_log["pred_30m"]
                model_dict["model_15_label"] = active_log.get("model_15m") or pred_res["model_label_15"]
                model_dict["model_30_label"] = active_log.get("model_30m") or pred_res["model_label_30"]
        except Exception as e:
            print(f"[AVISO predição usuário {uid}]: {e}")

    source_label = ", ".join(sources) if len(sources) <= 3 else f"{len(sources)} fontes ({len(df_user)} leituras)"

    return {
        "status": "ok",
        "file": source_label,
        "viewer": viewer_user,
        "target_user": target_user,
        "quality": quality,
        "clinical": clinical,
        "sessions": [] if is_patient_viewer else sessions_df.to_dict("records"),
        "model": model_dict,
        "history": hist.assign(timestamp=hist.timestamp.astype(str)).to_dict("records"),
        "daily_history": daily_hist,
        "available_days": available_days,
        "low": low,
        "high": high,
        "sources": sources,
        "total_accumulated_rows": len(df_user),
        "db_stats": safe_stats,
        "manual_history": manual_history,
        "prediction_logs": prediction_logs,
        "metabolic_profile": patient_profile,
        "diet_logs": diet_logs,
    }


# Inicialização da aplicação

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    seed_initial_data_if_empty()
    try:
        ensure_global_models_ready()
        print("[CGM] Banco inicializado e modelos globais prontos.")
    except Exception as e:
        print(f"[CGM] Aviso na inicialização dos modelos: {e}")
    yield


app = FastAPI(title="TCC — Monitoramento Glicêmico RBAC & IA Personalizada", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))


# Páginas da SPA e autenticação por sessão/JWT

@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
@app.get("/carteira-pacientes", response_class=HTMLResponse)
@app.get("/importacao", response_class=HTMLResponse)
@app.get("/alimentacao", response_class=HTMLResponse)
@app.get("/mensagens", response_class=HTMLResponse)
@app.get("/admin", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/api/auth/login")
async def login(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}

    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "").strip()

    if not username or not password:
        return JSONResponse({"error": "Informe o nome de usuário e a senha."}, status_code=400)

    user = get_user_by_username(username, include_password=True)
    if not user or not verify_password(password, user["password_hash"]):
        return JSONResponse({"error": "Usuário ou senha incorretos."}, status_code=401)

    jwt_token = create_session(user["id"])
    safe_user = get_user_by_id(user["id"])
    resp = JSONResponse({
        "status": "ok",
        "access_token": jwt_token,
        "token_type": "bearer",
        "user": safe_user,
    })
    resp.set_cookie(
        key="cgm_session_token",
        value=jwt_token,
        httponly=True,
        samesite="lax",
        max_age=7 * 24 * 3600,
    )
    return resp


@app.post("/api/auth/logout")
def logout(request: Request):
    token = request.cookies.get("cgm_session_token")
    if not token:
        auth_hdr = request.headers.get("Authorization")
        if auth_hdr and auth_hdr.startswith("Bearer "):
            token = auth_hdr.split(" ", 1)[1].strip()
    if token:
        delete_session(token)
    resp = JSONResponse({"status": "ok", "message": "Sessão encerrada com sucesso."})
    resp.delete_cookie("cgm_session_token")
    return resp


@app.get("/api/auth/me")
def auth_me(request: Request):
    user = get_current_user_optional(request)
    if not user:
        return {"authenticated": False}
    unread_map = get_unread_counts_for_user(user["id"])
    return {
        "authenticated": True,
        "user": user,
        "unread_total": sum(unread_map.values()),
    }


# Gestão administrativa de usuários e vínculos médico-paciente

@app.get("/api/admin/users")
def admin_get_users(admin: dict = Depends(require_admin)):
    all_users = list_all_users()
    doctors = [u for u in all_users if u["role"] == ROLE_MEDICO]
    patients = [u for u in all_users if u["role"] == ROLE_PACIENTE]
    portfolios = get_doctors_with_portfolios()
    return {
        "status": "ok",
        "users": all_users,
        "doctors": doctors,
        "patients": patients,
        "portfolios": portfolios,
    }


@app.post("/api/admin/users")
async def admin_create_user(request: Request, admin: dict = Depends(require_admin)):
    try:
        data = await request.json()
    except Exception:
        data = {}

    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "").strip()
    full_name = str(data.get("full_name") or "").strip()
    role = str(data.get("role") or ROLE_PACIENTE).strip().lower()
    doctor_id = data.get("doctor_id")
    crm = data.get("crm")

    if not username or not password:
        return JSONResponse({"error": "Login e senha são obrigatórios."}, status_code=400)

    try:
        new_user = create_user(
            username=username,
            password=password,
            role=role,
            full_name=full_name,
            doctor_id=int(doctor_id) if doctor_id not in (None, "", 0, "0") else None,
            crm=str(crm) if crm else None,
        )
        return {
            "status": "ok",
            "message": f"Cadastro '{new_user['username']}' ({new_user['role']}) criado com sucesso!",
            "user": new_user,
            "users": list_all_users(),
            "portfolios": get_doctors_with_portfolios(),
        }
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"Erro ao criar usuário: {str(e)}"}, status_code=500)


@app.put("/api/admin/users/{user_id}")
async def admin_update_user(user_id: int, request: Request, admin: dict = Depends(require_admin)):
    try:
        data = await request.json()
    except Exception:
        data = {}

    try:
        updated = update_user(
            user_id=user_id,
            username=data.get("username"),
            password=data.get("password"),
            full_name=data.get("full_name"),
            role=data.get("role"),
            doctor_id=data.get("doctor_id") if "doctor_id" in data else None,
            crm=data.get("crm") if "crm" in data else None,
        )
        return {
            "status": "ok",
            "message": f"Cadastro de '{updated['full_name']}' atualizado com sucesso.",
            "user": updated,
            "users": list_all_users(),
            "portfolios": get_doctors_with_portfolios(),
        }
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.patch("/api/admin/patients/{patient_id}/doctor")
async def admin_link_patient_doctor(patient_id: int, request: Request, admin: dict = Depends(require_admin)):
    try:
        data = await request.json()
    except Exception:
        data = {}

    raw_doc = data.get("doctor_id")
    doc_id = None if raw_doc in (None, "", 0, "0", "null") else int(raw_doc)
    try:
        updated = assign_patient_doctor(patient_id=patient_id, doctor_id=doc_id)
        msg = (
            f"Paciente '{updated['full_name']}' vinculado ao médico {updated.get('doctor_name')}."
            if doc_id
            else f"Paciente '{updated['full_name']}' desvinculado de médico."
        )
        return {
            "status": "ok",
            "message": msg,
            "user": updated,
            "users": list_all_users(),
            "portfolios": get_doctors_with_portfolios(),
        }
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: int, admin: dict = Depends(require_admin)):
    if admin["id"] == user_id:
        return JSONResponse({"error": "Você não pode excluir sua própria conta de administrador conectada."}, status_code=400)
    try:
        success = delete_user(user_id)
        if not success:
            return JSONResponse({"error": "Usuário não encontrado."}, status_code=404)
        delete_patient_model_bundle(user_id)
        return {
            "status": "ok",
            "message": "Registro excluído com sucesso.",
            "users": list_all_users(),
            "portfolios": get_doctors_with_portfolios(),
        }
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/admin/doctors/{doctor_id}/portfolio")
def admin_get_doctor_portfolio(doctor_id: int, admin: dict = Depends(require_admin)):
    doc = get_user_by_id(doctor_id)
    if not doc or doc["role"] != ROLE_MEDICO:
        raise HTTPException(status_code=404, detail="Médico não encontrado.")
    patients = list_all_users(role_filter=ROLE_PACIENTE, doctor_id_filter=doctor_id)
    return {
        "status": "ok",
        "doctor": doc,
        "patients": patients,
        "patients_count": len(patients),
    }


# Carteira de pacientes vinculados ao médico

@app.get("/api/doctor/patients")
def doctor_list_patients(current_user: dict = Depends(require_doctor_or_admin)):
    unread_map = get_unread_counts_for_user(current_user["id"])
    if current_user["role"] == ROLE_MEDICO:
        patients = list_all_users(role_filter=ROLE_PACIENTE, doctor_id_filter=current_user["id"])
    else:
        patients = list_all_users(role_filter=ROLE_PACIENTE)
    for p in patients:
        p["unread_messages"] = unread_map.get(p["id"], 0)
    return {"status": "ok", "patients": patients}


@app.post("/api/doctor/patients")
async def doctor_create_patient(request: Request, current_user: dict = Depends(require_doctor_or_admin)):
    try:
        data = await request.json()
    except Exception:
        data = {}

    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "").strip()
    full_name = str(data.get("full_name") or "").strip()

    if not username or not password:
        return JSONResponse({"error": "Login e senha do paciente são obrigatórios."}, status_code=400)

    assigned_doc_id = current_user["id"] if current_user["role"] == ROLE_MEDICO else data.get("doctor_id")
    try:
        new_pat = create_user(
            username=username,
            password=password,
            role=ROLE_PACIENTE,
            full_name=full_name,
            doctor_id=int(assigned_doc_id) if assigned_doc_id else None,
        )
        # Configurar dosagens metabólicas iniciais se enviadas
        if any(k in data for k in ("icr", "isf", "target_glucose")):
            save_patient_profile(
                user_id=new_pat["id"],
                icr=float(data.get("icr") or 8.0),
                isf=float(data.get("isf") or 40.0),
                target_glucose=float(data.get("target_glucose") or 100.0),
                updated_by=current_user["full_name"] or current_user["username"],
            )
        patients = list_all_users(
            role_filter=ROLE_PACIENTE,
            doctor_id_filter=current_user["id"] if current_user["role"] == ROLE_MEDICO else None,
        )
        return {
            "status": "ok",
            "message": f"Paciente '{new_pat['full_name']}' cadastrado na sua carteira!",
            "patient": new_pat,
            "patients": patients,
        }
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.put("/api/doctor/patients/{patient_id}")
async def doctor_update_patient(patient_id: int, request: Request, current_user: dict = Depends(require_doctor_or_admin)):
    pat = get_user_by_id(patient_id)
    if not pat or pat["role"] != ROLE_PACIENTE:
        return JSONResponse({"error": "Paciente não encontrado."}, status_code=404)
    if current_user["role"] == ROLE_MEDICO and pat.get("doctor_id") != current_user["id"]:
        return JSONResponse({"error": "Você só pode editar pacientes vinculados à sua carteira."}, status_code=403)

    try:
        data = await request.json()
    except Exception:
        data = {}

    try:
        updated = update_user(
            user_id=patient_id,
            username=data.get("username"),
            password=data.get("password"),
            full_name=data.get("full_name"),
            role=ROLE_PACIENTE,
            doctor_id=pat.get("doctor_id"),
        )
        patients = list_all_users(
            role_filter=ROLE_PACIENTE,
            doctor_id_filter=current_user["id"] if current_user["role"] == ROLE_MEDICO else None,
        )
        return {
            "status": "ok",
            "message": f"Dados do paciente '{updated['full_name']}' atualizados.",
            "patient": updated,
            "patients": patients,
        }
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.delete("/api/doctor/patients/{patient_id}")
def doctor_delete_patient(patient_id: int, current_user: dict = Depends(require_doctor_or_admin)):
    pat = get_user_by_id(patient_id)
    if not pat or pat["role"] != ROLE_PACIENTE:
        return JSONResponse({"error": "Paciente não encontrado."}, status_code=404)
    if current_user["role"] == ROLE_MEDICO and pat.get("doctor_id") != current_user["id"]:
        return JSONResponse({"error": "Você só pode excluir pacientes vinculados à sua carteira."}, status_code=403)

    delete_user(patient_id)
    delete_patient_model_bundle(patient_id)
    patients = list_all_users(
        role_filter=ROLE_PACIENTE,
        doctor_id_filter=current_user["id"] if current_user["role"] == ROLE_MEDICO else None,
    )
    return {
        "status": "ok",
        "message": "Paciente excluído da sua carteira com sucesso.",
        "patients": patients,
    }


# Banco de dados, retreinamento global e geração sintética (ADM)

@app.get("/api/database/info")
def database_info(admin: dict = Depends(require_admin)):
    return get_db_info()


@app.post("/api/database/reconnect")
def database_reconnect(admin: dict = Depends(require_admin)):
    init_db(force_recheck=True)
    seed_initial_data_if_empty(user_id=admin["id"])
    train_global_models()
    info = get_db_info()
    return {
        "status": "ok",
        "db_info": info,
        "message": f"Conexão testada pelo ADM. Motor ativo: {info['active_engine'].upper()}",
    }


@app.post("/api/models/retrain")
def retrain_models(
    patient_id: int | None = Query(None),
    admin: dict = Depends(require_admin),
):
    try:
        res = train_global_models()
        if res is None:
            return JSONResponse({"error": "Não há dados suficientes no banco para treinar os modelos globais."}, status_code=400)

        target = resolve_target_patient(admin, patient_id=patient_id) or admin
        pers = trigger_continuous_learning(target["id"])

        return {
            "status": "ok",
            "message": "Modelos preditivos globais e pesos personalizados recalibrados com sucesso!",
            "model": {
                "best": res["best"],
                "metrics": res["metrics"].round(4).to_dict("records"),
                "importance": res["importance"],
                "hyperparameters": res.get("hyperparameters", {}),
                "personalized": pers,
                "model_rows": res["model_rows"],
            },
            "db_stats": get_database_stats(user_id=target["id"]),
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/synthetic")
@app.post("/api/synthetic")
def synthetic(
    cumulative: bool = False,
    days: int = 15,
    target_user_id: int | None = Query(None),
    admin: dict = Depends(require_admin),
):
    """Gera e testa dados sintéticos associando-os a um paciente de teste selecionado pelo ADM."""
    target = resolve_target_patient(admin, patient_id=target_user_id) or admin
    uid = target["id"]
    if not cumulative:
        clear_all_data(user_id=uid)

    syn_df = generate(days)
    filename = f"dados_sinteticos_{days}_dias_{target['username']}.csv"
    insert_readings_df(syn_df, user_id=uid, source="sintetico", filename=filename)

    train_global_models()
    trigger_continuous_learning(uid)

    res = package_for_user(target_user=target, viewer_user=admin, low=70.0, high=180.0)
    res["synthetic_message"] = f"Dados sintéticos ({days} dias) associados ao perfil '{target['full_name']}' (#{uid})."
    return res


@app.get("/api/export/synthetic")
def export_synthetic(
    days: int = 15,
    target_user_id: int | None = Query(None),
    admin: dict = Depends(require_admin),
):
    """Baixa CSV de dados sintéticos do paciente de teste ou gera amostra sintética."""
    if target_user_id is not None:
        df_u = get_readings_df(user_id=target_user_id)
        syn_sub = df_u[df_u["source"] == "sintetico"]
        if not syn_sub.empty:
            target = get_user_by_id(target_user_id)
            uname = target["username"] if target else str(target_user_id)
            csv_str = syn_sub[["timestamp", "glucose"]].to_csv(index=False)
            return Response(
                content=csv_str,
                media_type="text/csv",
                headers={"Content-Disposition": f"attachment; filename=dados_sinteticos_{uname}.csv"},
            )

    syn_df = generate(days)
    csv_str = syn_df[["timestamp", "glucose"]].to_csv(index=False)
    return Response(
        content=csv_str,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=dados_sinteticos_{days}_dias.csv"},
    )


@app.post("/api/dataset/reseed")
def reseed_dataset(
    patient_id: int | None = Query(None),
    admin: dict = Depends(require_admin),
):
    target = resolve_target_patient(admin, patient_id=patient_id) or admin
    clear_all_data(user_id=target["id"])
    seeded = seed_initial_data_if_empty(user_id=target["id"])
    if not seeded:
        return JSONResponse({"error": "Não foi possível semear com dados iniciais."}, status_code=400)
    train_global_models()
    trigger_continuous_learning(target["id"])
    return package_for_user(target_user=target, viewer_user=admin)


@app.post("/api/dataset/reset")
def reset_dataset(
    patient_id: int | None = Query(None),
    admin: dict = Depends(require_admin),
):
    target = resolve_target_patient(admin, patient_id=patient_id) or admin
    clear_all_data(user_id=target["id"])
    delete_patient_model_bundle(target["id"])
    train_global_models()
    return {
        "status": "ok",
        "message": f"Dados do perfil '{target['full_name']}' limpos com sucesso.",
    }


# Leituras glicêmicas, importação de CSV e validação de previsões

@app.get("/api/dataset/current")
def get_current_dataset(
    patient_id: int | None = Query(None),
    current_user: dict = Depends(get_current_user),
):
    target = resolve_target_patient(current_user, patient_id=patient_id)
    if target is None:
        return {
            "status": "no_patient_selected",
            "viewer": current_user,
            "target_user": None,
            "message": "Nenhum paciente vinculado à sua carteira ainda. Cadastre um paciente acima para iniciar o acompanhamento.",
            "db_stats": {"sync_label": "Sincronizado", "total_readings": 0},
        }
    return package_for_user(target_user=target, viewer_user=current_user)


@app.get("/api/database/stats")
def database_stats(
    patient_id: int | None = Query(None),
    current_user: dict = Depends(get_current_user),
):
    target = resolve_target_patient(current_user, patient_id=patient_id)
    if not target:
        return {"sync_label": "Sincronizado", "total_readings": 0}
    if current_user["role"] == ROLE_PACIENTE:
        return {"sync_label": "Sincronizado"}
    return get_database_stats(user_id=target["id"])


@app.post("/api/analyze")
async def analyze(
    files: list[UploadFile] = File(...),
    low: float = Form(70),
    high: float = Form(180),
    cumulative: bool = Form(True),
    patient_id: int | None = Form(None),
    current_user: dict = Depends(get_current_user),
):
    if not files:
        return JSONResponse({"error": "Nenhum arquivo CSV foi enviado."}, status_code=400)

    target = resolve_target_patient(current_user, patient_id=patient_id)
    if not target:
        return JSONResponse({"error": "Selecione um paciente válido."}, status_code=400)

    uid = target["id"]
    if not cumulative:
        clear_all_data(user_id=uid)

    total_inserted = 0
    for file in files:
        suffix = Path(file.filename or ".csv").suffix
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
            f.write(await file.read())
            path = f.name
        try:
            cur_df, _ = read_libre_csv(path)
            rows = insert_readings_df(cur_df, user_id=uid, source="libre_csv", filename=file.filename or "upload.csv")
            total_inserted += rows
        except Exception as e:
            return JSONResponse({"error": f"Erro no arquivo {file.filename}: {str(e)}"}, status_code=400)
        finally:
            Path(path).unlink(missing_ok=True)

    if total_inserted == 0 and get_database_stats(user_id=uid)["total_readings"] == 0:
        return JSONResponse({"error": "Nenhum dado válido encontrado nos arquivos enviados."}, status_code=400)

    train_global_models()
    trigger_continuous_learning(uid)

    return package_for_user(target_user=target, viewer_user=current_user, low=low, high=high)


@app.post("/api/manual/predict")
def manual_predict(
    current: float = Form(...),
    glucose_15: float | None = Form(None),
    glucose_30: float | None = Form(None),
    timestamp: str | None = Form(None),
    patient_id: int | None = Form(None),
    retrain: bool = Form(False),
    current_user: dict = Depends(get_current_user),
):
    if current < 20 or current > 600:
        return JSONResponse({"error": "A glicose atual deve estar entre 20 e 600 mg/dL."}, status_code=400)

    target = resolve_target_patient(current_user, patient_id=patient_id)
    if not target:
        return JSONResponse({"error": "Paciente alvo inválido."}, status_code=400)
    uid = target["id"]

    if timestamp:
        try:
            dt_current = pd.to_datetime(timestamp)
        except Exception:
            dt_current = datetime.now()
    else:
        dt_current = datetime.now()

    explicit_g15 = glucose_15 is not None and not pd.isna(glucose_15)
    explicit_g30 = glucose_30 is not None and not pd.isna(glucose_30)

    recent_prior = get_latest_readings(user_id=uid, n=5)
    eff_g15 = float(glucose_15) if explicit_g15 else (float(recent_prior[0]["glucose"]) if len(recent_prior) >= 1 else float(current))
    eff_g30 = float(glucose_30) if explicit_g30 else (float(recent_prior[1]["glucose"]) if len(recent_prior) >= 2 else eff_g15)

    if any(v < 20 or v > 600 for v in (current, eff_g15, eff_g30)):
        return JSONResponse({"error": "Os valores de glicose devem estar entre 20 e 600 mg/dL."}, status_code=400)

    # Salva a leitura principal e, se o usuário preencheu, também os valores de 15 e 30 min atrás
    ts_str = dt_current.strftime("%Y-%m-%d %H:%M:%S")
    reading_id = insert_single_reading(ts_str, current, user_id=uid, source="manual")

    if explicit_g15:
        dt_15 = dt_current - timedelta(minutes=15)
        insert_single_reading(dt_15.strftime("%Y-%m-%d %H:%M:%S"), eff_g15, user_id=uid, source="manual")
    if explicit_g30:
        dt_30 = dt_current - timedelta(minutes=30)
        insert_single_reading(dt_30.strftime("%Y-%m-%d %H:%M:%S"), eff_g30, user_id=uid, source="manual")

    if not ensure_global_models_ready():
        return JSONResponse({"error": "Modelos base ainda não disponíveis. Importe uma base de leituras primeiro."}, status_code=400)

    # Atualiza valores reais de previsões passadas e recalibra o modelo do paciente antes de prever
    update_prediction_actuals(user_id=uid)
    pers_info = trigger_continuous_learning(uid)

    prof = get_patient_profile(uid)
    pred_out = predict_for_patient(
        user_id=uid,
        current=current,
        glucose_15=eff_g15,
        glucose_30=eff_g30,
        global_models=GLOBAL_MODELS,
        timestamp_val=dt_current,
        metabolic_profile=prof,
    )

    p15 = pred_out["pred_15m"]
    p30 = pred_out["pred_30m"]
    m15_name = pred_out["model_label_15"]
    m30_name = pred_out["model_label_30"]

    pred_log_id = log_prediction(
        ts_str,
        current,
        p15,
        p30,
        user_id=uid,
        model_15m=m15_name,
        model_30m=m30_name,
    )

    retrained_global = False
    if retrain and current_user["role"] == ROLE_ADM:
        train_global_models()
        retrained_global = True

    logs = get_prediction_logs(user_id=uid, limit=25)
    active_log = next((item for item in logs if item["id"] == pred_log_id), logs[0] if logs else None)

    return {
        "status": "ok",
        "reading_id": reading_id,
        "prediction_id": pred_log_id,
        "active_prediction_log": active_log,
        "current": float(current),
        "glucose_15": eff_g15,
        "glucose_30": eff_g30,
        "timestamp": ts_str,
        "prediction_15m": p15,
        "prediction_30m": p30,
        "model_15": m15_name,
        "model_30": m30_name,
        "personalized_updated": pers_info is not None,
        "retrained": retrained_global,
        "manual_history": get_manual_readings(user_id=uid, limit=40),
        "prediction_logs": logs,
        "db_stats": {"sync_label": "Sincronizado"} if current_user["role"] == ROLE_PACIENTE else get_database_stats(user_id=uid),
    }


@app.put("/api/manual/reading/{reading_id}")
async def edit_manual_reading(
    reading_id: int,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Permite editar valor da glicose, data e hora/minuto/segundo de qualquer registro manual salvo."""
    rec = get_reading_by_id(reading_id)
    if not rec:
        return JSONResponse({"error": "Registro manual não encontrado."}, status_code=404)

    verify_can_manage_reading_or_log(current_user, rec["user_id"])

    try:
        data = await request.json()
    except Exception:
        data = {}

    try:
        new_glucose = float(data.get("glucose"))
        new_ts = str(data.get("timestamp") or "").strip()
        if not new_ts:
            return JSONResponse({"error": "Informe a data e horário válidos."}, status_code=400)

        updated = update_manual_reading(
            reading_id=reading_id,
            new_glucose=new_glucose,
            new_timestamp=new_ts,
        )
        if not updated:
            return JSONResponse({"error": "Não foi possível atualizar o registro."}, status_code=400)

        # Recalibrar modelo personalizado do paciente após correção retroativa
        trigger_continuous_learning(rec["user_id"])

        return {
            "status": "ok",
            "message": f"Registro #{reading_id} atualizado com sucesso!",
            "reading": updated,
            "manual_history": get_manual_readings(user_id=rec["user_id"], limit=40),
            "prediction_logs": get_prediction_logs(user_id=rec["user_id"], limit=25),
        }
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"Erro ao editar registro: {str(e)}"}, status_code=500)


@app.delete("/api/manual/reading/{reading_id}")
def delete_manual_reading(reading_id: int, current_user: dict = Depends(get_current_user)):
    rec = get_reading_by_id(reading_id)
    if not rec:
        return JSONResponse({"error": "Registro não encontrado."}, status_code=404)

    verify_can_manage_reading_or_log(current_user, rec["user_id"])
    owner_uid = rec["user_id"]

    success = delete_reading_by_id(reading_id)
    if not success:
        return JSONResponse({"error": "Falha ao excluir o registro."}, status_code=400)

    trigger_continuous_learning(owner_uid)

    return {
        "status": "ok",
        "message": "Registro excluído com sucesso.",
        "manual_history": get_manual_readings(user_id=owner_uid, limit=40),
        "prediction_logs": get_prediction_logs(user_id=owner_uid, limit=25),
    }


@app.post("/api/manual/clear")
def clear_manual(
    patient_id: int | None = Query(None),
    admin: dict = Depends(require_admin),
):
    target = resolve_target_patient(admin, patient_id=patient_id) or admin
    count = clear_manual_readings(user_id=target["id"])
    trigger_continuous_learning(target["id"])
    return {
        "status": "ok",
        "message": f"{count} registros manuais removidos.",
        "db_stats": get_database_stats(user_id=target["id"]),
    }


@app.post("/api/prediction/feedback")
async def prediction_feedback(
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Confirma ou remove a marcação de exatidão de uma previsão (+15 ou +30 min)."""
    try:
        data = await request.json()
    except Exception:
        data = {}

    pred_id = data.get("prediction_id")
    horizon = int(data.get("horizon") or 15)
    confirmed = bool(data.get("confirmed", True))

    if not pred_id:
        return JSONResponse({"error": "ID da previsão não informado."}, status_code=400)

    log_row = get_prediction_by_id(int(pred_id))
    if not log_row:
        return JSONResponse({"error": "Previsão não encontrada."}, status_code=404)

    verify_can_manage_reading_or_log(current_user, log_row["user_id"])
    owner_uid = log_row["user_id"]

    actor_label = f"{current_user['full_name'] or current_user['username']} ({current_user['role']})"
    updated_log = confirm_prediction_feedback(
        prediction_id=int(pred_id),
        horizon=horizon,
        confirmed=confirmed,
        actor_label=actor_label,
    )

    # Ajustar pesos do modelo personalizado com a confirmação clínica
    trigger_continuous_learning(owner_uid)

    logs = get_prediction_logs(user_id=owner_uid, limit=25)
    return {
        "status": "ok",
        "message": (
            f"Previsão de +{horizon} min marcada como Exata / Confirmada!"
            if confirmed
            else f"Confirmação de +{horizon} min removida."
        ),
        "updated_log": updated_log,
        "prediction_logs": logs,
    }


@app.get("/api/manual/history")
def manual_history(
    patient_id: int | None = Query(None),
    current_user: dict = Depends(get_current_user),
):
    target = resolve_target_patient(current_user, patient_id=patient_id)
    if not target:
        return {"history": []}
    return {"history": get_manual_readings(user_id=target["id"], limit=50)}


@app.get("/api/prediction/logs")
def prediction_logs_endpoint(
    patient_id: int | None = Query(None),
    current_user: dict = Depends(get_current_user),
):
    target = resolve_target_patient(current_user, patient_id=patient_id)
    if not target:
        return {"logs": []}
    return {"logs": get_prediction_logs(user_id=target["id"], limit=50)}


@app.get("/api/export/consolidated")
def export_consolidated(
    patient_id: int | None = Query(None),
    current_user: dict = Depends(get_current_user),
):
    target = resolve_target_patient(current_user, patient_id=patient_id)
    if not target:
        return JSONResponse({"error": "Paciente inválido."}, status_code=400)
    df = get_readings_df(user_id=target["id"])
    if df.empty:
        return JSONResponse({"error": "Nenhum dado consolidado no banco para este perfil."}, status_code=400)
    csv_str = df[["timestamp", "glucose", "source"]].to_csv(index=False)
    filename = f"cgm_{target['username']}_consolidado.csv"
    return Response(
        content=csv_str,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# Perfil metabólico, análise de dieta (Gemini) e cálculo de bolus

@app.get("/api/patient/profile")
def get_profile_endpoint(
    patient_id: int | None = Query(None),
    current_user: dict = Depends(get_current_user),
):
    target = resolve_target_patient(current_user, patient_id=patient_id)
    if not target:
        return {"status": "ok", "profile": None}
    return {"status": "ok", "profile": get_patient_profile(user_id=target["id"])}


@app.post("/api/patient/profile")
async def update_profile_endpoint(
    request: Request,
    current_user: dict = Depends(require_doctor_or_admin),
):
    """Gerenciamento de Dosagens Metabólicas (ICR, ISF, Glicose Alvo) pelo Médico ou ADM."""
    try:
        data = await request.json()
    except Exception:
        data = {}

    patient_id = data.get("patient_id")
    target = resolve_target_patient(current_user, patient_id=int(patient_id) if patient_id else None)
    if not target:
        return JSONResponse({"error": "Selecione um paciente da sua carteira."}, status_code=400)

    try:
        icr = float(data.get("icr", 8.0))
        isf = float(data.get("isf", 40.0))
        target_glucose = float(data.get("target_glucose", 100.0))
        clinical_notes = data.get("clinical_notes")

        updated = save_patient_profile(
            user_id=target["id"],
            icr=icr,
            isf=isf,
            target_glucose=target_glucose,
            clinical_notes=str(clinical_notes) if clinical_notes is not None else None,
            updated_by=f"{current_user['full_name'] or current_user['username']}",
        )
        trigger_continuous_learning(target["id"])
        return {
            "status": "ok",
            "message": f"Dosagens metabólicas de '{target['full_name']}' salvas com sucesso!",
            "profile": updated,
        }
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"Erro ao salvar perfil: {str(e)}"}, status_code=500)


@app.post("/api/diet/analyze")
async def analyze_diet(
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    content_type = request.headers.get("content-type", "")
    diet_text: str | None = None
    file_bytes: bytes | None = None
    mime_type: str | None = None
    filename: str | None = None
    patient_id_raw: Any = None

    if "multipart/form-data" in content_type:
        form = await request.form()
        diet_text = str(form.get("diet_text") or "") or None
        patient_id_raw = form.get("patient_id")
        upload_file = form.get("file")
        if upload_file and hasattr(upload_file, "read"):
            file_bytes = await upload_file.read()
            mime_type = getattr(upload_file, "content_type", None)
            filename = getattr(upload_file, "filename", None)
    elif "application/json" in content_type:
        try:
            body = await request.json()
            diet_text = body.get("diet_text") or body.get("text")
            patient_id_raw = body.get("patient_id")
        except Exception:
            diet_text = None

    if not diet_text and not file_bytes:
        return JSONResponse(
            {"error": "Envie um texto descrevendo a dieta ou faça upload de um arquivo (PDF, Imagem ou TXT)."},
            status_code=400,
        )

    target = resolve_target_patient(
        current_user,
        patient_id=int(patient_id_raw) if patient_id_raw not in (None, "", "null") else None,
    )

    try:
        result = analyze_diet_content(
            text=diet_text,
            file_bytes=file_bytes,
            mime_type=mime_type,
            filename=filename,
        )
        # Registrar automaticamente resumo da análise para o módulo de Revisão de Dietas do Médico
        if target and result and isinstance(result.get("data"), dict):
            refs = result["data"].get("refeicoes") or []
            for ref in refs[:3]:
                opcoes = ref.get("opcoes") or []
                if opcoes:
                    opt0 = opcoes[0]
                    log_diet_entry(
                        user_id=target["id"],
                        horario=str(ref.get("horario") or "12:00"),
                        meal_summary=f"{ref.get('nome_refeicao', 'Refeição')}: {opt0.get('descricao', '')}",
                        carbs_g=float(opt0.get("carboidratos_g") or 0.0),
                        glycemic_index=str(opt0.get("indice_glicemico_estimado") or "Médio"),
                    )
            result["diet_logs"] = get_diet_logs(user_id=target["id"], limit=20)
        return result
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/diet/logs")
def list_patient_diet_logs(
    patient_id: int | None = Query(None),
    current_user: dict = Depends(get_current_user),
):
    target = resolve_target_patient(current_user, patient_id=patient_id)
    if not target:
        return {"status": "ok", "diet_logs": []}
    return {"status": "ok", "diet_logs": get_diet_logs(user_id=target["id"], limit=25)}


@app.post("/api/diet/logs/{diet_id}/review")
async def doctor_review_diet(
    diet_id: int,
    request: Request,
    current_user: dict = Depends(require_doctor_or_admin),
):
    entry = get_diet_entry_by_id(diet_id)
    if not entry:
        return JSONResponse({"error": "Registro nutricional não encontrado."}, status_code=404)

    verify_can_manage_reading_or_log(current_user, int(entry["user_id"]))

    try:
        data = await request.json()
    except Exception:
        data = {}

    notes = str(data.get("doctor_notes") or "").strip()
    reviewer = f"{current_user['full_name'] or current_user['username']}"
    updated = review_diet_entry(diet_id=diet_id, doctor_notes=notes, reviewed_by=reviewer)
    return {
        "status": "ok",
        "message": "Parecer nutricional registrado com sucesso!",
        "entry": updated,
        "diet_logs": get_diet_logs(user_id=int(entry["user_id"]), limit=20),
    }


@app.post("/api/metabolic/calculate")
async def calculate_metabolic(
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    try:
        data = await request.json()
    except Exception:
        data = {}

    pat_id = data.get("patient_id")
    target = resolve_target_patient(current_user, patient_id=int(pat_id) if pat_id else None)
    if not target:
        return JSONResponse({"error": "Selecione um paciente válido."}, status_code=400)
    uid = target["id"]

    carbs_g = float(data.get("carbs_g") or 0.0)
    current_glucose = data.get("current_glucose")
    horario = str(data.get("horario") or "12:00")
    glycemic_index = str(data.get("glycemic_index") or "Médio")
    take_bolus = bool(data.get("take_bolus", True))
    meal_desc = str(data.get("meal_summary") or "").strip()

    profile = get_patient_profile(uid)
    # Paciente utiliza estritamente seu perfil prescrito; Médico/ADM podem simular ajustes
    if current_user["role"] == ROLE_PACIENTE:
        icr = float(profile.get("icr") or 8.0)
        isf = float(profile.get("isf") or 40.0)
        target_glucose = float(profile.get("target_glucose") or 100.0)
    else:
        icr = float(data.get("icr") or profile.get("icr") or 8.0)
        isf = float(data.get("isf") or profile.get("isf") or 40.0)
        target_glucose = float(data.get("target_glucose") or profile.get("target_glucose") or 100.0)

    if current_glucose is None or str(current_glucose).strip() == "":
        recent = get_latest_readings(user_id=uid, n=1)
        current_glucose = float(recent[0]["glucose"]) if recent else 110.0
    else:
        current_glucose = float(current_glucose)

    if current_glucose < 20 or current_glucose > 600:
        return JSONResponse({"error": "A glicose atual deve estar entre 20 e 600 mg/dL."}, status_code=400)

    try:
        bolus_info = calculate_total_bolus(
            carbs_g=carbs_g,
            current_glucose=current_glucose,
            icr=icr,
            isf=isf,
            target_glucose=target_glucose,
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    base_15m, base_30m = None, None
    if ensure_global_models_ready():
        try:
            recent_readings = get_latest_readings(user_id=uid, n=3)
            u_c = float(current_glucose)
            u_15 = float(recent_readings[0]["glucose"]) if len(recent_readings) >= 1 else u_c
            u_30 = float(recent_readings[1]["glucose"]) if len(recent_readings) >= 2 else u_15
            p_res = predict_for_patient(
                user_id=uid,
                current=u_c,
                glucose_15=u_15,
                glucose_30=u_30,
                global_models=GLOBAL_MODELS,
                metabolic_profile=profile,
            )
            base_15m = p_res["pred_15m"]
            base_30m = p_res["pred_30m"]
        except Exception:
            pass

    simulation = simulate_postprandial_curve(
        current_glucose=current_glucose,
        carbs_g=carbs_g,
        icr=icr,
        isf=isf,
        target_glucose=target_glucose,
        glycemic_index=glycemic_index,
        bolus_applied=bolus_info["bolus_total"] if take_bolus else 0.0,
        base_trend_15m=base_15m,
        base_trend_30m=base_30m,
    )

    if meal_desc or carbs_g > 0:
        log_diet_entry(
            user_id=uid,
            horario=horario,
            meal_summary=meal_desc or f"Cálculo de Bolus ({carbs_g}g CHO - IG {glycemic_index})",
            carbs_g=carbs_g,
            glycemic_index=glycemic_index,
            current_glucose=current_glucose,
            bolus_total=bolus_info["bolus_total"],
        )

    return {
        "status": "ok",
        "horario": horario,
        "carbs_g": carbs_g,
        "current_glucose": current_glucose,
        "glycemic_index": glycemic_index,
        "bolus": bolus_info,
        "simulation": simulation,
        "diet_logs": get_diet_logs(user_id=uid, limit=20),
        "patient_profile": {
            "icr": icr,
            "isf": isf,
            "target_glucose": target_glucose,
        },
    }


# Mensagens diretas entre médico e paciente

def _validate_chat_pair(current_user: dict, peer_id: int) -> dict:
    peer = get_user_by_id(peer_id)
    if not peer:
        raise HTTPException(status_code=404, detail="Destinatário não encontrado.")

    role = current_user["role"]
    if role == ROLE_PACIENTE:
        if current_user.get("doctor_id") != peer["id"] or peer["role"] != ROLE_MEDICO:
            raise HTTPException(
                status_code=403,
                detail="Como paciente, você só pode trocar mensagens com o médico ao qual está vinculado.",
            )
        return peer

    if role == ROLE_MEDICO:
        if peer["role"] != ROLE_PACIENTE or peer.get("doctor_id") != current_user["id"]:
            raise HTTPException(
                status_code=403,
                detail="Você só pode trocar mensagens com pacientes vinculados à sua carteira.",
            )
        return peer

    raise HTTPException(status_code=403, detail="O chat clínico direto é exclusivo entre Médico e Paciente.")


@app.get("/api/messages/contacts")
def get_message_contacts(current_user: dict = Depends(get_current_user)):
    role = current_user["role"]
    unread_map = get_unread_counts_for_user(current_user["id"])

    if role == ROLE_PACIENTE:
        doc_id = current_user.get("doctor_id")
        if not doc_id:
            return {"status": "ok", "contacts": [], "unread_total": 0}
        doc = get_user_by_id(int(doc_id))
        if not doc:
            return {"status": "ok", "contacts": [], "unread_total": 0}
        contacts = [{
            "id": doc["id"],
            "full_name": doc["full_name"] or doc["username"],
            "username": doc["username"],
            "role": doc["role"],
            "crm": doc.get("crm"),
            "unread": unread_map.get(doc["id"], 0),
        }]
        return {"status": "ok", "contacts": contacts, "unread_total": sum(unread_map.values())}

    if role == ROLE_MEDICO:
        patients = list_all_users(role_filter=ROLE_PACIENTE, doctor_id_filter=current_user["id"])
        contacts = [{
            "id": p["id"],
            "full_name": p["full_name"] or p["username"],
            "username": p["username"],
            "role": p["role"],
            "last_glucose": p.get("last_glucose"),
            "unread": unread_map.get(p["id"], 0),
        } for p in patients]
        return {"status": "ok", "contacts": contacts, "unread_total": sum(unread_map.values())}

    return {"status": "ok", "contacts": [], "unread_total": 0}


@app.get("/api/messages/{peer_id}")
def get_chat_history(peer_id: int, current_user: dict = Depends(get_current_user)):
    peer = _validate_chat_pair(current_user, peer_id)
    msgs = get_messages_between(
        user_a=current_user["id"],
        user_b=peer["id"],
        limit=100,
        mark_read_for=current_user["id"],
    )
    unread_map = get_unread_counts_for_user(current_user["id"])
    return {
        "status": "ok",
        "peer": peer,
        "messages": msgs,
        "unread_total": sum(unread_map.values()),
    }


@app.post("/api/messages")
async def post_chat_message(request: Request, current_user: dict = Depends(get_current_user)):
    try:
        data = await request.json()
    except Exception:
        data = {}

    peer_id = data.get("receiver_id")
    message_text = str(data.get("message") or "").strip()

    if not peer_id:
        return JSONResponse({"error": "Selecione o destinatário da mensagem."}, status_code=400)
    if not message_text:
        return JSONResponse({"error": "Digite uma mensagem antes de enviar."}, status_code=400)

    peer = _validate_chat_pair(current_user, int(peer_id))
    try:
        saved = send_direct_message(
            sender_id=current_user["id"],
            receiver_id=peer["id"],
            message=message_text,
        )
        msgs = get_messages_between(
            user_a=current_user["id"],
            user_b=peer["id"],
            limit=100,
            mark_read_for=current_user["id"],
        )
        return {
            "status": "ok",
            "sent": saved,
            "messages": msgs,
        }
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
