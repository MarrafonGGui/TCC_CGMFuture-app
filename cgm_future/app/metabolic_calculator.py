from __future__ import annotations
from typing import Dict, Any, Optional


def calculate_food_bolus(carbs_g: float, icr: float) -> float:
    # Bolus para cobrir os carboidratos da refeição: CHO (g) / ICR (g/UI)
    if icr <= 0:
        raise ValueError("A Razão Carboidrato/Insulina (ICR) deve ser maior que zero.")
    if carbs_g < 0:
        raise ValueError("A quantidade de carboidratos não pode ser negativa.")

    return round(float(carbs_g) / float(icr), 2)


def calculate_correction_bolus(current_glucose: float, target_glucose: float, isf: float) -> float:
    # Correção entra apenas quando a glicemia atual está acima do alvo
    if isf <= 0:
        raise ValueError("O Fator de Sensibilidade à Insulina (ISF) deve ser maior que zero.")
    if current_glucose < 0 or target_glucose < 0:
        raise ValueError("Os valores glicêmicos não podem ser negativos.")

    delta = float(current_glucose) - float(target_glucose)
    if delta > 0:
        return round(delta / float(isf), 2)
    return 0.0


def calculate_total_bolus(
    carbs_g: float,
    current_glucose: float,
    icr: float,
    isf: float,
    target_glucose: float = 100.0
) -> Dict[str, Any]:
    """Soma bolus alimentar e de correção e monta o resumo clínico para exibição."""
    bolus_cho = calculate_food_bolus(carbs_g, icr)
    bolus_corr = calculate_correction_bolus(current_glucose, target_glucose, isf)
    bolus_total = round(bolus_cho + bolus_corr, 2)

    if current_glucose < 70.0:
        status_glicemico = "Hipoglicemia"
        alerta_clinico = "Atenção: glicose abaixo de 70 mg/dL. Corrija a hipoglicemia com 15g de carboidratos de absorção rápida antes de aplicar insulina (Regra dos 15)."
    elif current_glucose > 180.0:
        status_glicemico = "Hiperglicemia"
        alerta_clinico = "Glicose acima do limite clínico (180 mg/dL). Bolus de correção incluído para retorno à faixa alvo."
    elif current_glucose > target_glucose:
        status_glicemico = "Leve elevação pré-prandial"
        alerta_clinico = f"Glicose um pouco acima do alvo ({target_glucose} mg/dL). Ajuste fino de correção incluído."
    else:
        status_glicemico = "No Alvo (Normoglicemia)"
        alerta_clinico = "Glicose dentro da faixa esperada. Apenas o bolus da refeição é necessário."

    return {
        "carbs_g": float(carbs_g),
        "current_glucose": float(current_glucose),
        "target_glucose": float(target_glucose),
        "icr": float(icr),
        "isf": float(isf),
        "bolus_cho": bolus_cho,
        "bolus_correction": bolus_corr,
        "bolus_total": bolus_total,
        "status_glicemico": status_glicemico,
        "alerta_clinico": alerta_clinico,
        "formulas": {
            "bolus_cho_formula": f"{carbs_g}g / {icr} (ICR) = {bolus_cho} UI",
            "bolus_corr_formula": f"max(0, ({current_glucose} - {target_glucose}) / {isf} (ISF)) = {bolus_corr} UI",
            "bolus_total_formula": f"{bolus_cho} UI + {bolus_corr} UI = {bolus_total} UI"
        }
    }


def simulate_postprandial_curve(
    current_glucose: float,
    carbs_g: float,
    icr: float,
    isf: float,
    target_glucose: float = 100.0,
    glycemic_index: str = "Médio",
    bolus_applied: Optional[float] = None,
    base_trend_15m: Optional[float] = None,
    base_trend_30m: Optional[float] = None
) -> Dict[str, Any]:
    """Projeta a curva glicêmica pós-prandial (0 a 60 min, de 15 em 15 min) com e sem bolus."""
    if bolus_applied is None:
        calc = calculate_total_bolus(carbs_g, current_glucose, icr, isf, target_glucose)
        bolus_applied = calc["bolus_total"]

    # Cada grama de CHO eleva a glicose em cerca de (ISF / ICR) mg/dL
    carb_impact_factor = float(isf) / float(icr) if icr > 0 else 4.0
    total_carb_excursion = carbs_g * carb_impact_factor

    # Fração acumulada de absorção nos tempos [0, 15, 30, 45, 60] min segundo o IG
    gi_clean = str(glycemic_index or "Médio").strip().capitalize()
    if gi_clean == "Alto":
        carb_fraction = [0.0, 0.35, 0.70, 0.90, 0.95]
    elif gi_clean == "Baixo":
        carb_fraction = [0.0, 0.12, 0.30, 0.50, 0.68]
    else:
        carb_fraction = [0.0, 0.22, 0.50, 0.75, 0.88]

    # Fração estimada de ação da insulina ultrarrápida na primeira hora
    insulin_fraction = [0.0, 0.08, 0.25, 0.45, 0.65]

    time_points = [0, 15, 30, 45, 60]
    labels = ["Agora (0 min)", "+15 min", "+30 min", "+45 min", "+60 min"]

    curve_base = []
    curve_no_insulin = []
    curve_with_bolus = []

    if base_trend_15m is not None and base_trend_30m is not None:
        slope = (base_trend_30m - current_glucose) / 30.0
    else:
        slope = 0.0

    # Trava a inclinação basal para evitar projeções irreais
    slope = max(-0.8, min(0.8, slope))

    for idx, t in enumerate(time_points):
        g_base = current_glucose + (slope * t)
        g_carb = total_carb_excursion * carb_fraction[idx]
        g_insulin = bolus_applied * isf * insulin_fraction[idx]

        val_no_ins = round(max(20.0, min(500.0, g_base + g_carb)), 1)
        val_with_ins = round(max(20.0, min(500.0, g_base + g_carb - g_insulin)), 1)

        curve_base.append(round(g_base, 1))
        curve_no_insulin.append(val_no_ins)
        curve_with_bolus.append(val_with_ins)

    return {
        "time_points": time_points,
        "labels": labels,
        "curve_no_insulin": curve_no_insulin,
        "curve_with_bolus": curve_with_bolus,
        "target_glucose_line": [round(float(target_glucose), 1)] * len(time_points),
        "target_low": 70.0,
        "target_high": 180.0,
        "carb_impact_factor_mg_dl_per_g": round(carb_impact_factor, 2),
        "total_carb_excursion_mg_dl": round(total_carb_excursion, 1),
        "bolus_applied": bolus_applied,
        "glycemic_index": gi_clean
    }
