"""Cartographie des mélézins par Sentinel-2 (été + automne).

Le mélèze est le seul conifère des Alpes qui jaunit puis perd ses aiguilles :
vert comme un épicéa en juillet, doré fin octobre. On compare donc, pixel par
pixel (20 m), une image d'été sans nuages et les images claires d'octobre.

Vérité terrain : la BD Forêt V2 (IGN). Ses polygones de mélèze pur servent
d'exemples positifs ; sapinières/pessières, pinèdes, feuillus, landes boisées
d'exemples négatifs, toujours sur les pixels d'arbres (ESA WorldCover). Un classifieur (gradient boosting) apprend la
signature été/automne, sa précision est mesurée sur des blocs de 10 km laissés
de côté, puis il est appliqué à toute la forêt (masque arbres ESA WorldCover).
Il retrouve ainsi les mélézins que la BD Forêt range ailleurs (mélanges,
« feuillus », « autre pin »...).

Données ouvertes, sans compte :
- Sentinel-2 L2A « collection 1 » (COG, bucket AWS e84-earth-search-sentinel-data) ;
- ESA WorldCover 2021 (10 m) pour le masque des arbres ;
- Copernicus DEM GLO-30 pour l'altitude ;
- BD Forêt V2 (WFS Géoplateforme) pour l'apprentissage.
"""
from __future__ import annotations

import json
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import requests

S2_BUCKET = "https://e84-earth-search-sentinel-data.s3.us-west-2.amazonaws.com"
S2_COLLECTION = "sentinel-2-c1-l2a"
WORLDCOVER = ("https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
              "ESA_WorldCover_10m_2021_v200_{tile}_Map.tif")
DEM = ("https://copernicus-dem-30m.s3.amazonaws.com/Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM/"
       "Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM.tif")

# Tuiles Sentinel-2 couvrant les Alpes françaises.
ALPS_TILES = ["31TGJ", "31TGK", "31TGL", "31TGM", "32TLP", "32TLQ", "32TLR"]

RES = 20                        # m : résolution de travail
BANDS = {"B02": "blue", "B03": "green", "B04": "red", "B05": "re1", "B06": "re2",
         "B07": "re3", "B08": "nir", "B8A": "nir2", "B11": "swir1", "B12": "swir2"}
# SCL (classification de scène L2A) : végétation (4), sol nu (5) ; on écarte
# nuages, ombres, neige, eau et pixels saturés/défectueux.
SCL_CLEAR = (4, 5)

# Classes d'apprentissage (codes TFV BD Forêt V2)
CLASSES = {
    1: ("mélèze", ["code_tfv = 'FF2-63-63'"]),
    2: ("sapin, épicéa", ["code_tfv = 'FF2G61-61'"]),
    3: ("pins", ["code_tfv IN ('FF2-52-52','FF2G58-58','FF2G53-53','FF2-80-80')"]),
    4: ("feuillus", ["code_tfv LIKE 'FF1%'"]),
    5: ("landes, prairies", ["code_tfv IN ('LA4','LA6')"]),
}
LARCH = 1

GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "GDAL_HTTP_MULTIPLEX": "YES",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_HTTP_MAX_RETRY": "5",
    "GDAL_HTTP_RETRY_DELAY": "2",
    "VSI_CACHE": "TRUE",
}


def _rio_env():
    import rasterio
    env = dict(GDAL_ENV)
    for var in ("CURL_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        if os.environ.get(var):
            env.setdefault("CURL_CA_BUNDLE", os.environ[var])
    return rasterio.Env(**env)


# --- Catalogue (listing S3, sans STAC) ----------------------------------------

@dataclass
class Scene:
    tile: str
    date: str          # AAAA-MM-JJ
    prefix: str        # préfixe S3 du dossier de la scène
    cloud: float
    snow: float
    nodata: float
    time: str = ""     # date et heure UTC du passage (ISO 8601)

    def url(self, band: str) -> str:
        return f"{S2_BUCKET}/{self.prefix}{band}.tif"


def list_scenes(tile: str, year: int, months: list[int]) -> list[Scene]:
    z, b, g = tile[:2], tile[2], tile[3:]
    prefixes: list[str] = []
    for m in months:
        pre = f"{S2_COLLECTION}/{z}/{b}/{g}/{year}/{m}/"
        x = requests.get(S2_BUCKET, params={"list-type": "2", "delimiter": "/", "prefix": pre}, timeout=60).text
        prefixes += re.findall(r"<Prefix>([^<]+_L2A/)</Prefix>", x)

    def meta(pre: str) -> Scene:
        name = pre.rstrip("/").rsplit("/", 1)[-1]
        p = requests.get(f"{S2_BUCKET}/{pre}{name}.json", timeout=60).json()["properties"]
        return Scene(tile, p["datetime"][:10], pre, p.get("eo:cloud_cover", 100.0),
                     p.get("s2:snow_ice_percentage", 0.0), p.get("s2:nodata_pixel_percentage", 0.0),
                     p["datetime"])

    with ThreadPoolExecutor(8) as ex:
        return sorted(ex.map(meta, prefixes), key=lambda s: s.date)


def pick_scenes(scenes: list[Scene], start: str, end: str, max_cloud: float, n: int,
                max_nodata: float = 50.0) -> list[Scene]:
    """Les `n` scènes les moins nuageuses de la période (tuile au moins à moitié couverte)."""
    ok = [s for s in scenes if start <= s.date <= end and s.cloud <= max_cloud and s.nodata <= max_nodata]
    return sorted(sorted(ok, key=lambda s: (s.cloud + s.snow, s.nodata))[:n], key=lambda s: s.date)


# --- Lecture ------------------------------------------------------------------

@dataclass
class Grid:
    crs: str
    transform: object      # affine.Affine
    width: int
    height: int

    @property
    def bounds_lonlat(self) -> tuple[float, float, float, float]:
        from rasterio.warp import transform_bounds
        x0, y0 = self.transform * (0, self.height)
        x1, y1 = self.transform * (self.width, 0)
        return transform_bounds(self.crs, "EPSG:4326", x0, y0, x1, y1, densify_pts=21)


def tile_grid(scene: Scene) -> Grid:
    import rasterio
    with _rio_env(), rasterio.open(scene.url("B11")) as ds:   # bande native à 20 m
        return Grid(ds.crs.to_string(), ds.transform, ds.width, ds.height)


def read_scene(scene: Scene, grid: Grid, bands=tuple(BANDS)) -> tuple[dict[str, np.ndarray], np.ndarray]:
    """Réflectances (float32, NaN hors pixels clairs) et masque des pixels clairs, à 20 m."""
    import rasterio
    from rasterio.enums import Resampling

    def read(band: str, resampling) -> np.ndarray:
        with _rio_env(), rasterio.open(scene.url(band)) as ds:
            return ds.read(1, out_shape=(grid.height, grid.width), resampling=resampling)

    with ThreadPoolExecutor(4) as ex:
        futs = {b: ex.submit(read, b, Resampling.average) for b in bands}
        scl = read("SCL", Resampling.nearest)
        raw = {b: f.result() for b, f in futs.items()}
    clear = np.isin(scl, SCL_CLEAR)
    out = {}
    for b, a in raw.items():
        r = a.astype(np.float32) * 1e-4 - 0.1
        r[(a == 0) | ~clear] = np.nan
        out[BANDS[b]] = r
    return out, clear


AUTUMN_BANDS = ("B02", "B03", "B04", "B05", "B06", "B08", "B11")


def read_pixels(scene: Scene, grid: Grid, sel: np.ndarray, bands=tuple(BANDS)) -> tuple[dict[str, np.ndarray], float]:
    """Comme `read_scene`, mais ne garde que les pixels `sel` (économise la mémoire)."""
    full, clear = read_scene(scene, grid, bands)
    frac = float(clear[sel].mean()) if sel.any() else 0.0
    return {k: v[sel] for k, v in full.items()}, frac


def warp_to_grid(url: str, grid: Grid, resampling, dtype="float32", nodata=0):
    import rasterio
    from rasterio.vrt import WarpedVRT
    with _rio_env(), rasterio.open(url) as src, WarpedVRT(
            src, crs=grid.crs, transform=grid.transform, width=grid.width, height=grid.height,
            resampling=resampling, nodata=nodata) as vrt:
        return vrt.read(1).astype(dtype)


def _deg_tiles(bounds, step):
    lon0, lat0, lon1, lat1 = bounds
    for la in range(math.floor(lat0 / step) * step, math.ceil(lat1 / step) * step, step):
        for lo in range(math.floor(lon0 / step) * step, math.ceil(lon1 / step) * step, step):
            yield la, lo


def tree_mask(grid: Grid) -> np.ndarray:
    """Pixels 20 m dont au moins la moitié est « couvert arboré » (classe 10) dans ESA WorldCover."""
    from rasterio.enums import Resampling
    frac = np.zeros((grid.height, grid.width), np.float32)
    for la, lo in _deg_tiles(grid.bounds_lonlat, 3):
        tile = f"{'N' if la >= 0 else 'S'}{abs(la):02d}{'E' if lo >= 0 else 'W'}{abs(lo):03d}"
        try:
            wc = _wc_tree_fraction(WORLDCOVER.format(tile=tile), grid, Resampling)
        except Exception:  # noqa: BLE001 - tuile hors couverture
            continue
        frac = np.maximum(frac, wc)
    return frac >= 0.5


def _wc_tree_fraction(url: str, grid: Grid, Resampling) -> np.ndarray:
    # Lecture à 10 m dans la grille 20 m doublée, puis moyenne 2x2 de l'indicateur « arbre ».
    import rasterio
    from affine import Affine
    from rasterio.vrt import WarpedVRT
    g2 = Grid(grid.crs, grid.transform * Affine.scale(0.5), grid.width * 2, grid.height * 2)
    with _rio_env(), rasterio.open(url) as src, WarpedVRT(
            src, crs=g2.crs, transform=g2.transform, width=g2.width, height=g2.height,
            resampling=Resampling.nearest, nodata=0) as vrt:
        lc = vrt.read(1)
    t = (lc == 10).astype(np.float32)
    return t.reshape(grid.height, 2, grid.width, 2).mean(axis=(1, 3))


def elevation(grid: Grid) -> np.ndarray:
    """Altitude (m) Copernicus DEM GLO-30, rééchantillonnée sur la grille."""
    from rasterio.enums import Resampling
    z = np.full((grid.height, grid.width), np.nan, np.float32)
    for la, lo in _deg_tiles(grid.bounds_lonlat, 1):
        url = DEM.format(ns="N" if la >= 0 else "S", lat=abs(la), ew="E" if lo >= 0 else "W", lon=abs(lo))
        try:
            d = warp_to_grid(url, grid, Resampling.bilinear, nodata=-32767)
        except Exception:  # noqa: BLE001 - mer, hors couverture
            continue
        ok = d > -1000
        z[ok] = d[ok]
    return z


# --- Étiquettes BD Forêt -------------------------------------------------------

def bdforet_labels(grid: Grid, erode_px: int = 1, timeout: int = 300) -> np.ndarray:
    """Raster des classes d'apprentissage (0 = sans étiquette) ; bords des polygones retirés."""
    from rasterio.features import rasterize
    from rasterio.warp import transform_geom
    from scipy.ndimage import binary_erosion

    from .foret import TYPENAME, WFS_URL, _request
    lon0, lat0, lon1, lat1 = grid.bounds_lonlat
    labels = np.zeros((grid.height, grid.width), np.uint8)
    for cls, (_, filters) in CLASSES.items():
        feats, start = [], 0
        while True:
            r = _request("GET", WFS_URL, timeout=timeout, params={
                "SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature", "TYPENAMES": TYPENAME,
                "OUTPUTFORMAT": "application/json", "SRSNAME": "EPSG:4326", "COUNT": 2000,
                "STARTINDEX": start, "SORTBY": "id", "PROPERTYNAME": "geom,code_tfv",
                "CQL_FILTER": f"({' OR '.join(filters)}) AND BBOX(geom,{lon0},{lat0},{lon1},{lat1},'CRS:84')",
            })
            page = r.json().get("features", [])
            feats += page
            if len(page) < 2000:
                break
            start += 2000
        shapes = [(transform_geom("EPSG:4326", grid.crs, f["geometry"]), 1) for f in feats if f.get("geometry")]
        if not shapes:
            continue
        m = rasterize(shapes, out_shape=(grid.height, grid.width), transform=grid.transform, dtype="uint8")
        if erode_px:
            m = binary_erosion(m.astype(bool), iterations=erode_px)
        labels[(m > 0) & (labels == 0)] = cls
    return labels


# --- Indices et classification ----------------------------------------------

def _nd(a, b):
    with np.errstate(invalid="ignore", divide="ignore"):
        return (a - b) / (a + b)


def features(summer: dict[str, np.ndarray], autumns: list[dict[str, np.ndarray]],
             z: np.ndarray) -> tuple[np.ndarray, list[str]]:
    """Variables par pixel (tableau pixels x variables), NaN si une date manque.

    Les entrées sont des vecteurs 1-D des pixels retenus (voir `read_pixels`).
    """
    cols, names = [], []

    def add(name, arr):
        cols.append(arr.astype(np.float32, copy=False))
        names.append(name)

    for k in ("blue", "green", "red", "re1", "re2", "re3", "nir", "swir1", "swir2"):
        add(f"ete_{k}", summer[k])
    ndvi_s = _nd(summer["nir"], summer["red"])
    add("ete_ndvi", ndvi_s)
    add("ete_ndmi", _nd(summer["nir"], summer["swir1"]))
    add("ete_vert_rouge", _nd(summer["green"], summer["red"]))

    # Automne : médiane des dates claires + extrêmes (le pic varie avec l'altitude)
    stack = {k: np.stack([a[k] for a in autumns]) for k in autumns[0]}
    del autumns
    with np.errstate(all="ignore"):
        ndvi_a = _nd(stack["nir"], stack["red"])
        jaune = _nd(stack["red"], stack["blue"]) - _nd(stack["green"], stack["red"])  # doré : R haut, B bas
        rg = _nd(stack["red"], stack["green"])
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            for k in ("blue", "green", "red", "re1", "re2", "nir", "swir1"):
                add(f"aut_{k}", np.nanmedian(stack[k], axis=0))
            add("aut_ndvi_min", np.nanmin(ndvi_a, axis=0))
            add("aut_ndvi_med", np.nanmedian(ndvi_a, axis=0))
            add("aut_rouge_vert_max", np.nanmax(rg, axis=0))
            add("aut_jaune_max", np.nanmax(jaune, axis=0))
            add("delta_ndvi", np.nanmin(ndvi_a, axis=0) - ndvi_s)
            add("delta_rouge_vert", np.nanmax(rg, axis=0) - _nd(summer["red"], summer["green"]))
            add("aut_n_dates", np.sum(np.isfinite(stack["red"]), axis=0).astype(np.float32))
    add("altitude", z)
    return np.stack(cols, axis=1), names


NEIGHBOR_COLS = ("aut_jaune_max", "delta_ndvi", "aut_rouge_vert_max", "ete_ndvi", "ete_nir",
                 "aut_ndvi_min", "ete_vert_rouge")
NEIGHBOR_SIZES = (3, 7)          # fenêtres de 60 m et 140 m


def add_neighborhood(X: np.ndarray, names: list[str], sel: np.ndarray) -> tuple[np.ndarray, list[str]]:
    """Ajoute la moyenne locale de quelques variables : un mélézin est un peuplement, pas un pixel."""
    from scipy.ndimage import uniform_filter
    h, w = sel.shape
    extra, extra_names = [], []
    for col in NEIGHBOR_COLS:
        v = X[:, names.index(col)].astype(np.float32)
        f = np.isfinite(v)
        full = np.zeros((h, w), np.float32)
        ok = np.zeros((h, w), np.float32)
        full[sel] = np.where(f, v, 0)
        ok[sel] = f
        for size in NEIGHBOR_SIZES:
            with np.errstate(all="ignore"):
                r = uniform_filter(full, size) / uniform_filter(ok, size)
            extra.append(r[sel].astype(X.dtype))
            extra_names.append(f"{col}_moy{size * RES}m")
    return np.column_stack([X] + extra), names + extra_names


def _model(seed: int = 0):
    from sklearn.ensemble import HistGradientBoostingClassifier
    return HistGradientBoostingClassifier(max_iter=300, learning_rate=0.08, max_leaf_nodes=48,
                                          l2_regularization=1.0, random_state=seed)


def evaluate(model, X: np.ndarray, y: np.ndarray, w: np.ndarray | None = None,
             thresholds=(0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95)) -> dict:
    """Précision / rappel du mélèze dans les proportions réelles (poids `w` si sous-échantillonné)."""
    w = np.ones(len(y)) if w is None else w
    p = model.predict_proba(X)[:, 1]
    t = y == LARCH
    rows = []
    for th in thresholds:
        k = p >= th
        tp, pos, real = (w * (k & t)).sum(), (w * k).sum(), (w * t).sum()
        prec, rec = tp / max(pos, 1e-9), tp / max(real, 1e-9)
        rows.append({"seuil": th, "precision": round(float(prec), 3), "rappel": round(float(rec), 3),
                     "f1": round(float(2 * prec * rec / max(prec + rec, 1e-9)), 3),
                     "fausses_alarmes": {CLASSES[c][0]: round(float(k[y == c].mean()), 4)
                                         for c in CLASSES if c != LARCH and (y == c).any()}})
    best = max(rows, key=lambda r: r["f1"])
    return {"pixels_validation": int(len(y)), "meleze_validation": int(t.sum()),
            "seuils": rows, "seuil_retenu": best["seuil"]}


def train(X: np.ndarray, y: np.ndarray, blocks: np.ndarray, per_class: int = 60000, seed: int = 0):
    """Apprentissage + validation par blocs spatiaux (une tuile). Renvoie (modèle, seuil, rapport).

    Apprentissage sur un échantillon équilibré par classe ; validation sur
    *tous* les pixels étiquetés des blocs laissés de côté, donc dans les
    proportions réelles (le mélèze pur est minoritaire) : c'est elle qui fixe le
    seuil de décision (meilleur F1).
    """
    rng = np.random.default_rng(seed)
    test_px = (blocks % 4) == 0

    def sample(mask):
        idx = []
        for c in np.unique(y[mask & (y > 0)]):
            ii = np.flatnonzero(mask & (y == c))
            idx.append(rng.choice(ii, size=min(per_class, ii.size), replace=False))
        return np.concatenate(idx)

    tr = sample(~test_px)
    m = _model(seed).fit(X[tr], y[tr] == LARCH)
    te = np.flatnonzero(test_px & (y > 0))
    rep = evaluate(m, X[te], y[te])
    rep["pixels_apprentissage"] = int(tr.size)
    allx = sample(np.ones_like(test_px))
    return _model(seed).fit(X[allx], y[allx] == LARCH), rep["seuil_retenu"], rep


def spatial_blocks(grid: Grid, size_m: float = 10_000) -> np.ndarray:
    cols = np.arange(grid.width) * RES // size_m
    rows = np.arange(grid.height) * RES // size_m
    # Identifiant de bloc mélangé, pour que « bloc % 4 » ne donne pas des bandes
    return ((rows[:, None] * 7 + cols[None, :] * 3) % 1009).astype(np.int32).ravel()


# --- Chaîne complète (plusieurs tuiles) ----------------------------------------

def tile_centers(tiles: list[str], year: int) -> dict[str, tuple[float, float]]:
    """Centre lon/lat de chaque tuile (pour départager les recouvrements)."""
    out = {}
    for t in tiles:
        sc = [s for s in list_scenes(t, year, [7]) if s.nodata < 5] or list_scenes(t, year, [7])
        g = tile_grid(sc[0])
        lon0, lat0, lon1, lat1 = g.bounds_lonlat
        out[t] = ((lon0 + lon1) / 2, (lat0 + lat1) / 2)
    return out


def owned_pixels(grid: Grid, tile: str, centers: dict[str, tuple[float, float]]) -> np.ndarray:
    """Pixels plus proches du centre de cette tuile que de celui des autres (pas de doublon)."""
    from rasterio.warp import transform as warp_xy
    step = 50   # calcul sur une grille grossière, puis agrandissement
    rows = np.arange(0, grid.height, step) + step // 2
    cols = np.arange(0, grid.width, step) + step // 2
    cc, rr = np.meshgrid(cols, rows)
    xs, ys = grid.transform * (cc.ravel() + 0.5, rr.ravel() + 0.5)
    lon, lat = warp_xy(grid.crs, "EPSG:4326", xs, ys)
    lon, lat = np.array(lon), np.array(lat)
    k = math.cos(math.radians(45))

    def d2(c):
        return ((lon - c[0]) * k) ** 2 + (lat - c[1]) ** 2

    mine = d2(centers[tile])
    own = np.ones_like(mine, bool)
    for t, c in centers.items():
        if t != tile:
            own &= mine <= d2(c)
    own = own.reshape(rr.shape)
    return np.repeat(np.repeat(own, step, 0), step, 1)[:grid.height, :grid.width]


def prepare_tile(tile: str, year: int, cache: Path, centers: dict, n_autumn: int = 4, log=print) -> Path:
    """Variables de tous les pixels utiles d'une tuile, mises en cache (float16)."""
    out = cache / f"{tile}_{year}.npz"
    if out.exists() and (cache / f"{tile}_{year}_X.npy").exists():
        log(f"[{tile}] déjà prêt")
        return out
    sc = list_scenes(tile, year, [7, 8, 9, 10, 11])
    summer = pick_scenes(sc, f"{year}-07-01", f"{year}-08-31", 5, 1, max_nodata=5)
    autumn = pick_scenes(sc, f"{year}-10-01", f"{year}-11-05", 45, n_autumn)
    if not summer or not autumn:
        raise RuntimeError(f"{tile} : pas assez d'images claires (été {len(summer)}, automne {len(autumn)})")
    log(f"[{tile}] été {summer[0].date}, automne {', '.join(a.date for a in autumn)}")
    grid = tile_grid(summer[0])
    z = elevation(grid)
    trees = tree_mask(grid)
    lab = bdforet_labels(grid)
    own = owned_pixels(grid, tile, centers)
    sel = trees | (lab > 0)
    S, _ = read_pixels(summer[0], grid, sel)
    A = []
    for a in autumn:
        r, frac = read_pixels(a, grid, sel, AUTUMN_BANDS)
        A.append(r)
        log(f"[{tile}]   {a.date} : {frac * 100:.0f} % de pixels clairs")
    X, names = features(S, A, z[sel])
    del S, A
    X, names = add_neighborhood(X, names, sel)
    cache.mkdir(parents=True, exist_ok=True)
    np.save(cache / f"{tile}_{year}_X.npy", X.astype(np.float16))
    np.savez_compressed(out, sel=sel, lab=lab, trees=trees, own=own, z=z.astype(np.float32),
                        names=np.array(names), transform=np.array(tuple(grid.transform)[:6]),
                        crs=grid.crs, shape=(grid.height, grid.width),
                        dates=np.array([summer[0].date] + [a.date for a in autumn]))
    log(f"[{tile}] {sel.sum():,} pixels, {len(names)} variables".replace(",", " "))
    return out


def _load(npz: Path):
    from affine import Affine
    d = np.load(npz)
    X = np.load(str(npz).replace(".npz", "_X.npy"), mmap_mode="r")
    h, w = map(int, d["shape"])
    grid = Grid(str(d["crs"]), Affine(*d["transform"]), w, h)
    return d, X, grid


def train_all(npzs: list[Path], per_class: int = 25000, test_keep: float = 0.2, seed: int = 0, log=print):
    """Un modèle pour toutes les tuiles ; validation par blocs de 10 km, proportions réelles."""
    rng = np.random.default_rng(seed)
    tr_X, tr_y, te_X, te_y, te_w = [], [], [], [], []
    for npz in npzs:
        d, X, grid = _load(npz)
        sel, lab, trees = d["sel"], d["lab"], d["trees"]
        # Apprentissage sur les seuls pixels d'arbres, comme la prédiction : sinon
        # le modèle apprend que « forte chute d'automne = prairie » et rejette les
        # mélèzes les plus jaunis.
        y = np.where(trees, lab, 0)[sel]
        test = (spatial_blocks(grid)[sel.ravel()] % 4) == 0
        for c in np.unique(y[y > 0]):
            ii = np.flatnonzero(~test & (y == c))
            ii = rng.choice(ii, size=min(per_class, ii.size), replace=False)
            tr_X.append(np.asarray(X[np.sort(ii)], np.float32))
            tr_y.append(np.full(ii.size, c, np.uint8))
            jj = np.flatnonzero(test & (y == c))
            keep = 1.0 if c == LARCH else test_keep          # tout le mélèze, 20 % du reste
            jj = np.sort(rng.choice(jj, size=int(jj.size * keep), replace=False))
            te_X.append(np.asarray(X[jj], np.float32))
            te_y.append(np.full(jj.size, c, np.uint8))
            te_w.append(np.full(jj.size, 1 / keep, np.float32))
        log(f"  {npz.name} : {int((y == LARCH).sum()):,} pixels de mélèze pur".replace(",", " "))
    Xtr, ytr = np.concatenate(tr_X), np.concatenate(tr_y)
    Xte, yte, wte = np.concatenate(te_X), np.concatenate(te_y), np.concatenate(te_w)
    m = _model(seed).fit(Xtr, ytr == LARCH)
    rep = evaluate(m, Xte, yte, wte)
    rep["pixels_apprentissage"] = int(len(ytr))
    final = _model(seed).fit(np.concatenate([Xtr, Xte]), np.concatenate([ytr, yte]) == LARCH)
    return final, rep


def predict_tile(model, npz: Path) -> tuple[np.ndarray, Grid, np.ndarray]:
    """Probabilité de mélèze (0-100, uint8) sur les arbres de la tuile qui lui reviennent."""
    d, X, grid = _load(npz)
    sel, trees, own = d["sel"], d["trees"], d["own"]
    target = (trees & own)[sel]
    prob = np.zeros(sel.shape, np.uint8)
    idx = np.flatnonzero(target)
    vals = np.empty(idx.size, np.uint8)
    for i in range(0, idx.size, 2_000_000):
        part = np.asarray(X[idx[i:i + 2_000_000]], np.float32)
        vals[i:i + part.shape[0]] = np.round(model.predict_proba(part)[:, 1] * 100).astype(np.uint8)
    flat = np.zeros(int(sel.sum()), np.uint8)
    flat[idx] = vals
    prob[sel] = flat
    return prob, grid, d["z"]


def vectorize(prob: np.ndarray, grid: Grid, z: np.ndarray, threshold: int, min_px: int = 13,
              tol_m: float = 20.0, ndigits: int = 4) -> list[dict]:
    """Polygones des zones où la probabilité dépasse `threshold` (≥ `min_px` pixels, soit 0,5 ha)."""
    from rasterio.features import shapes
    from rasterio.warp import transform_geom
    from scipy.ndimage import label, sum_labels, mean

    from .foret import simplify_feature
    mask = prob >= threshold
    lab, n = label(mask)
    if n == 0:
        return []
    ids = np.arange(1, n + 1)
    size = sum_labels(mask, lab, ids)
    pmean = mean(prob, lab, ids)
    zmean = mean(np.nan_to_num(z), lab, ids)
    keep = np.zeros(n + 1, bool)
    keep[1:] = size >= min_px
    lab[~keep[lab]] = 0
    feats = []
    for geom, v in shapes(lab.astype(np.int32), mask=lab > 0, transform=grid.transform, connectivity=4):
        i = int(v) - 1
        g = transform_geom(grid.crs, "EPSG:4326", geom)
        if g["type"] == "Polygon":
            g = {"type": "MultiPolygon", "coordinates": [g["coordinates"]]}
        # Pixels de 20 m : simplification à 20 m et 4 décimales (~8 m) suffisent.
        f = simplify_feature({"type": "Feature", "properties": {"id": None}, "geometry": g}, tol_m, ndigits)
        if not f:
            continue
        f["properties"] = {"ha": f["properties"]["ha"], "p": int(round(pmean[i])), "z": int(round(zmean[i]))}
        feats.append(f)
    return feats
