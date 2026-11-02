"""Suivi de la coloration des mélézins par Sentinel-2, en complément des webcams.

La carte des mélézins (Sentinel-2 + BD Forêt) sert de masque. À chaque passage
(tous les 2 à 3 jours avec les trois satellites), on ne lit que la fenêtre de
chaque zone suivie (quelques centaines de pixels de 20 m) et on calcule, sur les
pixels de mélèze sans nuage ni neige, un **indice d'or satellite** :

    jaunissement d'un pixel :  (rouge-vert)/(rouge+vert) du jour
                               − la même valeur sur une image d'été
    indice :  jaunissement médian du mélèze
              − jaunissement médian des conifères persistants (témoins)
              ramené à 0-100 par `GOLD_DELTA` (100 = mélèze entièrement doré)

La référence d'été propre à chaque pixel neutralise relief, exposition et sol.
Les témoins (épicéas, sapins, pins de la BD Forêt, dans la même zone et sur la
même image) neutralisent ce qui change pour toute la forêt : soleil plus bas en
automne, ombres, atmosphère. Sans eux, une pessière « jaunirait » presque
autant qu'un mélézin sur les images d'octobre.
La « part de végétation » (pour repérer la chute des aiguilles) est le NDVI du
mélèze rapporté à son NDVI d'été, corrigé de la même façon par les témoins.

Les mesures sont écrites dans un historique au même format que celui des
webcams (`data/satellite_history.csv`, identifiant `s2:<zone>`), si bien que
stades, lissage, ajustement logistique et date de pic sont calculés exactement
comme pour les webcams.

Zones : une par webcam (mélèze à moins de `radius_km`), plus des massifs sans
webcam déclarés dans `cams.yaml` (clé `satellite.zones`).
"""
from __future__ import annotations

import csv
import json
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from . import sentinel as s2
from .store import FIELDS

PREFIX = "s2:"
SAT_FIELDS = FIELDS + ["scene", "tile", "clear_frac", "n_px", "n_ref", "ndvi",
                       "delta_meleze", "delta_temoins", "ndvi_ratio_meleze", "ndvi_ratio_temoins"]
# Jaunissement du mélèze en plus de celui des témoins pour « 100 % doré ».
# Calibré sur la saison 2026 (voir README).
GOLD_DELTA = 0.12
EVERGREEN_TFV = ("FF2G61-61", "FF2-52-52", "FF2G58-58", "FF2G53-53", "FF2-80-80")
REF_RADIUS_KM = 8.0      # témoins cherchés jusqu'à cette distance (mélèze : `radius_km`)
MIN_CLEAR = 0.2          # part minimale du mélèze visible (sans nuage, neige ni ombre du relief)
MIN_PX = 100             # soit 4 ha de mélèze visible
MAX_REF_DRIFT = 0.15     # NDVI des témoins à ± 15 % de l'été, sinon voile nuageux
BASELINE_DAYS = ("09-01", "09-15")   # mélèze encore vert partout : zéro de l'indice
NDVI_BARE_RATIO = 0.45   # NDVI (rapporté à l'été) d'un mélézin sans aiguilles
# Filtres appliqués à la lecture de l'historique (ajustables sans tout recalculer)
REF_NDVI_RANGE = (0.95, 1.15)   # témoins hors de cette plage : voile léger, image écartée
MAX_BRIGHTNESS = 1.6            # luminosité > 1,6 × médiane de la zone : voile, neige fine
OUTLIER_POINTS = 30             # écart aux voisins (± 6 jours) au-delà duquel un point est isolé
BARE_VEG = 0.4                  # part d'aiguilles en dessous de laquelle le mélézin est « nu »
READ_BANDS = ("B02", "B03", "B04", "B08")


@dataclass
class Zone:
    id: str
    name: str
    lat: float
    lon: float
    radius_km: float
    cam_id: str | None = None     # zone d'une webcam, ou massif sans webcam (None)

    @property
    def key(self) -> str:
        return PREFIX + self.id


def zones_from_config(cfg) -> list[Zone]:
    sat = cfg.satellite
    zones = [Zone(c.id, c.name, c.lat, c.lon, sat.get("radius_km", 4.0), cam_id=c.id) for c in cfg.cams]
    for z in sat.get("zones", []) or []:
        zones.append(Zone(str(z["id"]), str(z.get("name", z["id"])), float(z["lat"]), float(z["lon"]),
                          float(z.get("radius_km", sat.get("radius_km", 4.0)))))
    ids = [z.id for z in zones]
    if len(ids) != len(set(ids)):
        raise ValueError("satellite.zones : un identifiant de zone reprend celui d'une webcam ou d'une autre zone")
    return zones


# --- Masques -------------------------------------------------------------------

@dataclass
class ZoneMask:
    zone: Zone
    tile: str
    window: tuple[int, int, int, int]      # col, ligne, largeur, hauteur (pixels de 20 m de la tuile)
    grid: s2.Grid
    mask: np.ndarray                       # pixels de mélèze dans le disque
    ref: np.ndarray                        # conifères persistants (témoins) dans le disque
    rg_summer: np.ndarray                  # (R-G)/(R+G) d'été, par pixel (NaN si inconnu)
    ndvi_summer: np.ndarray
    summer_date: str

    @property
    def hectares(self) -> float:
        return float(self.mask.sum()) * (s2.RES ** 2) / 10_000


def _rg(r, g):
    with np.errstate(all="ignore"):
        return (r - g) / (r + g)


def _ndvi(nir, r):
    with np.errstate(all="ignore"):
        return (nir - r) / (nir + r)


def read_window(scene: s2.Scene, grid: s2.Grid, window, bands=READ_BANDS) -> tuple[dict, np.ndarray]:
    """Réflectances à 20 m (NaN hors pixels clairs) et SCL, dans une fenêtre de la tuile."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.windows import Window
    col, row, w, h = window

    def read(band, res, resampling):
        f = 20 // res      # facteur entre la bande et la grille 20 m
        with s2._rio_env(), rasterio.open(scene.url(band)) as ds:
            return ds.read(1, window=Window(col * f, row * f, w * f, h * f), out_shape=(h, w),
                           resampling=resampling, boundless=False)

    native = {"B02": 10, "B03": 10, "B04": 10, "B08": 10, "B11": 20}
    with ThreadPoolExecutor(len(bands) + 1) as ex:
        futs = {b: ex.submit(read, b, native[b], Resampling.average) for b in bands}
        scl = ex.submit(read, "SCL", 20, Resampling.nearest).result()
        raw = {b: f.result() for b, f in futs.items()}
    clear = np.isin(scl, s2.SCL_CLEAR)
    out = {}
    for b, a in raw.items():
        r = a.astype(np.float32) * 1e-4 - 0.1
        r[(a == 0) | ~clear] = np.nan
        out[s2.BANDS[b]] = r
    return out, scl


def _zone_center(grid: s2.Grid, zone: Zone) -> tuple[float, float]:
    """Position (colonne, ligne) du centre de la zone dans la grille 20 m de la tuile."""
    from rasterio.warp import transform as warp_xy
    (x,), (y,) = warp_xy("EPSG:4326", grid.crs, [zone.lon], [zone.lat])
    return ~grid.transform * (x, y)


def _zone_window(grid: s2.Grid, zone: Zone) -> tuple[int, int, int, int] | None:
    """Fenêtre du disque des témoins, rognée au bord de la tuile.

    Refusée seulement si le disque du mélèze (`radius_km`) ne tient pas dans la tuile.
    """
    col, row = _zone_center(grid, zone)
    r_larch = zone.radius_km * 1000 / s2.RES
    if col - r_larch < 0 or row - r_larch < 0 or col + r_larch > grid.width or row + r_larch > grid.height:
        return None
    r = int(math.ceil(max(zone.radius_km, REF_RADIUS_KM) * 1000 / s2.RES))
    c0, r0 = max(0, int(col) - r), max(0, int(row) - r)
    c1, r1 = min(grid.width, int(col) + r), min(grid.height, int(row) + r)
    return c0, r0, c1 - c0, r1 - r0


def _disk(grid: s2.Grid, window, zone: Zone, radius_km: float) -> np.ndarray:
    col, row, w, h = window
    cx, cy = _zone_center(grid, zone)
    yy, xx = np.mgrid[0:h, 0:w]
    return (xx + col + 0.5 - cx) ** 2 + (yy + row + 0.5 - cy) ** 2 <= (radius_km * 1000 / s2.RES) ** 2


def _larch_in_window(grid: s2.Grid, window, zone: Zone, geojsons: list[Path]) -> np.ndarray:
    from affine import Affine
    from rasterio.features import rasterize
    from rasterio.warp import transform_geom
    col, row, w, h = window
    tr = grid.transform * Affine.translation(col, row)
    dlat = zone.radius_km / 111.0
    dlon = dlat / math.cos(math.radians(zone.lat))
    shapes = []
    for path in geojsons:
        if not path.exists():
            continue
        for f in json.loads(path.read_bytes())["features"]:
            ring = f["geometry"]["coordinates"][0][0]
            lon, lat = ring[0]
            if abs(lat - zone.lat) > dlat + 0.05 or abs(lon - zone.lon) > dlon + 0.07:
                continue
            shapes.append((transform_geom("EPSG:4326", grid.crs, f["geometry"]), 1))
    m = rasterize(shapes, out_shape=(h, w), transform=tr, dtype="uint8") > 0 if shapes else np.zeros((h, w), bool)
    return m & _disk(grid, window, zone, zone.radius_km)


def _dilate(m: np.ndarray, n: int) -> np.ndarray:
    from scipy.ndimage import binary_dilation
    return binary_dilation(m, iterations=n) if m.any() else m


def _evergreen_in_window(grid: s2.Grid, window, zone: Zone) -> np.ndarray:
    """Conifères persistants de la BD Forêt (épicéa, sapin, pins) dans le disque de la zone."""
    from affine import Affine
    from rasterio.features import rasterize
    from rasterio.warp import transform_geom
    from scipy.ndimage import binary_erosion

    from .foret import TYPENAME, WFS_URL, _request
    col, row, w, h = window
    tr = grid.transform * Affine.translation(col, row)
    dlat = max(zone.radius_km, REF_RADIUS_KM) / 111.0
    dlon = dlat / math.cos(math.radians(zone.lat))
    codes = ",".join(f"'{c}'" for c in EVERGREEN_TFV)
    r = _request("GET", WFS_URL, timeout=300, params={
        "SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature", "TYPENAMES": TYPENAME,
        "OUTPUTFORMAT": "application/json", "SRSNAME": "EPSG:4326", "COUNT": 5000,
        "PROPERTYNAME": "geom,code_tfv",
        "CQL_FILTER": (f"code_tfv IN ({codes}) AND BBOX(geom,{zone.lon - dlon},{zone.lat - dlat},"
                       f"{zone.lon + dlon},{zone.lat + dlat},'CRS:84')"),
    })
    shapes = [(transform_geom("EPSG:4326", grid.crs, f["geometry"]), 1)
              for f in r.json().get("features", []) if f.get("geometry")]
    m = rasterize(shapes, out_shape=(h, w), transform=tr, dtype="uint8") > 0 if shapes else np.zeros((h, w), bool)
    m = binary_erosion(m, iterations=1)   # bords des polygones écartés
    return m & _disk(grid, window, zone, max(zone.radius_km, REF_RADIUS_KM))


def build_masks(zones: list[Zone], year: int, geojsons: list[Path], cache: Path | None = None,
                log=print) -> list[ZoneMask]:
    """Masque de mélèze et référence d'été de chaque zone (mis en cache par année)."""
    centers = s2.tile_centers(s2.ALPS_TILES, year)
    grids: dict[str, s2.Grid] = {}
    summers: dict[str, s2.Scene] = {}
    out = []
    for z in zones:
        cpath = cache / f"{z.id}_{year}_{z.radius_km:g}km_t{REF_RADIUS_KM:g}km.npz" if cache else None
        # Tuile dont le centre est le plus proche (et qui contient la fenêtre)
        k = math.cos(math.radians(z.lat))
        order = sorted(centers, key=lambda t: ((centers[t][0] - z.lon) * k) ** 2 + (centers[t][1] - z.lat) ** 2)
        chosen = None
        for t in order:
            if t not in grids:
                sc = s2.list_scenes(t, year, [7, 8])
                best = s2.pick_scenes(sc, f"{year}-07-01", f"{year}-08-31", 5, 1, max_nodata=5)
                if not best:
                    continue
                summers[t] = best[0]
                grids[t] = s2.tile_grid(best[0])
            win = _zone_window(grids[t], z)
            if win:
                chosen = (t, win)
                break
        if not chosen:
            log(f"[{z.id}] hors des tuiles des Alpes : ignorée")
            continue
        t, win = chosen
        grid = grids[t]
        if cpath and cpath.exists():
            d = np.load(cpath)
            out.append(ZoneMask(z, t, tuple(int(v) for v in d["window"]), grid, d["mask"], d["ref"],
                                d["rg"], d["ndvi"], str(d["summer"])))
            continue
        mask = _larch_in_window(grid, win, z, geojsons)
        ref = _evergreen_in_window(grid, win, z) & ~_dilate(mask, 2)
        bands, _ = read_window(summers[t], grid, win)
        zm = ZoneMask(z, t, win, grid, mask, ref, _rg(bands["red"], bands["green"]),
                      _ndvi(bands["nir"], bands["red"]), summers[t].date)
        if cpath:
            cpath.parent.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(cpath, window=np.array(win), mask=mask, ref=ref,
                                rg=zm.rg_summer.astype(np.float32), ndvi=zm.ndvi_summer.astype(np.float32),
                                summer=zm.summer_date)
        log(f"[{z.id}] tuile {t}, {zm.hectares:.0f} ha de mélèze, "
            f"{ref.sum() * s2.RES ** 2 / 1e4:.0f} ha de témoins, été {zm.summer_date}")
        out.append(zm)
    return out


# --- Mesure d'une scène ----------------------------------------------------------

def _stats(px: np.ndarray, bands, zm: ZoneMask) -> tuple[float, float]:
    """(jaunissement médian, NDVI du jour / NDVI d'été) d'un ensemble de pixels."""
    r, g, nir = bands["red"][px], bands["green"][px], bands["nir"][px]
    d = _rg(r, g) - zm.rg_summer[px]
    with np.errstate(all="ignore"):
        ratio = _ndvi(nir, r) / zm.ndvi_summer[px]
    return float(np.nanmedian(d)), float(np.nanmedian(ratio))


def veg_share(ndvi_ratio: float) -> float:
    """Part des aiguilles encore présentes : 1 en été, 0 pour un mélézin nu."""
    return round(float(np.clip((ndvi_ratio - NDVI_BARE_RATIO) / (1 - NDVI_BARE_RATIO), 0, 1)), 3)


def gold_from(delta_larch: float, delta_ref: float) -> float:
    return round(float(np.clip((delta_larch - delta_ref) / GOLD_DELTA, 0, 1)) * 100, 1)


def measure(zm: ZoneMask, bands: dict[str, np.ndarray]) -> dict:
    """Indice d'or satellite d'une zone sur une scène (champs de l'historique)."""
    r, g, b, nir = bands["red"], bands["green"], bands["blue"], bands["nir"]
    seen = np.isfinite(r) & np.isfinite(g) & np.isfinite(nir) & np.isfinite(zm.rg_summer)
    ok, ok_ref = zm.mask & seen, zm.ref & seen
    n_mask = int(zm.mask.sum())
    frac = ok.sum() / max(n_mask, 1)
    row = {"n_px": int(ok.sum()), "n_ref": int(ok_ref.sum()), "clear_frac": round(float(frac), 3)}
    if n_mask < MIN_PX:
        return {**row, "valid": 0, "reason": "trop peu de mélèze dans la zone"}
    if frac < MIN_CLEAR or ok.sum() < MIN_PX:
        return {**row, "valid": 0, "reason": f"nuages, ombre ou neige ({frac * 100:.0f} % du mélèze visible)"}
    d_l, v_l = _stats(ok, bands, zm)
    if ok_ref.sum() >= MIN_PX:
        d_r, v_r = _stats(ok_ref, bands, zm)
        if abs(v_r - 1) > MAX_REF_DRIFT:
            return {**row, "valid": 0, "reason": f"voile nuageux (NDVI des témoins à {v_r * 100:.0f} % de l'été)"}
    else:   # pas de témoins visibles : pas de correction (n_ref le signale)
        d_r, v_r = 0.0, 1.0
    rgb = [float(np.nanmean(v[ok])) for v in (r, g, b)]
    s = sum(rgb) or 1.0
    # Couleur d'affichage : une forêt vue d'en haut ne réfléchit que 3 à 8 % de la
    # lumière, on étire donc 0-10 % de réflectance sur 0-255.
    hexcol = "#" + "".join(f"{int(max(0, min(255, round(v / 0.1 * 255)))):02x}" for v in rgb)
    return {
        **row, "valid": 1, "reason": "",
        "gold_index": gold_from(d_l, d_r),
        "veg_share": veg_share(v_l / v_r if v_r > 0 else v_l),
        "gcc": round(rgb[1] / s, 4), "rcc": round(rgb[0] / s, 4),
        "mean_rgb": hexcol, "brightness": round(s / 3, 4),
        "ndvi": round(float(np.nanmedian(_ndvi(nir[ok], r[ok]))), 3),
        "delta_meleze": round(d_l, 4), "delta_temoins": round(d_r, 4),
        "ndvi_ratio_meleze": round(v_l, 4), "ndvi_ratio_temoins": round(v_r, 4),
        "green": "", "yellow": "", "orange": "", "brown": "",
    }


def scene_time(scene: s2.Scene, tz: str) -> datetime:
    t = datetime.fromisoformat((scene.time or scene.date + "T10:30:00Z").replace("Z", "+00:00"))
    return t.astimezone(ZoneInfo(tz)).replace(tzinfo=None, second=0, microsecond=0)


def processed(history: Path) -> set[tuple[str, str]]:
    if not history.exists():
        return set()
    with history.open(newline="", encoding="utf-8") as f:
        return {(r["cam"], r["scene"]) for r in csv.DictReader(f)}


def update(masks: list[ZoneMask], since: date, until: date, history: Path, tz: str,
           max_cloud: float = 90.0, log=print) -> int:
    """Mesure toutes les scènes non encore traitées ; renvoie le nombre de lignes ajoutées."""
    done = processed(history)
    by_tile: dict[str, list[ZoneMask]] = {}
    for zm in masks:
        by_tile.setdefault(zm.tile, []).append(zm)
    months: dict[int, list[int]] = {}
    y, m = since.year, since.month
    while (y, m) <= (until.year, until.month):
        months.setdefault(y, []).append(m)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    jobs = []
    for tile, zms in by_tile.items():
        scenes = []
        for y, ms in months.items():
            scenes += s2.list_scenes(tile, y, ms)
        for sc in scenes:
            if not (since.isoformat() <= sc.date <= until.isoformat()) or sc.cloud > max_cloud or sc.nodata > 95:
                continue
            todo = [zm for zm in zms if (zm.zone.key, sc.prefix) not in done]
            if todo:
                jobs.append((sc, todo))
    log(f"{len(jobs)} scène(s) à traiter")

    def run(job):
        sc, zms = job
        rows = []
        when = scene_time(sc, tz)
        for zm in zms:
            try:
                bands, _ = read_window(sc, zm.grid, zm.window)
                m = measure(zm, bands)
            except Exception as e:  # noqa: BLE001 - scène partielle, lecture coupée...
                m = {"valid": 0, "reason": f"lecture impossible : {type(e).__name__}", "n_px": 0,
                     "n_ref": 0, "clear_frac": 0}
            rows.append({**{k: "" for k in SAT_FIELDS}, **m, "timestamp": when.isoformat(timespec="minutes"),
                         "cam": zm.zone.key, "scene": sc.prefix, "tile": sc.tile})
        return sc, rows

    added = 0
    history.parent.mkdir(parents=True, exist_ok=True)
    new = not history.exists()
    with history.open("a", newline="", encoding="utf-8") as f, ThreadPoolExecutor(4) as ex:
        w = csv.DictWriter(f, fieldnames=SAT_FIELDS)
        if new:
            w.writeheader()
        for sc, rows in ex.map(run, sorted(jobs, key=lambda j: j[0].date)):
            for r in rows:
                w.writerow(r)
            f.flush()
            added += len(rows)
            ok = [r for r in rows if r["valid"] == 1]
            log(f"  {sc.date} {sc.tile} : {len(ok)}/{len(rows)} zone(s) mesurée(s)"
                + (" — " + ", ".join(f"{r['cam'][len(PREFIX):]} {r['gold_index']:.0f}" for r in ok) if ok else ""))
    return added


# --- Comparaison webcam / satellite ---------------------------------------------

def compare(cam_points, sat_points, max_gap_days: int = 1) -> dict | None:
    """Écart moyen entre les courbes journalières webcam et satellite (jours communs ± 1 j)."""
    if not cam_points or not sat_points:
        return None
    cam = {p.day: p.gold_index for p in cam_points}
    diffs = []
    for p in sat_points:
        near = [cam[d] for d in cam if abs((d - p.day).days) <= max_gap_days]
        if near:
            diffs.append(float(np.median(near)) - p.gold_index)
    if not diffs:
        return None
    return {"jours": len(diffs), "ecart_moyen": round(float(np.mean(diffs)), 1),
            "ecart_absolu": round(float(np.mean(np.abs(diffs))), 1)}


# --- Courbe journalière (même logique que les webcams) --------------------------

def daily_points(rows: list[dict], key: str, year: int | None = None):
    """Points journaliers d'une zone, l'indice étant ramené à 0 sur le début de septembre.

    Début septembre, le mélèze est vert partout, mais un mélézin clair « jaunit »
    déjà un peu sur l'image (herbe du sous-bois qui sèche) : cette valeur de
    départ, propre à chaque zone, est soustraite. Renvoie (points, zéro retenu).
    """
    from datetime import date as _date

    from .store import DailyPoint
    vals, lum = [], []
    for r in rows:
        if r["cam"] != key or r["valid"] not in ("1", 1) or r.get("delta_meleze") in ("", None):
            continue
        ts = datetime.fromisoformat(r["timestamp"])
        if year and ts.year != year:
            continue
        if int(r.get("n_ref") or 0) >= MIN_PX and not (
                REF_NDVI_RANGE[0] <= float(r["ndvi_ratio_temoins"]) <= REF_NDVI_RANGE[1]):
            continue
        vals.append((ts.date(), float(r["delta_meleze"]) - float(r["delta_temoins"] or 0),
                     float(r["veg_share"]), float(r["gcc"]), r["mean_rgb"], float(r["brightness"])))
    if vals:
        med_lum = float(np.median([v[5] for v in vals]))
        vals = [v[:5] for v in vals if v[5] <= MAX_BRIGHTNESS * med_lum]
    if not vals:
        return [], None
    y = vals[0][0].year
    lo, hi = (_date(y, *map(int, d.split("-"))) for d in BASELINE_DAYS)
    base_vals = [d for day, d, *_ in vals if lo <= day <= hi]
    if len(base_vals) < 2:          # trop peu d'images claires : les 3 premières valeurs
        base_vals = [d for _, d, *_ in sorted(vals)[:3]]
    base = float(np.median(base_vals))
    by_day: dict = {}
    for day, d, v, gcc, rgb in vals:
        by_day.setdefault(day, []).append((round(float(np.clip((d - base) / GOLD_DELTA, 0, 1)) * 100, 1),
                                           v, gcc, rgb))
    med = {d: float(np.median([x[0] for x in xs])) for d, xs in by_day.items()}
    while True:         # points isolés (très loin de leurs voisins des ± 6 jours), le pire d'abord
        worst, gap = None, OUTLIER_POINTS
        for d in med:
            nb = [med[o] for o in med if o != d and abs((o - d).days) <= 6]
            if len(nb) >= 2 and abs(med[d] - float(np.median(nb))) > gap:
                worst, gap = d, abs(med[d] - float(np.median(nb)))
        if worst is None:
            break
        del med[worst], by_day[worst]
    pts = []
    for day in sorted(by_day):
        xs = by_day[day]
        gi = float(np.median([x[0] for x in xs]))
        pts.append(DailyPoint(day=day, gold_index=round(gi, 1),
                              veg_share=round(float(np.median([x[1] for x in xs])), 3),
                              gcc=round(float(np.median([x[2] for x in xs])), 4),
                              mean_rgb=min(xs, key=lambda x: abs(x[0] - gi))[3], n=len(xs)))
    return pts, round(base, 4)


def split_bare(points) -> tuple[list, list]:
    """Sépare les jours où le mélézin a perdu ses aiguilles (fin de saison).

    Le jaunissement reste maximal pendant que les aiguilles brunissent et
    tombent ; c'est la chute du NDVI qui marque le déclin. Ces jours sont
    traités comme les images « sans végétation colorée » des webcams.
    """
    keep = [p for p in points if p.veg_share >= BARE_VEG]
    bare = [p.day for p in points if p.veg_share < BARE_VEG]
    return keep, bare
