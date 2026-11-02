"""Températures journalières (Open-Meteo, sans clé) à l'altitude des mélèzes."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import requests

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
TIMEOUT = 30

# Refroidissement automnal moyen en montagne (°C/jour) pour prolonger la
# prévision au-delà de l'horizon d'Open-Meteo (16 jours).
SEASONAL_COOLING = 0.15


def _query(url: str, lat: float, lon: float, elev: float, **params) -> dict[str, float]:
    r = requests.get(url, params={
        "latitude": lat, "longitude": lon, "elevation": elev,
        "daily": "temperature_2m_mean", "timezone": "Europe/Paris", **params,
    }, timeout=TIMEOUT)
    r.raise_for_status()
    j = r.json()
    if j.get("error"):
        raise RuntimeError(j.get("reason", "erreur Open-Meteo"))
    d = j["daily"]
    return {t: v for t, v in zip(d["time"], d["temperature_2m_mean"]) if v is not None}


def daily_mean_temps(lat: float, lon: float, elev: float, start: date, today: date,
                     cache: Path | None = None) -> dict[date, float]:
    """Températures moyennes du `start` jusqu'à today+15 (observées puis prévues)."""
    temps: dict[str, float] = {}
    if cache and cache.exists():
        temps.update(json.loads(cache.read_text()))

    # Archive (réanalyse ERA5, ~5 jours de retard) pour le passé non encore en cache.
    archive_end = today - timedelta(days=6)
    missing_old = [start + timedelta(days=i) for i in range((archive_end - start).days + 1)
                   if (start + timedelta(days=i)).isoformat() not in temps]
    if missing_old:
        try:
            temps.update(_query(ARCHIVE_URL, lat, lon, elev,
                                start_date=min(missing_old).isoformat(),
                                end_date=max(missing_old).isoformat()))
        except Exception:  # noqa: BLE001 - la prévision couvre 92 jours en arrière
            pass

    # Prévision : passé récent + 16 jours ; prioritaire sur le cache pour ces dates.
    past = min(92, max(7, (today - start).days + 1))
    temps.update(_query(FORECAST_URL, lat, lon, elev, past_days=past, forecast_days=16))

    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(dict(sorted(temps.items())), indent=0))
    return {date.fromisoformat(k): v for k, v in temps.items() if date.fromisoformat(k) >= start}


def extend(temps: dict[date, float], until: date) -> dict[date, float]:
    """Prolonge la série au-delà de la prévision par une tendance saisonnière."""
    if not temps:
        return temps
    out = dict(temps)
    last = max(out)
    recent = [out[last - timedelta(days=i)] for i in range(7) if last - timedelta(days=i) in out]
    base = sum(recent) / len(recent)
    d, k = last, 0
    while d < until:
        d += timedelta(days=1)
        k += 1
        out[d] = base - SEASONAL_COOLING * k
    return out
