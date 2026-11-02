"""Stade de coloration et prévision de la date de pic."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

from .store import DailyPoint

STAGES = {
    "inconnu": ("Pas de mesure", "#9aa0a6"),
    "vert": ("Encore vert", "#3f8f3a"),
    "debut": ("Début de coloration", "#a7b53a"),
    "en_cours": ("Coloration en cours", "#e0b42a"),
    "pic": ("Pic des couleurs", "#f08c00"),
    "declin": ("Déclin – chute des aiguilles", "#8d6e57"),
}


def smooth(points: list[DailyPoint], k: int = 3) -> list[float]:
    """Médiane glissante centrée (atténue nuages, reflets, neige ponctuelle)."""
    v = [p.gold_index for p in points]
    h = k // 2
    return [float(np.median(v[max(0, i - h): i + h + 1])) for i in range(len(v))]


def stage(points: list[DailyPoint], bare: list[date] | None = None) -> str:
    """`bare` : jours sans végétation colorée visible (cf. store.bare_days)."""
    if not points:
        return "inconnu"
    g = smooth(points)
    i_max = int(np.argmax(g))
    peak, last = g[i_max], g[-1]
    peak_veg = points[i_max].veg_share
    after = [d for d in (bare or []) if d > points[-1].day]
    if peak >= 55 and len(after) >= 2:
        return "declin"
    if peak >= 55 and i_max < len(g) - 1 and (
        last <= peak - 15 or points[-1].veg_share <= 0.6 * peak_veg
    ):
        return "declin"
    if last >= 70:
        return "pic"
    if last >= 35:
        return "en_cours"
    if last >= 10:
        return "debut"
    return "vert"


def observed_peak(points: list[DailyPoint], bare: list[date] | None = None) -> date | None:
    """Date du pic, seulement s'il est confirmé (déclin amorcé)."""
    if stage(points, bare) != "declin":
        return None
    g = smooth(points)
    return points[int(np.argmax(g))].day


# --------------------------------------------------------------------------
# 1. Ajustement d'une courbe logistique sur les observations
# --------------------------------------------------------------------------

@dataclass
class LogisticFit:
    base: float
    amplitude: float
    t0: date        # date de mi-coloration
    scale: float    # jours
    peak: date      # 90 % de la coloration atteinte
    peak_min: date
    peak_max: date
    rmse: float

    def predict(self, d: date) -> float:
        x = (d - self.t0).days / self.scale
        return self.base + self.amplitude / (1 + math.exp(-x))


PEAK_FRACTION_X = math.log(0.9 / 0.1)  # sigmoïde à 90 % : x = ln(9) ≈ 2.2


def fit_logistic(points: list[DailyPoint]) -> LogisticFit | None:
    if len(points) < 5:
        return None
    # On n'ajuste que la phase montante (jusqu'au maximum lissé).
    g_s = smooth(points)
    pts = points[: int(np.argmax(g_s)) + 1]
    if len(pts) < 5:
        pts = points
    d0 = pts[0].day
    t = np.array([(p.day - d0).days for p in pts], dtype=float)
    y = np.array([p.gold_index for p in pts], dtype=float)
    if t[-1] - t[0] < 5 or y.max() - y.min() < 15:
        return None

    results = []
    for t0 in np.arange(t[0] - 10, t[-1] + 45, 0.5):
        for s in np.arange(1.0, 15.01, 0.5):
            x = 1 / (1 + np.exp(-(t - t0) / s))
            A = np.vstack([np.ones_like(x), x]).T
            (b, c), *_ = np.linalg.lstsq(A, y, rcond=None)
            if c <= 5 or b < -5 or b + c > 105:
                continue
            sse = float(((A @ [b, c] - y) ** 2).sum())
            results.append((sse, t0, s, b, c))
    if not results:
        return None
    results.sort()
    sse, t0, s, b, c = results[0]
    n = len(t)
    tol = sse * (1 + 4.0 / max(n - 4, 1)) + n * 4.0  # ~ ±2 points d'indice
    peaks = [r[1] + PEAK_FRACTION_X * r[2] for r in results if r[0] <= tol]
    day = lambda v: d0 + timedelta(days=int(round(v)))  # noqa: E731
    return LogisticFit(
        base=round(b, 1), amplitude=round(c, 1), t0=day(t0), scale=s,
        peak=day(t0 + PEAK_FRACTION_X * s), peak_min=day(min(peaks)), peak_max=day(max(peaks)),
        rmse=round(math.sqrt(sse / n), 1),
    )


# --------------------------------------------------------------------------
# 2. Modèle « froid cumulé » (degrés-jours sous un seuil)
# --------------------------------------------------------------------------
# La sénescence du mélèze est déclenchée par la baisse de la photopériode
# puis accélérée par le froid. Modèle volontairement simple (dans l'esprit de
# Delpierre et al. 2009) : à partir d'une date fixe (proxy de la photopériode),
# on cumule max(0, Tbase - Tmoy) ; le pic survient quand le cumul atteint un
# seuil. Le seuil par défaut est indicatif : `larch calibrate` l'ajuste sur les
# pics observés par vos webcams.

@dataclass
class ColdModel:
    accumulated: float
    threshold: float
    peak: date | None
    extrapolated: bool  # la date dépend de la tendance au-delà de J+15

    @property
    def progress(self) -> float:
        return min(1.0, self.accumulated / self.threshold) if self.threshold else 0.0


def season_start(today: date, start_month_day: str) -> date:
    m, d = (int(x) for x in start_month_day.split("-"))
    s = date(today.year, m, d)
    return s if s <= today else date(today.year - 1, m, d)


def cold_sum(temps: dict[date, float], start: date, end: date, base: float) -> float:
    return sum(max(0.0, base - t) for d, t in temps.items() if start <= d <= end)


def cold_model(temps: dict[date, float], start: date, today: date, base: float,
               threshold: float, forecast_end: date | None = None) -> ColdModel:
    acc = cold_sum(temps, start, today, base)
    s = 0.0
    peak = None
    for d in sorted(temps):
        if d < start:
            continue
        s += max(0.0, base - temps[d])
        if s >= threshold:
            peak = d
            break
    extrapolated = bool(peak and forecast_end and peak > forecast_end)
    return ColdModel(round(acc, 1), threshold, peak, extrapolated)
