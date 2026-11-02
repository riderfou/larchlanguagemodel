"""Génération de la page « météo des mélèzes » (HTML statique)."""
from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from .carte import render_map
from .phenology import STAGES, ColdModel, LogisticFit
from .store import DailyPoint

MOIS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.",
        "oct.", "nov.", "déc."]


def fr_date(d: date | None) -> str:
    return f"{d.day} {MOIS[d.month - 1]}" if d else "—"


@dataclass
class SatInfo:
    """Suivi Sentinel-2 d'une zone (autour d'une webcam ou massif sans webcam)."""
    points: list[DailyPoint]
    bare: list[date]
    stage: str
    fit: LogisticFit | None = None
    observed_peak: date | None = None
    hectares: float | None = None
    radius_km: float | None = None
    comparison: dict | None = None      # écart webcam - satellite (jours communs)

    @property
    def gold(self) -> float | None:
        return self.points[-1].gold_index if self.points else None

    def peak_summary(self) -> tuple[str, str]:
        return peak_text(self.observed_peak, self.fit, None)

    def to_json(self) -> dict:
        peak, src = self.peak_summary()
        return {"stage": self.stage, "stage_label": STAGES[self.stage][0], "gold_index": self.gold,
                "last_day": self.points[-1].day.isoformat() if self.points else None,
                "peak": peak, "peak_source": src, "hectares": self.hectares, "radius_km": self.radius_km,
                "comparison": self.comparison,
                "series": [{"day": p.day.isoformat(), "gold_index": p.gold_index,
                            "veg_share": p.veg_share} for p in self.points]}


def peak_text(observed: date | None, fit: LogisticFit | None, cold: ColdModel | None) -> tuple[str, str]:
    """(date du pic, provenance)"""
    if observed:
        return fr_date(observed), "pic observé"
    if fit:
        rng = "" if fit.peak_min == fit.peak_max else f" ({fr_date(fit.peak_min)} – {fr_date(fit.peak_max)})"
        return fr_date(fit.peak) + rng, "d'après l'évolution observée"
    if cold and cold.peak:
        src = "modèle froid cumulé" + (" + tendance saisonnière" if cold.extrapolated else "")
        return "vers le " + fr_date(cold.peak), src
    return "—", "pas encore assez de données"


@dataclass
class CamReport:
    id: str
    name: str
    elevation: float
    stage: str
    points: list[DailyPoint]
    last: dict | None = None            # dernière mesure (valide ou non)
    last_time: datetime | None = None
    image: str | None = None            # chemin relatif dans le site
    overlay: str | None = None
    fit: LogisticFit | None = None
    cold: ColdModel | None = None
    observed_peak: date | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)
    lat: float | None = None
    lon: float | None = None
    kind: str = "webcam"                # "webcam" ou "satellite" (massif sans webcam)
    sat: SatInfo | None = None

    @property
    def gold(self) -> float | None:
        return self.points[-1].gold_index if self.points else None

    def peak_summary(self) -> tuple[str, str]:
        """(date du pic, provenance)"""
        return peak_text(self.observed_peak, self.fit, self.cold)

    def to_json(self) -> dict:
        peak, src = self.peak_summary()
        return {
            "id": self.id, "name": self.name, "stage": self.stage,
            "lat": self.lat, "lon": self.lon, "elevation": self.elevation, "image": self.image,
            "kind": self.kind, "satellite": self.sat.to_json() if self.sat else None,
            "stage_label": STAGES[self.stage][0], "gold_index": self.gold,
            "peak": peak, "peak_source": src,
            "observed_peak": self.observed_peak.isoformat() if self.observed_peak else None,
            "fit_peak": self.fit.peak.isoformat() if self.fit else None,
            "cold_model_peak": self.cold.peak.isoformat() if self.cold and self.cold.peak else None,
            "cold_progress": round(self.cold.progress, 2) if self.cold else None,
            "last_measure": self.last, "error": self.error,
            "series": [{"day": p.day.isoformat(), "gold_index": p.gold_index,
                        "veg_share": p.veg_share} for p in self.points],
        }


def _sparkline(c: CamReport, today: date, w: int = 320, h: int = 90) -> str:
    sat = c.sat.points if (c.sat and c.kind == "webcam") else []
    start = min([p.day for p in c.points] + [p.day for p in sat] + [today - timedelta(days=30)])
    end = max(today + timedelta(days=10), c.fit.peak + timedelta(days=5) if c.fit else today)
    span = max((end - start).days, 1)
    X = lambda d: 4 + (w - 8) * (d - start).days / span  # noqa: E731
    Y = lambda v: h - 14 - (h - 22) * max(0, min(100, v)) / 100  # noqa: E731
    parts = [f'<svg viewBox="0 0 {w} {h}" class="spark" role="img" '
             f'aria-label="Évolution de l\'indice d\'or">']
    for v in (35, 70):
        parts.append(f'<line x1="4" x2="{w-4}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" class="grid"/>')
    if c.fit:
        pts, d = [], max(start, c.fit.t0 - timedelta(days=int(4 * c.fit.scale)))
        while d <= end:
            pts.append(f"{X(d):.1f},{Y(c.fit.predict(d)):.1f}")
            d += timedelta(days=1)
        parts.append(f'<polyline points="{" ".join(pts)}" class="fit"/>')
    if c.points:
        line = " ".join(f"{X(p.day):.1f},{Y(p.gold_index):.1f}" for p in c.points)
        parts.append(f'<polyline points="{line}" class="obs"/>')
        for p in c.points:
            parts.append(f'<circle cx="{X(p.day):.1f}" cy="{Y(p.gold_index):.1f}" r="2.2" '
                         f'fill="{html.escape(p.mean_rgb)}" class="dot"><title>{fr_date(p.day)} : '
                         f'{p.gold_index:.0f}</title></circle>')
    for p in sat:   # satellite autour de la webcam : petits carrés
        parts.append(f'<rect x="{X(p.day) - 2.2:.1f}" y="{Y(p.gold_index) - 2.2:.1f}" width="4.4" '
                     f'height="4.4" class="sat"><title>Satellite, {fr_date(p.day)} : '
                     f'{p.gold_index:.0f}</title></rect>')
    xt = X(today)
    parts.append(f'<line x1="{xt:.1f}" x2="{xt:.1f}" y1="4" y2="{h-14}" class="today"/>')
    parts.append(f'<text x="{xt:.1f}" y="{h-2}" class="lbl" text-anchor="middle">auj.</text>')
    parts.append(f'<text x="4" y="{h-2}" class="lbl">{fr_date(start)}</text>')
    parts.append(f'<text x="{w-4}" y="{h-2}" class="lbl" text-anchor="end">{fr_date(end)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _card(c: CamReport, today: date) -> str:
    label, color = STAGES[c.stage]
    peak, src = c.peak_summary()
    gold = "—" if c.gold is None else f"{c.gold:.0f}"
    e = html.escape
    img = (f'<figure class="shot"><img src="{e(c.image)}" alt="Dernière image {e(c.name)}" loading="lazy">'
           + (f'<img src="{e(c.overlay)}" class="ov" alt="" loading="lazy">' if c.overlay else "")
           + "</figure>") if c.image else '<figure class="shot empty">pas d\'image</figure>'
    bars = ""
    if c.last and c.last.get("valid"):
        L = c.last
        bars = ('<div class="bars" title="Répartition des aiguilles visibles">'
                f'<i style="flex:{L["green"]};background:var(--g)"></i>'
                f'<i style="flex:{L["yellow"]};background:var(--y)"></i>'
                f'<i style="flex:{L["orange"]};background:var(--o)"></i></div>'
                f'<div class="legend"><span>vert {L["green"]*100:.0f} %</span>'
                f'<span>jaune {L["yellow"]*100:.0f} %</span><span>orangé {L["orange"]*100:.0f} %</span></div>')
    outlook = ""
    if c.fit and c.stage not in ("declin",):
        outlook = '<div class="outlook">' + "".join(
            f'<div><b>{fr_date(today + timedelta(days=k))}</b>{min(100, c.fit.predict(today + timedelta(days=k))):.0f}</div>'
            for k in (3, 7, 14)) + "</div>"
    cold = ""
    if c.cold:
        cold = (f'<p class="muted">Froid cumulé : {c.cold.accumulated:.0f} / {c.cold.threshold:.0f} '
                f'degrés-jours ({c.cold.progress*100:.0f} %)</p>')
    warn = ""
    if c.error:
        warn = f'<p class="warn">Image indisponible : {e(c.error)}</p>'
    elif c.last and not c.last.get("valid"):
        warn = f'<p class="warn">Dernière image inexploitable : {e(c.last.get("reason", ""))}</p>'
    if c.kind == "satellite":
        return _sat_card(c, today)
    when = f"mis à jour {c.last_time:%d/%m %H:%M}" if c.last_time else ""
    swatch = c.points[-1].mean_rgb if c.points else "#888"
    return f"""
<article class="card" id="{e(c.id)}">
  {img}
  <div class="body">
    <header><h2>{e(c.name)}</h2><span class="muted">{c.elevation:.0f} m · {when}</span></header>
    <div class="now">
      <span class="swatch" style="background:{e(swatch)}"></span>
      <div><div class="stage" style="--c:{color}">{label}</div>
      <div class="muted">indice d'or <b class="big">{gold}</b>/100</div></div>
    </div>
    {bars}
    <div class="peak"><span class="muted">Pic des couleurs</span><b>{peak}</b><small>{e(src)}</small></div>
    {outlook}
    {_sparkline(c, today)}
    {_sat_block(c)}
    {cold}{warn}
  </div>
</article>"""


def _sat_block(c: CamReport) -> str:
    """Satellite autour d'une webcam : stade, dernier indice, écart avec la webcam."""
    si = c.sat
    if not si:
        return ""
    e = html.escape
    if not si.points:
        return ('<p class="satline muted"><span class="satkey"></span>Satellite : '
                'pas encore de passage sans nuages sur les mélèzes voisins</p>')
    label, color = STAGES[si.stage]
    last = si.points[-1]
    cmp = ""
    if si.comparison:
        k = si.comparison
        sign = "+" if k["ecart_moyen"] > 0 else ""
        cmp = (f'<br>Webcam − satellite : {sign}{k["ecart_moyen"]:.0f} points en moyenne '
               f'(± {k["ecart_absolu"]:.0f}) sur {k["jours"]} jour{"s" if k["jours"] > 1 else ""} communs')
    peak, src = si.peak_summary()
    return (f'<p class="satline muted"><span class="satkey"></span>Satellite, mélèze à moins de '
            f'{si.radius_km:g} km : <b style="color:{color}">{e(label)}</b>, indice {last.gold_index:.0f} '
            f'le {fr_date(last.day)} · pic {e(peak)}{cmp}</p>')


def _sat_card(c: CamReport, today: date) -> str:
    """Massif sans webcam, suivi par Sentinel-2."""
    e = html.escape
    si = c.sat
    label, color = STAGES[c.stage]
    peak, src = c.peak_summary()
    gold = "—" if c.gold is None else f"{c.gold:.0f}"
    swatch = c.points[-1].mean_rgb if c.points else "#888"
    last = f"dernier passage exploitable : {fr_date(c.points[-1].day)}" if c.points else "aucun passage exploitable"
    ha = f"{si.hectares:.0f} ha de mélèze suivis" if si and si.hectares else ""
    return f"""
<article class="card sat" id="{e(c.id)}">
  <div class="body">
    <header><h2>{e(c.name)}</h2><span class="muted">Sentinel-2 · {ha} · {last}</span></header>
    <div class="now">
      <span class="swatch" style="background:{e(swatch)}"></span>
      <div><div class="stage" style="--c:{color}">{label}</div>
      <div class="muted">indice d'or <b class="big">{gold}</b>/100</div></div>
    </div>
    <div class="peak"><span class="muted">Pic des couleurs</span><b>{peak}</b><small>{e(src)}</small></div>
    {_sparkline(c, today)}
  </div>
</article>"""


CSS = """
:root{--bg:#f6f3ec;--card:#fff;--fg:#1f2421;--muted:#6b706b;--line:#e3ded2;
--g:#3f8f3a;--y:#f2c230;--o:#e8771e;--accent:#c9861a;--sat:#3d7fc4}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#151815;--card:#1f231f;
--fg:#ecebe6;--muted:#9da39b;--line:#333a33}}
:root[data-theme="dark"]{--bg:#151815;--card:#1f231f;--fg:#ecebe6;--muted:#9da39b;--line:#333a33}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1180px;margin:0 auto;padding:24px 16px 60px}
h1{font-size:1.7rem;margin:0 0 4px}h2{font-size:1.05rem;margin:0}
.muted{color:var(--muted)}.sub{margin:0 0 20px}
.top{display:flex;gap:12px;flex-wrap:wrap;margin:0 0 22px}
.pill{background:var(--card);border:1px solid var(--line);border-radius:999px;padding:6px 14px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden}
.shot{margin:0;position:relative;aspect-ratio:16/9;background:#0002}
.shot img{width:100%;height:100%;object-fit:cover;display:block}
.shot .ov{position:absolute;inset:0;opacity:0;transition:opacity .2s}
.shot:hover .ov{opacity:.85}.shot.empty{display:grid;place-items:center;color:var(--muted)}
.body{padding:14px 16px 16px}.body header{display:flex;flex-direction:column;gap:2px;margin-bottom:10px}
.now{display:flex;gap:12px;align-items:center;margin-bottom:10px}
.swatch{width:46px;height:46px;border-radius:10px;border:1px solid var(--line);flex:none}
.stage{font-weight:650;color:var(--c)}.big{font-size:1.25rem;color:var(--fg)}
.bars{display:flex;height:8px;border-radius:4px;overflow:hidden;background:var(--line)}
.bars i{display:block}.legend{display:flex;gap:10px;font-size:.78rem;color:var(--muted);margin:4px 0 10px}
.peak{display:flex;flex-direction:column;border-top:1px solid var(--line);padding-top:10px}
.peak b{font-size:1.1rem;color:var(--accent)}.peak small{color:var(--muted)}
.outlook{display:flex;gap:8px;margin:10px 0 2px}.outlook div{flex:1;text-align:center;
border:1px solid var(--line);border-radius:8px;padding:4px;font-size:.8rem}
.outlook b{display:block;font-weight:500;color:var(--muted)}
.spark{width:100%;height:auto;margin-top:8px}.spark .grid{stroke:var(--line);stroke-dasharray:3 3}
.spark .obs{fill:none;stroke:var(--accent);stroke-width:2}.spark .fit{fill:none;stroke:var(--muted);
stroke-dasharray:4 3;stroke-width:1.2}.spark .today{stroke:var(--muted);stroke-width:.8}
.spark .lbl{font-size:9px;fill:var(--muted)}.spark .dot{stroke:var(--card);stroke-width:.6}
.warn{color:#b3541e;font-size:.85rem;margin:8px 0 0}
.spark .sat{fill:var(--sat);stroke:var(--card);stroke-width:.6}
.satline{font-size:.82rem;margin:8px 0 0;line-height:1.45}
.satkey{display:inline-block;width:8px;height:8px;background:var(--sat);margin-right:6px;vertical-align:0}
.card.sat{border-style:dashed}h2.section{font-size:1.2rem;margin:34px 0 4px}
footer{margin-top:28px;font-size:.85rem;color:var(--muted)}
"""


def _zones_section(zones: list[CamReport], today: date) -> str:
    if not zones:
        return ""
    return (f'<h2 class="section">Massifs suivis par satellite</h2>'
            f'<p class="muted">Sans webcam : indice d\'or des mélézins mesuré sur les images '
            f'Sentinel-2 sans nuages, mêmes stades et même date de pic que pour les webcams.</p>'
            f'<section class="grid">{"".join(_card(z, today) for z in zones)}</section>')


def render(cams: list[CamReport], generated: datetime, out_dir: Path,
           larch_geojson: Path | None = None, mixed_geojson: Path | None = None,
           sat_geojson: Path | None = None) -> Path:
    today = generated.date()
    out_dir.mkdir(parents=True, exist_ok=True)
    zones = [c for c in cams if c.kind == "satellite"]
    cams_all, cams = cams, [c for c in cams if c.kind != "satellite"]
    ranked = sorted((c for c in cams if c.gold is not None and c.stage != "declin"),
                    key=lambda c: -c.gold)
    pills = []
    if ranked:
        b = ranked[0]
        pills.append(f'<span class="pill">Le plus doré : <b>{html.escape(b.name)}</b> '
                     f'({b.gold:.0f}/100)</span>')
    counts: dict[str, int] = {}
    for c in cams:
        counts[c.stage] = counts.get(c.stage, 0) + 1
    for k, (label, color) in STAGES.items():
        if counts.get(k):
            pills.append(f'<span class="pill" style="color:{color}">{label} : {counts[k]}</span>')
    doc = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Météo des mélèzes</title><style>{CSS}</style></head><body><main>
<h1>Météo des mélèzes</h1>
<p class="sub muted">Coloration d'automne estimée à partir des webcams · {generated:%d/%m/%Y %H:%M}
· <a href="carte.html">Carte des mélézins →</a></p>
<div class="top">{''.join(pills)}</div>
<section class="grid">{''.join(_card(c, today) for c in cams)}</section>
{_zones_section(zones, today)}
<footer>Indice d'or = part des aiguilles jaunes/orangées parmi les aiguilles colorées visibles
(0 : tout vert, 100 : tout doré). Survoler une image montre la classification des pixels.
Courbe pleine : mesures journalières ; pointillés : tendance ajustée ; carrés bleus : satellite.
Satellite (Sentinel-2, un passage tous les 2-3 jours) : jaunissement des pixels de mélèze par
rapport à l'été, corrigé par les épicéas et pins voisins, ramené à 0 début septembre ; la chute
des aiguilles se lit dans la baisse de leur NDVI.
Prévisions indicatives : éclairage, neige et nuages influencent les mesures.</footer>
</main></body></html>"""
    path = out_dir / "index.html"
    path.write_text(doc, encoding="utf-8")
    cams_json = [c.to_json() for c in cams_all]
    (out_dir / "latest.json").write_text(json.dumps(
        {"generated": generated.isoformat(timespec="minutes"),
         "cams": cams_json}, ensure_ascii=False, indent=1), encoding="utf-8")
    render_map(cams_json, STAGES, out_dir, larch_geojson, mixed_geojson, sat_geojson)
    return path
