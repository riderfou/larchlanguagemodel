from datetime import date, timedelta

import numpy as np
from PIL import Image

from larch import analysis, phenology
from larch.fetch import FetchError, candidate_images_from_html, skaping_last_media, skaping_media_url
from larch.store import DailyPoint


def scene(needle_rgb, sky=(120, 170, 230), w=400, h=300, seed=0):
    """Ciel en haut, forêt texturée en bas."""
    rng = np.random.default_rng(seed)
    a = np.zeros((h, w, 3), dtype=np.float32)
    a[: h // 3] = sky
    forest = np.array(needle_rgb, dtype=np.float32) * rng.uniform(0.6, 1.15, (h - h // 3, w, 1))
    a[h // 3:] = forest
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


FOREST_ROI = [[(0, 0.4), (1, 0.4), (1, 1), (0, 1)]]


def test_green_forest_is_green():
    s = analysis.analyze(scene((60, 120, 50)), FOREST_ROI)
    assert s.valid and s.gold_index < 5 and s.green > 0.9


def test_golden_larch_is_gold():
    s = analysis.analyze(scene((220, 170, 40)), FOREST_ROI)
    assert s.valid and s.gold_index > 90 and s.yellow > 0.5


def test_orange_larch():
    s = analysis.analyze(scene((210, 110, 30)), FOREST_ROI)
    assert s.valid and s.gold_index > 90 and s.orange > 0.5


def test_sky_ignored_without_roi():
    s = analysis.analyze(scene((220, 170, 40)))
    assert s.valid and s.gold_index > 90


def test_night_and_fog_invalid():
    assert not analysis.analyze(scene((10, 12, 8), sky=(5, 5, 8))).valid
    flat = Image.new("RGB", (400, 300), (190, 192, 195))
    assert not analysis.analyze(flat).valid


def test_snow_invalid():
    snow = scene((240, 242, 245), sky=(235, 238, 242))
    assert not analysis.analyze(snow, FOREST_ROI).valid


def test_mixed_proportion():
    a = np.asarray(scene((60, 120, 50))).copy()
    b = np.asarray(scene((220, 170, 40)))
    a[:, 200:] = b[:, 200:]
    s = analysis.analyze(Image.fromarray(a), FOREST_ROI)
    assert 40 < s.gold_index < 60


def season(values, start=date(2026, 9, 20), veg=None):
    return [DailyPoint(start + timedelta(days=i), v, (veg or [0.8] * len(values))[i], 0.35, "#aa9933", 3)
            for i, v in enumerate(values)]


def test_stages():
    assert phenology.stage([]) == "inconnu"
    assert phenology.stage(season([2, 3, 4])) == "vert"
    assert phenology.stage(season([2, 8, 15, 22])) == "debut"
    assert phenology.stage(season([10, 30, 50, 60])) == "en_cours"
    assert phenology.stage(season([30, 60, 80, 85, 86])) == "pic"
    pts = season([30, 60, 80, 88, 86, 70, 60, 55])
    assert phenology.stage(pts) == "declin"
    assert phenology.observed_peak(pts) == date(2026, 9, 23)


def test_needle_drop_detected_by_veg_share():
    pts = season([60, 80, 85, 85, 84], veg=[0.8, 0.8, 0.8, 0.5, 0.4])
    assert phenology.stage(pts) == "declin"


def test_logistic_fit_recovers_peak():
    t0, s = 18, 4.0  # mi-coloration le 8 oct.
    vals = [5 + 85 / (1 + np.exp(-(i - t0) / s)) for i in range(16)]  # observé jusqu'au 5 oct.
    fit = phenology.fit_logistic(season([round(v, 1) for v in vals]))
    true_peak = date(2026, 9, 20) + timedelta(days=round(t0 + 2.197 * s))
    assert fit is not None
    assert abs((fit.peak - true_peak).days) <= 3
    assert fit.peak_min <= true_peak <= fit.peak_max


def test_logistic_needs_signal():
    assert phenology.fit_logistic(season([3, 4, 3, 5, 4, 3])) is None


def test_cold_model():
    start = date(2026, 8, 1)
    temps = {start + timedelta(days=i): 12 - 0.15 * i for i in range(120)}
    m = phenology.cold_model(temps, start, date(2026, 9, 1), base=10, threshold=100)
    assert m.peak is not None and date(2026, 9, 15) < m.peak < date(2026, 10, 15)
    assert 0 < m.progress < 1


def test_html_candidates():
    html = ('<meta property="og:image" content="/cam/last.jpg"><img src="logo.png">'
            '<script>var u="https://cdn.x.com/a/b.jpg?t=1"</script>')
    c = candidate_images_from_html(html, "https://site.fr/webcams")
    assert c[0] == "https://site.fr/cam/last.jpg"
    assert "https://cdn.x.com/a/b.jpg?t=1" in c
    assert not any("logo" in u for u in c)


def test_bare_days_after_peak_mean_decline():
    pts = season([40, 70, 85, 86])
    after = [date(2026, 9, 24), date(2026, 9, 25)]
    assert phenology.stage(pts) == "pic"
    assert phenology.stage(pts, after) == "declin"
    assert phenology.observed_peak(pts, after) == date(2026, 9, 23)


SKAPING_HTML = (
    '{"camera_name":"Serre Chevalier - Cucumelle","player_url":"https:\\/\\/www.skaping.com\\/serre-chevalier\\/cucumelle",'
    '"last_media":"https:\\/\\/skaping.s3.gra.io.cloud.ovh.net\\/serre-chevalier\\/cucumelle\\/2026\\/11\\/02\\/mini\\/15-41.jpg"},'
    '"povs":{"2897":{"type_of_view":"photo - 360","is_online":true,'
    '"player_url":"https:\\/\\/www.skaping.com\\/serre-chevalier\\/mea","player_id":"2940",'
    '"last_media":"https:\\/\\/skaping.s3.gra.io.cloud.ovh.net\\/serre-chevalier\\/mea\\/2026\\/11\\/02\\/mini\\/15-50.jpg"},'
    '"2907":{"type_of_view":"video - fixe","player_url":"https:\\/\\/www.skaping.com\\/serre-chevalier\\/mea\\/video",'
    '"last_media":"https:\\/\\/skaping.s3.gra.io.cloud.ovh.net\\/serre-chevalier\\/mea\\/video\\/2026\\/11\\/02\\/15-45.jpg"}}'
)


def test_skaping_last_media_picks_the_right_view():
    lm = skaping_last_media(SKAPING_HTML, "https://skaping.com/serre-chevalier/mea/")
    assert lm == "https://skaping.s3.gra.io.cloud.ovh.net/serre-chevalier/mea/2026/11/02/mini/15-50.jpg"
    lm = skaping_last_media(SKAPING_HTML, "http://www.skaping.com/serre-chevalier/mea/video")
    assert lm.endswith("/mea/video/2026/11/02/15-45.jpg")
    try:
        skaping_last_media(SKAPING_HTML, "https://www.skaping.com/ailleurs/vue")
    except FetchError:
        pass
    else:
        raise AssertionError("vue inexistante non signalée")


def test_skaping_media_url_quality():
    lm = "https://h/serre-chevalier/mea/2026/11/02/mini/15-50.jpg"
    assert skaping_media_url(lm, "large") == "https://h/serre-chevalier/mea/2026/11/02/large/15-50.jpg"
    assert skaping_media_url(lm, "full") == "https://h/serre-chevalier/mea/2026/11/02/15-50.jpg"
    video = "https://h/serre-chevalier/mea/video/2026/11/02/15-45.jpg"
    assert skaping_media_url(video, "large") == video


def _square(lon, lat, side_deg, n=50):
    """Carré fermé avec beaucoup de points alignés sur les côtés."""
    pts = []
    for k in range(n):
        pts.append([lon + side_deg * k / n, lat])
    for k in range(n):
        pts.append([lon + side_deg, lat + side_deg * k / n])
    for k in range(n):
        pts.append([lon + side_deg - side_deg * k / n, lat + side_deg])
    for k in range(n):
        pts.append([lon, lat + side_deg - side_deg * k / n])
    return pts + [pts[0]]


def test_foret_simplify_keeps_shape_and_area():
    from larch import foret
    sq = _square(6.6, 44.6, 0.01)  # ~790 m x 1110 m
    f = {"type": "Feature", "properties": {"id": "X"},
         "geometry": {"type": "MultiPolygon", "coordinates": [[sq]]}}
    out = foret.simplify_feature(f, tol_m=8)
    ring = out["geometry"]["coordinates"][0][0]
    assert len(ring) == 5 and ring[0] == ring[-1]          # 4 coins + fermeture
    assert 85 < out["properties"]["ha"] < 90               # 0.01° x 0.01° à 44.6° N ≈ 88 ha


def test_foret_hole_area_is_subtracted():
    from larch import foret
    outer, hole = _square(6.6, 44.6, 0.01), _square(6.602, 44.602, 0.005)
    f = {"type": "Feature", "properties": {"id": "Y"},
         "geometry": {"type": "MultiPolygon", "coordinates": [[outer, hole]]}}
    ha = foret.simplify_feature(f, tol_m=8)["properties"]["ha"]
    assert 64 < ha < 68                                     # 88 - 22 ha


def test_larch_nearby():
    from larch.carte import larch_nearby
    fc = {"features": [
        {"properties": {"ha": 10}, "geometry": {"coordinates": [[_square(6.70, 44.57, 0.002)]]}},
        {"properties": {"ha": 7}, "geometry": {"coordinates": [[_square(6.90, 44.57, 0.002)]]}},
    ]}
    cams = [{"id": "a", "lat": 44.575, "lon": 6.699}, {"id": "b", "lat": None, "lon": None}]
    assert larch_nearby(fc, cams) == {"a": 10}


def test_map_page_is_generated(tmp_path):
    import json as _json
    from larch.carte import render_map
    from larch.phenology import STAGES
    geo = tmp_path / "m.geojson"
    geo.write_text(_json.dumps({"type": "FeatureCollection", "features": []}))
    cams = [{"id": "x", "name": "A </script> B", "lat": 45.0, "lon": 6.5, "elevation": 1900,
             "stage": "pic", "gold_index": 88.0, "peak": "20 oct.", "peak_source": "pic observé",
             "image": "img/x.jpg"}]
    page = render_map(cams, STAGES, tmp_path / "site", geo).read_text()
    assert "meleze.json" in page and (tmp_path / "site" / "meleze.json").exists()
    assert "const HAS_MIXED = false" in page
    assert "A </script> B" not in page                      # échappé dans le JSON inline


def test_build_mixed_filters_by_elevation(tmp_path, monkeypatch):
    from larch import foret

    def fake_download(code, bbox=None):
        assert bbox == foret.ALPS_BBOX
        return [{"type": "Feature", "properties": {"id": f"{code}-{k}"},
                 "geometry": {"type": "MultiPolygon",
                              "coordinates": [[_square(6.6 + k * 0.1, 44.6, 0.01)]]}} for k in range(2)]

    # 1er polygone de chaque code en altitude, le 2e en vallée
    monkeypatch.setattr(foret, "download", fake_download)
    monkeypatch.setattr(foret, "elevations", lambda pts: [1800.0, 900.0][:len(pts)])
    info = foret.build_mixed(tmp_path / "m.geojson", log=lambda *_: None)
    fc = _json_load(tmp_path / "m.geojson")
    assert info["features"] == len(foret.MIXED_CODES)
    assert {f["properties"]["t"] for f in fc["features"]} == {"melange", "ouverte"}
    assert all(f["properties"]["z"] == 1800 for f in fc["features"])


def _json_load(path):
    import json as _json
    return _json.loads(path.read_text())


def test_cql_bbox_is_lon_lat():
    from larch import foret
    assert foret.cql("FO2", (5.5, 43.7, 7.8, 46.5)) == \
        "code_tfv='FO2' AND BBOX(geom,5.5,43.7,7.8,46.5,'CRS:84')"


# --- Sentinel-2 (hors ligne) ---------------------------------------------------

def test_pick_scenes_prefers_clear_full_tiles():
    from larch.sentinel import Scene, pick_scenes
    sc = [Scene("31TGL", d, "p/", c, 0, n) for d, c, n in [
        ("2026-07-04", 1.0, 0), ("2026-07-05", 0.0, 58), ("2026-07-24", 0.0, 0), ("2026-10-12", 0.0, 0)]]
    best = pick_scenes(sc, "2026-07-01", "2026-08-31", 2, 1, max_nodata=5)
    assert [s.date for s in best] == ["2026-07-24"]


def test_features_capture_autumn_yellowing():
    from larch import sentinel as s2
    n = 4
    green = {"blue": .03, "green": .07, "red": .04, "re1": .1, "re2": .25, "re3": .3, "nir": .35,
             "nir2": .36, "swir1": .15, "swir2": .07}
    summer = {k: np.full(n, v, np.float32) for k, v in green.items()}
    gold = {"blue": .04, "green": .09, "red": .10, "re1": .13, "re2": .18, "nir": .22, "swir1": .2}
    # pixels : mélèze doré, mélèze doré, épicéa (reste vert), nuage
    autumn = [{k: np.array([gold[k], gold[k], green[k], np.nan], np.float32) for k in gold}]
    X, names = s2.features(summer, autumn, np.full(n, 1800, np.float32))
    col = dict(zip(names, X.T))
    assert X.shape == (n, len(names))
    assert col["delta_ndvi"][0] < -0.2 and abs(col["delta_ndvi"][2]) < 0.01  # le mélèze perd du NDVI
    assert col["aut_rouge_vert_max"][0] > col["aut_rouge_vert_max"][2]   # et rougit
    assert np.isnan(col["aut_ndvi_min"][3])                              # date nuageuse : NaN


def test_evaluate_uses_weights():
    from larch.sentinel import LARCH, evaluate

    class Fixed:
        def predict_proba(self, X):
            p = X[:, 0]
            return np.column_stack([1 - p, p])

    # 10 mélèzes bien vus, 10 épicéas échantillonnés au 1/10 dont 1 fausse alarme
    X = np.array([[0.95]] * 10 + [[0.95]] + [[0.1]] * 9)
    y = np.array([LARCH] * 10 + [2] * 10)
    w = np.array([1.0] * 10 + [10.0] * 10)
    r = next(r for r in evaluate(Fixed(), X, y, w)["seuils"] if r["seuil"] == 0.9)
    assert r["rappel"] == 1.0 and r["precision"] == 0.5                  # 10 vrais / (10 + 1×10)


def test_vectorize_keeps_stands_and_drops_specks():
    from affine import Affine
    from larch.sentinel import Grid, vectorize
    grid = Grid("EPSG:32632", Affine(20, 0, 300000, 0, -20, 5000000), 60, 60)
    prob = np.zeros((60, 60), np.uint8)
    prob[10:30, 10:25] = 95          # peuplement de 20 x 15 px = 12 ha
    prob[50, 50] = 99                # pixel isolé : ignoré
    feats = vectorize(prob, grid, np.full((60, 60), 1900.0), threshold=90)
    assert len(feats) == 1
    assert 11 < feats[0]["properties"]["ha"] < 13
    assert feats[0]["properties"]["p"] == 95 and feats[0]["properties"]["z"] == 1900


# --- Suivi satellite (hors ligne) ---------------------------------------------

def _zone_mask(rg_summer=-0.10, ndvi_summer=0.80, n=40):
    from larch import satsuivi
    from larch.sentinel import Grid
    from affine import Affine
    z = satsuivi.Zone("z", "Zone", 45.0, 6.5, 4.0)
    mask = np.zeros((n, n), bool); mask[:, : n // 2] = True       # mélèze à gauche
    ref = np.zeros((n, n), bool); ref[:, n // 2:] = True           # témoins à droite
    return satsuivi.ZoneMask(z, "32TLR", (0, 0, n, n), Grid("EPSG:32632", Affine.identity(), n, n),
                             mask, ref, np.full((n, n), rg_summer, np.float32),
                             np.full((n, n), ndvi_summer, np.float32), "2026-07-24")


def _bands(n, larch_rg, ref_rg, larch_ndvi=0.8, ref_ndvi=0.8):
    """Réflectances donnant les (R-G)/(R+G) et NDVI voulus à gauche (mélèze) et à droite."""
    out = {k: np.zeros((n, n), np.float32) for k in ("red", "green", "blue", "nir")}
    for sl, rg, nd in ((np.s_[:, : n // 2], larch_rg, larch_ndvi), (np.s_[:, n // 2:], ref_rg, ref_ndvi)):
        g = 0.08
        r = g * (1 + rg) / (1 - rg)
        out["green"][sl], out["red"][sl], out["blue"][sl] = g, r, 0.04
        out["nir"][sl] = r * (1 + nd) / (1 - nd)
    return out


def test_measure_subtracts_evergreen_drift():
    from larch import satsuivi
    zm = _zone_mask()
    # tout rougit de 0,03 (soleil bas), le mélèze de 0,03 + 0,06 de plus
    m = satsuivi.measure(zm, _bands(40, -0.10 + 0.09, -0.10 + 0.03))
    assert m["valid"] == 1
    assert abs(m["delta_meleze"] - 0.09) < 1e-3 and abs(m["delta_temoins"] - 0.03) < 1e-3
    assert abs(m["gold_index"] - 0.06 / satsuivi.GOLD_DELTA * 100) < 1


def test_measure_rejects_clouds_and_haze():
    from larch import satsuivi
    zm = _zone_mask()
    b = _bands(40, -0.05, -0.10)
    b["red"][:, :18] = np.nan                      # 90 % du mélèze sous les nuages
    assert satsuivi.measure(zm, b)["reason"].startswith("nuages")
    hazy = _bands(40, -0.05, -0.10, ref_ndvi=0.55)  # témoins à 69 % de leur NDVI d'été
    assert satsuivi.measure(zm, hazy)["reason"].startswith("voile")


def _sat_rows(series):
    rows = []
    for day, d_larch, ratio in series:
        rows.append({"cam": "s2:z", "valid": "1", "timestamp": f"{day}T12:38", "delta_meleze": str(d_larch),
                     "delta_temoins": "0.0", "n_ref": "500", "ndvi_ratio_temoins": "1.0",
                     "veg_share": str(__import__("larch.satsuivi", fromlist=["x"]).veg_share(ratio)),
                     "gcc": "0.4", "mean_rgb": "#888888", "brightness": "0.04"})
    return rows


def test_daily_points_baseline_peak_and_needle_drop():
    from larch import phenology, satsuivi
    D = satsuivi.GOLD_DELTA
    series = [("2026-09-02", 0.02, 0.97), ("2026-09-07", 0.02, 0.96), ("2026-09-12", 0.02, 0.97),
              ("2026-10-02", 0.02 + 0.3 * D, 0.9), ("2026-10-07", 0.02 + 0.5 * D, 0.85),
              ("2026-10-12", 0.02 + 0.8 * D, 0.8), ("2026-10-17", 0.02 + 1.0 * D, 0.75),
              ("2026-10-24", 0.02 + 1.2 * D, 0.6), ("2026-10-27", 0.02 + 1.3 * D, 0.62),
              ("2026-10-30", 0.02 + 1.5 * D, 0.62), ("2026-11-01", 0.02 + 1.5 * D, 0.62)]
    pts, base = satsuivi.daily_points(_sat_rows(series), "s2:z")
    assert base == 0.02 and pts[0].gold_index == 0                 # zéro de début septembre
    assert [p.gold_index for p in pts][3:6] == [30, 50, 80]
    keep, bare = satsuivi.split_bare(pts)
    assert len(bare) == 4                                          # aiguilles tombées (NDVI)
    assert phenology.stage(keep, bare) == "declin"


def test_daily_points_drops_isolated_outlier():
    from larch import satsuivi
    D = satsuivi.GOLD_DELTA
    series = [("2026-09-02", 0.0, 0.97), ("2026-09-05", 0.7 * D, 0.97), ("2026-09-08", 0.0, 0.97),
              ("2026-09-10", 0.0, 0.97)]
    pts, _ = satsuivi.daily_points(_sat_rows(series), "s2:z")
    assert [p.day.day for p in pts] == [2, 8, 10]


def test_compare_webcam_and_satellite():
    from datetime import date as _d
    from larch.satsuivi import compare
    from larch.store import DailyPoint
    cam = [DailyPoint(_d(2026, 10, d), v, 1, 0.4, "#888", 1) for d, v in ((10, 40), (11, 50), (15, 80))]
    sat = [DailyPoint(_d(2026, 10, d), v, 1, 0.4, "#888", 1) for d, v in ((12, 40), (15, 70), (25, 90))]
    k = compare(cam, sat)
    assert k == {"jours": 2, "ecart_moyen": 10.0, "ecart_absolu": 10.0}


def test_fetch_daylight_only_skips_outside_hours(tmp_path, monkeypatch):
    from datetime import datetime as _dt
    from larch import cli

    def boom(*a, **k):
        raise AssertionError("aucune webcam ne doit être relevée hors de la plage horaire")

    monkeypatch.setattr(cli, "fetch_cam", boom)
    monkeypatch.setattr(cli, "_now", lambda cfg: _dt(2026, 11, 4, 18, 20))
    assert cli.main(["--data", str(tmp_path), "fetch", "--daylight-only"]) == 0
    assert not (tmp_path / "history.csv").exists()
