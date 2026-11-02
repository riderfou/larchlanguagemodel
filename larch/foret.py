"""Zones de mélèzes de la BD Forêt® V2 (IGN), pour la carte.

La BD Forêt V2 ne distingue le mélèze que pour les peuplements purs :
code TFV « FF2-63-63 » (forêt fermée de mélèze pur, ≥ 75 % du couvert). Les
mélézins mélangés sont rangés dans « mélange de conifères » (FF2-00-00) ou
« forêt ouverte de conifères » (FO2), sans précision d'essence.

Les ~4 800 polygones de France sont téléchargés une fois par le WFS de la
Géoplateforme, simplifiés (quelques mètres) et enregistrés en GeoJSON : la carte
n'a ainsi pas besoin d'interroger l'IGN à chaque visite.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import requests

WFS_URL = "https://data.geopf.fr/wfs/ows"
TYPENAME = "LANDCOVER.FORESTINVENTORY.V2:formation_vegetale"
LARCH_TFV = "FF2-63-63"
PAGE = 1000
EARTH_R = 6371008.8


def download(code_tfv: str = LARCH_TFV, timeout: int = 300) -> list[dict]:
    """Toutes les entités du code TFV donné, en GeoJSON lon/lat (EPSG:4326)."""
    feats: list[dict] = []
    start = 0
    while True:
        r = requests.get(WFS_URL, timeout=timeout, params={
            "SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature",
            "TYPENAMES": TYPENAME, "OUTPUTFORMAT": "application/json",
            "SRSNAME": "EPSG:4326", "CQL_FILTER": f"code_tfv='{code_tfv}'",
            "SORTBY": "id", "COUNT": PAGE, "STARTINDEX": start,
        })
        r.raise_for_status()
        page = r.json().get("features", [])
        feats.extend(page)
        if len(page) < PAGE:
            return feats
        start += PAGE


# --- Simplification et surface ---------------------------------------------

def _to_xy(ring: list[list[float]], lat0: float) -> list[tuple[float, float]]:
    """Projection équirectangulaire locale (mètres), suffisante à l'échelle d'un polygone."""
    k = math.cos(math.radians(lat0))
    return [(math.radians(lon) * EARTH_R * k, math.radians(lat) * EARTH_R) for lon, lat in ring]


def _rdp(pts: list[tuple[float, float]], tol: float) -> list[int]:
    """Indices conservés par Ramer-Douglas-Peucker (itératif)."""
    n = len(pts)
    if n < 3:
        return list(range(n))
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        (x1, y1), (x2, y2) = pts[i], pts[j]
        dx, dy = x2 - x1, y2 - y1
        norm = math.hypot(dx, dy)
        best, idx = -1.0, -1
        for k in range(i + 1, j):
            x, y = pts[k]
            d = (abs(dy * x - dx * y + x2 * y1 - y2 * x1) / norm) if norm else math.hypot(x - x1, y - y1)
            if d > best:
                best, idx = d, k
        if best > tol:
            keep[idx] = True
            stack += [(i, idx), (idx, j)]
    return [k for k in range(n) if keep[k]]


def _area(xy: list[tuple[float, float]]) -> float:
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(xy, xy[1:] + xy[:1]))) / 2


def simplify_ring(ring: list[list[float]], tol_m: float, ndigits: int = 5) -> list[list[float]] | None:
    lat0 = sum(p[1] for p in ring) / len(ring)
    xy = _to_xy(ring, lat0)
    if len(ring) > 1 and ring[0] == ring[-1]:
        # Anneau fermé : on simplifie en coupant au point le plus éloigné du départ
        # (sinon RDP n'a qu'un segment nul comme référence).
        far = max(range(len(xy)), key=lambda k: (xy[k][0] - xy[0][0]) ** 2 + (xy[k][1] - xy[0][1]) ** 2)
        a = _rdp(xy[: far + 1], tol_m)
        b = [far + k for k in _rdp(xy[far:], tol_m)]
        idx = a + b[1:]
    else:
        idx = _rdp(xy, tol_m)
    out = []
    for k in idx:
        p = [round(ring[k][0], ndigits), round(ring[k][1], ndigits)]
        if not out or p != out[-1]:
            out.append(p)
    if len(out) < 4:
        return None
    if out[0] != out[-1]:
        out.append(out[0])
    return out


def simplify_feature(feat: dict, tol_m: float) -> dict | None:
    geom = feat["geometry"]
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    out_polys, area = [], 0.0
    for poly in polys:
        rings = []
        for n, ring in enumerate(poly):
            lat0 = sum(p[1] for p in ring) / len(ring)
            a = _area(_to_xy(ring[:-1] if ring[0] == ring[-1] else ring, lat0))
            area += -a if n else a
            s = simplify_ring(ring, tol_m)
            if s:
                rings.append(s)
            elif n == 0:
                break  # contour extérieur dégénéré : on laisse tomber ce polygone
        if rings:
            out_polys.append(rings)
    if not out_polys:
        return None
    return {
        "type": "Feature",
        "properties": {"id": feat["properties"].get("id"), "ha": round(area / 10_000, 1)},
        "geometry": {"type": "MultiPolygon", "coordinates": out_polys},
    }


def build(out: Path, tol_m: float = 8.0) -> dict:
    """Télécharge, simplifie et écrit le GeoJSON ; renvoie un petit résumé."""
    raw = download()
    feats = [f for f in (simplify_feature(f, tol_m) for f in raw) if f]
    fc = {
        "type": "FeatureCollection",
        "name": "meleze_bdforet_v2",
        "source": ("IGN – BD Forêt® V2, formations végétales de code TFV FF2-63-63 "
                   "(forêt fermée de mélèze pur), Licence Ouverte Etalab 2.0"),
        "simplification_m": tol_m,
        "features": feats,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(fc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return {"features": len(feats), "downloaded": len(raw),
            "hectares": round(sum(f["properties"]["ha"] for f in feats)),
            "bytes": out.stat().st_size}
