"""Historique des mesures (CSV) et agrégation journalière."""
from __future__ import annotations

import csv
import statistics
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .analysis import ColorStats

FIELDS = [
    "timestamp", "cam", "valid", "reason", "gold_index", "green", "yellow",
    "orange", "brown", "veg_share", "gcc", "rcc", "mean_rgb", "brightness",
]


def append(path: Path, cam_id: str, ts: datetime, stats: ColorStats) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    d = stats.as_dict()
    row = {k: d.get(k, "") for k in FIELDS}
    row.update(timestamp=ts.isoformat(timespec="minutes"), cam=cam_id, valid=int(stats.valid))
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


@dataclass
class DailyPoint:
    day: date
    gold_index: float
    veg_share: float
    gcc: float
    mean_rgb: str
    n: int


def daily(rows: list[dict], cam_id: str, hours: tuple[int, int] = (0, 24)) -> list[DailyPoint]:
    """Médiane journalière des mesures valides prises dans la plage horaire."""
    by_day: dict[date, list[dict]] = {}
    for r in rows:
        if r["cam"] != cam_id or r["valid"] not in ("1", 1, True):
            continue
        ts = datetime.fromisoformat(r["timestamp"])
        if not (hours[0] <= ts.hour < hours[1]):
            continue
        by_day.setdefault(ts.date(), []).append(r)
    out = []
    for d in sorted(by_day):
        rs = by_day[d]
        gi = [float(r["gold_index"]) for r in rs]
        med = statistics.median(gi)
        closest = min(rs, key=lambda r: abs(float(r["gold_index"]) - med))
        out.append(DailyPoint(
            day=d,
            gold_index=round(med, 1),
            veg_share=round(statistics.median(float(r["veg_share"]) for r in rs), 3),
            gcc=round(statistics.median(float(r["gcc"]) for r in rs), 4),
            mean_rgb=closest["mean_rgb"],
            n=len(rs),
        ))
    return out


LOW_VEG = "trop peu de végétation"


def bare_days(rows: list[dict], cam_id: str, hours: tuple[int, int] = (0, 24)) -> list[date]:
    """Jours dont toutes les images de la plage horaire montrent peu de végétation
    colorée (aiguilles tombées, ou neige) : signe de fin de saison après un pic."""
    status: dict[date, bool] = {}
    for r in rows:
        if r["cam"] != cam_id:
            continue
        ts = datetime.fromisoformat(r["timestamp"])
        if not (hours[0] <= ts.hour < hours[1]):
            continue
        bare = r["valid"] not in ("1", 1, True) and r["reason"].startswith(LOW_VEG)
        status[ts.date()] = status.get(ts.date(), True) and bare
    return sorted(d for d, b in status.items() if b)
