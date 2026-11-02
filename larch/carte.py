"""Carte des mélézins (BD Forêt® V2) et des webcams : page Leaflet statique."""
from __future__ import annotations

import html
import json
import math
from pathlib import Path

GEOJSON_NAME = "meleze.geojson"
NEARBY_KM = 5.0


def _centroid(geom: dict) -> tuple[float, float]:
    ring = geom["coordinates"][0][0]
    return (sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring))


def _km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    k = math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot((lon2 - lon1) * k, lat2 - lat1) * 111.195


def larch_nearby(geojson: dict, cams: list[dict], radius_km: float = NEARBY_KM) -> dict[str, float]:
    """Surface (ha) de mélèze pur dont le centre est à moins de `radius_km` de chaque webcam."""
    cents = [(_centroid(f["geometry"]), f["properties"].get("ha", 0)) for f in geojson["features"]]
    out = {}
    for c in cams:
        if c.get("lat") is None:
            continue
        out[c["id"]] = round(sum(ha for (lon, lat), ha in cents
                                 if abs(lat - c["lat"]) < 0.1 and _km(c["lon"], c["lat"], lon, lat) <= radius_km))
    return out


TEMPLATE = r"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Carte des mélézins</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<style>
:root{--bg:#f6f4ef;--card:#fff;--ink:#22201c;--muted:#6b665d;--line:#e3ded3;--accent:#c47a00;
--larch:#e8a317;--larch-line:#9a5b00}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#171613;--card:#211f1b;
--ink:#eeeae2;--muted:#a39d91;--line:#38342d;--accent:#f0a830}}
:root[data-theme="dark"]{--bg:#171613;--card:#211f1b;--ink:#eeeae2;--muted:#a39d91;--line:#38342d;--accent:#f0a830}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg);color:var(--ink);
font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
#map{position:absolute;inset:0}
.panel{background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:10px;
box-shadow:0 2px 10px rgba(0,0,0,.15)}
#head{position:absolute;z-index:1000;top:10px;left:56px;right:56px;max-width:560px;padding:8px 12px}
#head h1{margin:0;font-size:1.05rem}#head p{margin:2px 0 0;font-size:.82rem;color:var(--muted)}
#head a{color:var(--accent);text-decoration:none;font-weight:600}
#legend{padding:8px 10px;font-size:.8rem;line-height:1.6}#legend b{display:block;margin-bottom:2px}
.sw{display:inline-block;width:12px;height:12px;border-radius:3px;vertical-align:-2px;margin-right:6px}
.dot{border-radius:50%;border:2px solid #fff;box-shadow:0 0 0 1px rgba(0,0,0,.35)}
#status{position:absolute;z-index:1000;bottom:28px;left:50%;transform:translateX(-50%);
padding:6px 12px;font-size:.85rem}
.leaflet-popup-content-wrapper,.leaflet-popup-tip{background:var(--card);color:var(--ink)}
.leaflet-popup-content{margin:10px 12px;font-size:.88rem;line-height:1.4;min-width:220px}
.leaflet-popup-content h3{margin:0 0 2px;font-size:1rem}
.leaflet-popup-content img{display:block;width:100%;max-width:300px;border-radius:6px;margin:6px 0}
.leaflet-popup-content a{color:var(--accent);font-weight:600;text-decoration:none}
.muted{color:var(--muted)}.big{font-size:1.4rem;font-weight:700;color:var(--accent)}
.leaflet-control-layers{background:var(--card);color:var(--ink);border-color:var(--line)!important}
.leaflet-bar a{background:var(--card);color:var(--ink);border-color:var(--line)}
.leaflet-container a.leaflet-popup-close-button{color:var(--muted)}
#legend summary{cursor:pointer;font-weight:600}#legend details[open] summary{margin-bottom:4px}
@media (max-width:640px){#head{left:52px;right:52px;font-size:.9rem;padding:6px 10px}
#head .desc{display:none}#legend{font-size:.75rem}}
</style></head><body>
<div id="map" role="application" aria-label="Carte des mélézins et des webcams"></div>
<div id="head" class="panel"><h1>Carte des mélézins</h1>
<p><span class="desc">Forêts de mélèze pur de la BD Forêt® V2 (IGN) et webcams suivies · </span><a
href="index.html">← Météo des mélèzes</a></p></div>
<div id="status" class="panel" role="status">Chargement des mélézins…</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script>
const CAMS = __CAMS__;
const STAGES = __STAGES__;
const NEARBY_KM = __NEARBY__;

function wmts(layer, fmt, style, opts) {
  return L.tileLayer("https://data.geopf.fr/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0" +
    "&LAYER=" + layer + "&STYLE=" + (style || "normal") + "&TILEMATRIXSET=PM&FORMAT=" + fmt +
    "&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}",
    Object.assign({maxZoom: 18, attribution: '&copy; <a href="https://www.ign.fr/">IGN</a> – Géoplateforme'}, opts));
}
const plan = wmts("GEOGRAPHICALGRIDSYSTEMS.PLANIGNV2", "image/png", "normal", {maxNativeZoom: 19});
const ortho = wmts("ORTHOIMAGERY.ORTHOPHOTOS", "image/jpeg", "normal", {maxNativeZoom: 19});
const relief = wmts("ELEVATION.ELEVATIONGRIDCOVERAGE.SHADOW", "image/png", "estompage_grayscale",
  {maxNativeZoom: 15, opacity: 0.45});
const foretV2 = wmts("LANDCOVER.FORESTINVENTORY.V2", "image/png", "normal",
  {minZoom: 6, maxNativeZoom: 16, opacity: 0.55,
   attribution: '&copy; IGN – BD Forêt® V2'});

const map = L.map("map", {preferCanvas: true, zoomControl: true, layers: [plan]});
map.attributionControl.setPrefix('<a href="https://leafletjs.com">Leaflet</a>');
L.control.scale({imperial: false}).addTo(map);

const fr = new Intl.NumberFormat("fr-FR", {maximumFractionDigits: 1});
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

// --- Webcams ---------------------------------------------------------------
const camLayer = L.layerGroup();
const camBounds = [];
for (const c of CAMS) {
  if (c.lat == null) continue;
  const st = STAGES[c.stage] || STAGES.inconnu;
  const m = L.circleMarker([c.lat, c.lon], {radius: 9, weight: 2.5, color: "#fff",
    fillColor: st[1], fillOpacity: 1, pane: "markerPane"});
  const gold = c.gold_index == null ? "—" : Math.round(c.gold_index);
  const near = c.larch_nearby_ha == null ? "" :
    `<div class="muted">Mélèze pur à moins de ${NEARBY_KM} km : ${fr.format(c.larch_nearby_ha)} ha</div>`;
  m.bindPopup(`<h3>${esc(c.name)}</h3>
    <div style="color:${st[1]};font-weight:600">${esc(st[0])}</div>
    ${c.image ? `<img src="${esc(c.image)}" alt="Dernière image de ${esc(c.name)}" loading="lazy">` : ""}
    <div><span class="big">${gold}</span><span class="muted"> / 100 · indice d'or</span></div>
    <div>Pic : <b>${esc(c.peak)}</b> <span class="muted">(${esc(c.peak_source)})</span></div>
    <div class="muted">Altitude des mélèzes visibles : ${Math.round(c.elevation)} m</div>
    ${near}
    <div style="margin-top:6px"><a href="index.html#${encodeURIComponent(c.id)}">Voir la fiche →</a></div>`,
    {maxWidth: 320});
  m.bindTooltip(esc(c.name), {direction: "top", offset: [0, -8]});
  m.addTo(camLayer);
  camBounds.push([c.lat, c.lon]);
}
camLayer.addTo(map);
if (camBounds.length) map.fitBounds(camBounds, {padding: [60, 60], maxZoom: 10});
else map.setView([45.0, 6.6], 8);

// --- Mélézins BD Forêt V2 ---------------------------------------------------
const larchStyle = {color: getComputedStyle(document.documentElement).getPropertyValue("--larch-line").trim() || "#9a5b00",
  weight: 1, fillColor: "#e8a317", fillOpacity: 0.5};
const larch = L.geoJSON(null, {
  style: () => larchStyle,
  onEachFeature: (f, l) => l.bindPopup(
    `<h3>Forêt fermée de mélèze pur</h3>
     <div><span class="big">${fr.format(f.properties.ha)}</span> <span class="muted">ha</span></div>
     <div class="muted">BD Forêt® V2 (IGN) · code TFV FF2-63-63<br>Identifiant ${esc(f.properties.id)}</div>`)
});
larch.addTo(map);

const layers = L.control.layers(
  {"Plan IGN": plan, "Photographies aériennes": ortho},
  {"Mélèze pur (BD Forêt V2)": larch,
   "Toutes formations forestières (BD Forêt V2)": foretV2,
   "Relief (ombrage)": relief,
   "Webcams": camLayer},
  {collapsed: window.matchMedia("(max-width: 640px)").matches}).addTo(map);
// Les webcams toujours au-dessus des polygones
map.on("overlayadd", () => camLayer.eachLayer(l => l.bringToFront && l.bringToFront()));

const status = document.getElementById("status");
fetch("__GEOJSON__").then(r => {
  if (!r.ok) throw new Error(r.status + " " + r.statusText);
  return r.json();
}).then(fc => {
  larch.addData(fc);
  camLayer.eachLayer(l => l.bringToFront());
  const ha = fc.features.reduce((s, f) => s + (f.properties.ha || 0), 0);
  status.textContent = `${fr.format(fc.features.length)} forêts de mélèze pur · ${fr.format(Math.round(ha))} ha`;
  setTimeout(() => status.remove(), 4000);
}).catch(e => {
  status.textContent = "Mélézins indisponibles (" + e.message + ")";
});

// --- Légende -----------------------------------------------------------------
const legend = L.control({position: "bottomleft"});
legend.onAdd = () => {
  const d = L.DomUtil.create("div", "panel");
  d.id = "legend";
  const open = window.matchMedia("(max-width: 640px)").matches ? "" : " open";
  d.innerHTML = `<details${open}><summary>Légende</summary>
    <b>Mélézins</b><span class="sw" style="background:#e8a317;border:1px solid #9a5b00"></span>Forêt fermée de mélèze pur<br>
    <b style="margin-top:4px">Webcams</b>` +
    Object.entries(STAGES).filter(([k]) => CAMS.some(c => c.stage === k) || k !== "inconnu")
      .map(([, [label, color]]) => `<span class="sw dot" style="background:${color}"></span>${esc(label)}`).join("<br>") +
    "</details>";
  L.DomEvent.disableClickPropagation(d);
  return d;
};
legend.addTo(map);
</script></body></html>
"""


def render_map(cams: list[dict], stages: dict, out_dir: Path, larch_geojson: Path | None) -> Path:
    """Écrit `carte.html` (et copie le GeoJSON des mélézins) dans `out_dir`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    cams = [dict(c) for c in cams]
    if larch_geojson and larch_geojson.exists():
        raw = larch_geojson.read_bytes()
        (out_dir / GEOJSON_NAME).write_bytes(raw)
        near = larch_nearby(json.loads(raw), cams)
        for c in cams:
            c["larch_nearby_ha"] = near.get(c["id"])
    keep = ("id", "name", "lat", "lon", "elevation", "stage", "gold_index", "peak", "peak_source",
            "image", "larch_nearby_ha")
    payload = [{k: c.get(k) for k in keep} for c in cams]

    def js(obj) -> str:  # JSON sûr dans un <script>
        return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")

    doc = (TEMPLATE.replace("__CAMS__", js(payload))
           .replace("__STAGES__", js({k: list(v) for k, v in stages.items()}))
           .replace("__NEARBY__", js(NEARBY_KM))
           .replace("__GEOJSON__", html.escape(GEOJSON_NAME)))
    path = out_dir / "carte.html"
    path.write_text(doc, encoding="utf-8")
    return path
