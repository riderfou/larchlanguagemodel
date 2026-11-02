"""Chargement de la configuration (cams.yaml)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

Polygon = list[tuple[float, float]]


@dataclass
class ForecastSettings:
    start_month_day: str = "08-01"
    base_temp: float = 10.0
    peak_threshold: float = 45.0


@dataclass
class Settings:
    daylight_hours: tuple[int, int] = (10, 15)
    timezone: str = "Europe/Paris"
    forecast: ForecastSettings = field(default_factory=ForecastSettings)


@dataclass
class Cam:
    id: str
    name: str
    lat: float
    lon: float
    elevation: float
    source: dict[str, Any]
    roi: list[Polygon] = field(default_factory=list)
    # Seuil propre à la webcam (surcharge settings.forecast.peak_threshold),
    # typiquement produit par `larch calibrate`.
    peak_threshold: float | None = None


@dataclass
class Config:
    cams: list[Cam]
    settings: Settings
    # Suivi Sentinel-2 : {"radius_km": 4, "zones": [{id, name, lat, lon, radius_km?}, ...]}
    satellite: dict[str, Any] = field(default_factory=dict)

    def cam(self, cam_id: str) -> Cam:
        for c in self.cams:
            if c.id == cam_id:
                return c
        raise KeyError(f"webcam inconnue : {cam_id}")


def _parse_roi(raw: Any) -> list[Polygon]:
    if not raw:
        return []
    polys: list[Polygon] = []
    for poly in raw:
        pts = [(float(x), float(y)) for x, y in poly]
        if len(pts) < 3:
            raise ValueError(f"polygone ROI invalide (moins de 3 points) : {poly}")
        for x, y in pts:
            if not (0 <= x <= 1 and 0 <= y <= 1):
                raise ValueError(f"coordonnées ROI hors de [0,1] : {poly}")
        polys.append(pts)
    return polys


def load_config(path: str | Path = "cams.yaml") -> Config:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    cams = []
    seen = set()
    for c in data.get("cams", []):
        cam = Cam(
            id=str(c["id"]),
            name=str(c.get("name", c["id"])),
            lat=float(c["lat"]),
            lon=float(c["lon"]),
            elevation=float(c.get("elevation", 1800)),
            source=dict(c["source"]),
            roi=_parse_roi(c.get("roi")),
            peak_threshold=c.get("peak_threshold"),
        )
        if cam.id in seen:
            raise ValueError(f"id de webcam en double : {cam.id}")
        seen.add(cam.id)
        cams.append(cam)

    s = data.get("settings", {}) or {}
    f = s.get("forecast", {}) or {}
    settings = Settings(
        daylight_hours=tuple(s.get("daylight_hours", (10, 15))),
        timezone=s.get("timezone", "Europe/Paris"),
        forecast=ForecastSettings(
            start_month_day=str(f.get("start_month_day", "08-01")),
            base_temp=float(f.get("base_temp", 10.0)),
            peak_threshold=float(f.get("peak_threshold", 45.0)),
        ),
    )
    return Config(cams=cams, settings=settings, satellite=dict(data.get("satellite") or {}))
