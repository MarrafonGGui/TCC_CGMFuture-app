import numpy as np
import pandas as pd


def generate(days=15, start="2026-01-01"):
    # Gera série sintética para testes com ritmo circadiano e três picos pós-prandiais por dia
    rng = np.random.default_rng(42)
    n = days * 96
    t = pd.date_range(start, periods=n, freq="15min")
    h = t.hour + t.minute / 60
    g = 115 + 12 * np.sin(2 * np.pi * (h - 6) / 24) + rng.normal(0, 6, n)
    minute = np.arange(n) * 15
    for day in range(days):
        for hour, amp in [(8, 55), (13, 70), (19, 65)]:
            center = day * 1440 + hour * 60
            g += amp * np.exp(-((minute - center) / 75) ** 2)
    return pd.DataFrame({"timestamp": t, "glucose": np.clip(g, 55, 350)})
