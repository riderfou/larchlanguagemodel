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
    assert "meleze.geojson" in page and (tmp_path / "site" / "meleze.geojson").exists()
    assert "A </script> B" not in page                      # échappé dans le JSON inline
