"""Ligne de commande : larch fetch | report | run | analyze | roi | calibrate."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw

from . import analysis, phenology, store, weather
from .config import Cam, Config, load_config
from .fetch import fetch_cam
from .report import CamReport, render

ARCHIVE_WIDTH = 800


def _now(cfg: Config) -> datetime:
    return datetime.now(ZoneInfo(cfg.settings.timezone)).replace(tzinfo=None, second=0, microsecond=0)


def _resize(img: Image.Image, width: int) -> Image.Image:
    if img.width <= width:
        return img
    return img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)


def _status_path(data: Path) -> Path:
    return data / "status.json"


def cmd_fetch(cfg: Config, args) -> int:
    data = Path(args.data)
    now = _now(cfg)
    status = json.loads(_status_path(data).read_text()) if _status_path(data).exists() else {}
    failures = 0
    for cam in _select(cfg, args.cam):
        try:
            img = fetch_cam(cam, base_dir=Path(args.config).parent)
        except Exception as e:  # noqa: BLE001
            failures += 1
            msg = f"{type(e).__name__}: {e}"[:300]
            status[cam.id] = {"time": now.isoformat(timespec="minutes"), "error": msg}
            print(f"[{cam.id}] ÉCHEC : {msg}", file=sys.stderr)
            continue
        stats = analysis.analyze(img, cam.roi)
        store.append(data / "history.csv", cam.id, now, stats)
        latest = data / "latest"
        latest.mkdir(parents=True, exist_ok=True)
        _resize(img, ARCHIVE_WIDTH * 2).save(latest / f"{cam.id}.jpg", quality=85)
        analysis.classification_overlay(img, cam.roi).save(latest / f"{cam.id}_classes.jpg", quality=80)
        h0, h1 = cfg.settings.daylight_hours
        if args.archive and h0 <= now.hour < h1:
            # Une image par jour suffit (rejouer l'analyse, recaler ROI et modèle).
            arch = data / "archive" / cam.id
            arch.mkdir(parents=True, exist_ok=True)
            if not any(arch.glob(f"{now:%Y-%m-%d}_*.jpg")):
                _resize(img, ARCHIVE_WIDTH).save(arch / f"{now:%Y-%m-%d_%H%M}.jpg", quality=80)
        status[cam.id] = {"time": now.isoformat(timespec="minutes"), "error": None,
                          "measure": stats.as_dict()}
        tag = f"indice d'or {stats.gold_index:.0f}" if stats.valid else f"invalide ({stats.reason})"
        print(f"[{cam.id}] {tag}")
    data.mkdir(parents=True, exist_ok=True)
    _status_path(data).write_text(json.dumps(status, ensure_ascii=False, indent=1))
    return 1 if failures == len(cfg.cams) and failures else 0


def _calibration(data: Path) -> dict:
    p = data / "calibration.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _threshold(cfg: Config, cam: Cam, calib: dict) -> float:
    if cam.peak_threshold:
        return float(cam.peak_threshold)
    c = calib.get(cam.id)
    if c and c.get("seasons"):
        vals = list(c["seasons"].values())
        return sum(vals) / len(vals)
    return cfg.settings.forecast.peak_threshold


def _season_points(points: list[store.DailyPoint], start: date) -> list[store.DailyPoint]:
    return [p for p in points if p.day >= start]


def build_reports(cfg: Config, args) -> list[CamReport]:
    data = Path(args.data)
    site = Path(args.site)
    now = _now(cfg)
    today = now.date()
    rows = store.load(data / "history.csv")
    status = json.loads(_status_path(data).read_text()) if _status_path(data).exists() else {}
    calib = _calibration(data)
    fc = cfg.settings.forecast
    start = phenology.season_start(today, fc.start_month_day)
    (site / "img").mkdir(parents=True, exist_ok=True)

    reports = []
    for cam in cfg.cams:
        hours = cfg.settings.daylight_hours
        pts = _season_points(store.daily(rows, cam.id, hours), start)
        bare = [d for d in store.bare_days(rows, cam.id, hours) if d >= start]
        st = status.get(cam.id, {})
        rep = CamReport(
            id=cam.id, name=cam.name, elevation=cam.elevation, stage=phenology.stage(pts, bare),
            points=pts, last=st.get("measure"), error=st.get("error"),
            last_time=datetime.fromisoformat(st["time"]) if st.get("time") else None,
            fit=phenology.fit_logistic(pts), observed_peak=phenology.observed_peak(pts, bare),
        )
        for name, attr in ((f"{cam.id}.jpg", "image"), (f"{cam.id}_classes.jpg", "overlay")):
            src = data / "latest" / name
            if src.exists():
                (site / "img" / name).write_bytes(src.read_bytes())
                setattr(rep, attr, f"img/{name}")
        if not args.no_weather:
            try:
                temps = weather.daily_mean_temps(cam.lat, cam.lon, cam.elevation, start, today,
                                                 cache=data / "weather" / f"{cam.id}.json")
                horizon = max(temps)
                temps = weather.extend(temps, date(start.year, 12, 31))
                rep.cold = phenology.cold_model(temps, start, today, fc.base_temp,
                                                _threshold(cfg, cam, calib), forecast_end=horizon)
            except Exception as e:  # noqa: BLE001
                rep.notes.append(f"météo indisponible : {e}")
                print(f"[{cam.id}] météo indisponible : {e}", file=sys.stderr)
        reports.append(rep)
    return reports


def cmd_report(cfg: Config, args) -> int:
    reports = build_reports(cfg, args)
    path = render(reports, _now(cfg), Path(args.site))
    for r in reports:
        peak, src = r.peak_summary()
        print(f"[{r.id}] {phenology.STAGES[r.stage][0]} · pic : {peak} ({src})")
    print(f"→ {path}")
    return 0


def cmd_run(cfg: Config, args) -> int:
    cmd_fetch(cfg, args)
    return cmd_report(cfg, args)


def cmd_analyze(cfg: Config, args) -> int:
    roi = cfg.cam(args.cam).roi if args.cam else []
    img = Image.open(args.image)
    stats = analysis.analyze(img, roi)
    print(json.dumps(stats.as_dict(), ensure_ascii=False, indent=1))
    if args.overlay:
        analysis.classification_overlay(img, roi).save(args.overlay)
        print(f"→ {args.overlay}")
    return 0


def cmd_roi(cfg: Config, args) -> int:
    """Image quadrillée (coordonnées relatives) pour tracer les ROI."""
    cam = cfg.cam(args.cam)
    src = Path(args.image) if args.image else Path(args.data) / "latest" / f"{cam.id}.jpg"
    img = analysis.prepare(Image.open(src)).convert("RGBA")
    over = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    w, h = img.size
    for i in range(1, 20):
        major = i % 2 == 0
        col = (255, 255, 255, 170 if major else 70)
        x, y = i * w / 20, i * h / 20
        d.line([(x, 0), (x, h)], fill=col, width=1)
        d.line([(0, y), (w, y)], fill=col, width=1)
        if major:
            d.text((x + 2, 2), f"{i/20:.1f}", fill=(255, 255, 0, 255))
            d.text((2, y + 2), f"{i/20:.1f}", fill=(255, 255, 0, 255))
    for poly in cam.roi:
        d.polygon([(x * (w - 1), y * (h - 1)) for x, y in poly], outline=(255, 0, 255, 255),
                  fill=(255, 0, 255, 50))
    out = Path(args.out or f"roi_{cam.id}.png")
    Image.alpha_composite(img, over).convert("RGB").save(out)
    print(f"→ {out}  (coordonnées x,y de 0 à 1 ; à reporter dans cams.yaml, clé roi)")
    return 0


def cmd_calibrate(cfg: Config, args) -> int:
    """Ajuste le seuil de froid de chaque webcam sur les pics observés."""
    data = Path(args.data)
    rows = store.load(data / "history.csv")
    calib = _calibration(data)
    fc = cfg.settings.forecast
    today = _now(cfg).date()
    for cam in _select(cfg, args.cam):
        allpts = store.daily(rows, cam.id, cfg.settings.daylight_hours)
        allbare = store.bare_days(rows, cam.id, cfg.settings.daylight_hours)
        for year in sorted({p.day.year for p in allpts}):
            start = phenology.season_start(date(year, 12, 31), fc.start_month_day)
            pts = [p for p in allpts if start <= p.day <= date(year, 12, 31)]
            peak = phenology.observed_peak(pts, [d for d in allbare if d.year == year])
            if not peak:
                print(f"[{cam.id}] {year} : pas de pic confirmé")
                continue
            temps = weather.daily_mean_temps(cam.lat, cam.lon, cam.elevation, start,
                                             min(today, peak + timedelta(days=1)),
                                             cache=data / "weather" / f"{cam.id}.json")
            thr = phenology.cold_sum(temps, start, peak, fc.base_temp)
            calib.setdefault(cam.id, {"seasons": {}})["seasons"][str(year)] = round(thr, 1)
            print(f"[{cam.id}] {year} : pic le {peak}, seuil = {thr:.0f} degrés-jours")
    data.mkdir(parents=True, exist_ok=True)
    (data / "calibration.json").write_text(json.dumps(calib, indent=1))
    return 0


def _select(cfg: Config, cam_id: str | None) -> list[Cam]:
    return [cfg.cam(cam_id)] if cam_id else cfg.cams


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="larch", description="Météo des couleurs des mélèzes")
    p.add_argument("--config", default="cams.yaml")
    p.add_argument("--data", default="data")
    p.add_argument("--site", default="site")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="récupère et analyse les images")
    f.add_argument("--cam")
    f.add_argument("--no-archive", dest="archive", action="store_false")
    r = sub.add_parser("report", help="génère la page HTML")
    r.add_argument("--no-weather", action="store_true")
    run = sub.add_parser("run", help="fetch + report")
    run.add_argument("--cam")
    run.add_argument("--no-archive", dest="archive", action="store_false")
    run.add_argument("--no-weather", action="store_true")
    a = sub.add_parser("analyze", help="analyse une image locale")
    a.add_argument("image")
    a.add_argument("--cam", help="utiliser la ROI de cette webcam")
    a.add_argument("--overlay", help="écrire l'image de classification")
    o = sub.add_parser("roi", help="image quadrillée pour tracer les zones de mélèzes")
    o.add_argument("cam")
    o.add_argument("--image")
    o.add_argument("--out")
    c = sub.add_parser("calibrate", help="ajuste le modèle sur les pics observés")
    c.add_argument("--cam")

    args = p.parse_args(argv)
    cfg = load_config(args.config)
    return {"fetch": cmd_fetch, "report": cmd_report, "run": cmd_run, "analyze": cmd_analyze,
            "roi": cmd_roi, "calibrate": cmd_calibrate}[args.cmd](cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
