"""Récupération des images de webcams."""
from __future__ import annotations

import io
import os
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import requests
from PIL import Image

from .config import Cam

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 larch-meteo/0.1"
)
TIMEOUT = 30
MIN_PIXELS = 320 * 200  # en dessous : vignette / logo, pas une image de webcam


class FetchError(RuntimeError):
    pass


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def _decode(data: bytes, what: str) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:  # noqa: BLE001
        raise FetchError(f"{what} : contenu non lisible comme image ({e})") from e
    return img.convert("RGB")


def fetch_image_url(url: str, referer: str | None = None) -> Image.Image:
    headers = {"Referer": referer} if referer else {}
    r = _session().get(url, timeout=TIMEOUT, headers=headers)
    r.raise_for_status()
    return _decode(r.content, url)


class _ImgCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.og: list[str] = []
        self.imgs: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta" and a.get("property") in ("og:image", "og:image:url"):
            if a.get("content"):
                self.og.append(a["content"])
        elif tag == "img":
            for key in ("src", "data-src", "data-original"):
                if a.get(key):
                    self.imgs.append(a[key])
                    break


_IMG_IN_TEXT = re.compile(r"https?://[^\s\"'<>]+?\.(?:jpe?g|png|webp)(?:\?[^\s\"'<>]*)?", re.I)


def candidate_images_from_html(html: str, base_url: str) -> list[str]:
    """URL d'images probables, par ordre de préférence."""
    p = _ImgCollector()
    p.feed(html)
    out: list[str] = []
    for u in p.og + p.imgs + _IMG_IN_TEXT.findall(html):
        full = urljoin(base_url, u)
        if full not in out and not re.search(r"logo|icon|sprite|avatar|\.svg", full, re.I):
            out.append(full)
    return out


def fetch_from_page(url: str, max_candidates: int = 15) -> Image.Image:
    r = _session().get(url, timeout=TIMEOUT)
    r.raise_for_status()
    best: Image.Image | None = None
    for cand in candidate_images_from_html(r.text, url)[:max_candidates]:
        try:
            img = fetch_image_url(cand, referer=url)
        except Exception:  # noqa: BLE001
            continue
        if img.width * img.height >= MIN_PIXELS and (
            best is None or img.width * img.height > best.width * best.height
        ):
            best = img
    if best is None:
        raise FetchError(f"aucune image de webcam trouvée dans {url} (essayer type: browser)")
    return best


_LARGEST_MEDIA_JS = """
(sel) => {
  const els = sel ? Array.from(document.querySelectorAll(sel))
                  : Array.from(document.querySelectorAll('img, canvas, video'));
  let best = null, bestArea = 0;
  for (const el of els) {
    const r = el.getBoundingClientRect();
    const area = r.width * r.height;
    if (area > bestArea && r.width > 50 && r.height > 50) { best = el; bestArea = area; }
  }
  if (!best) return null;
  best.setAttribute('data-larch-target', '1');
  return {tag: best.tagName.toLowerCase(),
          src: best.currentSrc || best.src || null,
          nw: best.naturalWidth || 0, nh: best.naturalHeight || 0};
}
"""


def fetch_with_browser(url: str, selector: str | None = None, wait_ms: int = 6000) -> Image.Image:
    """Rend la page dans Chromium et récupère la plus grande image visible."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise FetchError(
            "type: browser nécessite Playwright : pip install 'larch-meteo[browser]' "
            "&& playwright install chromium"
        ) from e

    with sync_playwright() as pw:
        exe = os.environ.get("LARCH_CHROMIUM")
        browser = pw.chromium.launch(executable_path=exe) if exe else pw.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1600, "height": 1000}, user_agent=USER_AGENT)
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:  # noqa: BLE001 - certains sites ne sont jamais « idle »
                pass
            page.wait_for_timeout(wait_ms)
            info = page.evaluate(_LARGEST_MEDIA_JS, selector)
            if not info:
                raise FetchError(f"aucun élément image/canvas trouvé sur {url}")
            # Image avec une vraie URL et pleine résolution : on la télécharge
            # (meilleure qualité qu'une capture d'écran).
            if info["tag"] == "img" and info["src"] and info["src"].startswith("http") \
                    and info["nw"] * info["nh"] >= MIN_PIXELS:
                try:
                    resp = page.request.get(info["src"], headers={"Referer": url})
                    if resp.ok:
                        return _decode(resp.body(), info["src"])
                except Exception:  # noqa: BLE001 - ex. proxy non pris en compte : on capture
                    pass
            png = page.locator("[data-larch-target='1']").first.screenshot()
            return _decode(png, f"capture de {url}")
        finally:
            browser.close()


# --- Skaping ---------------------------------------------------------------
# Les pages Skaping (www.skaping.com/<lieu>/<vue>) embarquent, dans le HTML
# brut, un JSON listant les vues (« povs ») avec leur dernière image :
#   "player_url":"https://www.skaping.com/serre-chevalier/mea", ...
#   "last_media":"https://skaping.s3.gra.io.cloud.ovh.net/serre-chevalier/mea/2026/11/02/mini/15-50.jpg"
# Le segment de qualité (`mini`) se remplace par `small` (2730 px), `large`
# (4096 px) ou se retire pour la pleine résolution (12288 px, ~3,5 Mo).
# Si le stockage OVH ne répond pas, le lecteur Skaping bascule sur le domaine
# de secours rvfbdt.skaping.com (mêmes chemins) : on fait pareil.

SKAPING_QUALITIES = ("mini", "small", "large", "full")
SKAPING_FALLBACK_HOST = "rvfbdt.skaping.com"
_JSON_OBJ = re.compile(r"\{[^{}]*\}")


def _norm_url(u: str) -> str:
    return re.sub(r"^(https?:)?//(www\.)?", "", u.replace("\\/", "/")).rstrip("/").lower()


def skaping_last_media(html: str, page_url: str) -> str:
    """URL de la dernière image de la vue `page_url` dans le HTML d'une page Skaping."""
    target = _norm_url(page_url)
    found: list[tuple[str, str]] = []
    for m in _JSON_OBJ.finditer(html):
        obj = m.group(0)
        pu = re.search(r'"player_url"\s*:\s*"([^"]+)"', obj)
        lm = re.search(r'"last_media"\s*:\s*"([^"]+)"', obj)
        if pu and lm:
            found.append((_norm_url(pu.group(1)), lm.group(1).replace("\\/", "/")))
    for pu, lm in found:
        if pu == target:
            return lm
    raise FetchError(
        f"Skaping : vue {page_url} introuvable dans la page "
        f"({len(found)} vues listées : {', '.join(sorted({p for p, _ in found})[:5])}...)"
    )


def skaping_media_url(last_media: str, quality: str = "large") -> str:
    if quality not in SKAPING_QUALITIES:
        raise FetchError(f"Skaping : qualité inconnue {quality!r} ({', '.join(SKAPING_QUALITIES)})")
    # Seules les vues « photo » ont un segment de qualité ; les vues vidéo
    # (.../video/AAAA/MM/JJ/HH-MM.jpg) n'existent qu'en une taille.
    m = re.match(r"(.*/\d{4}/\d{2}/\d{2}/)(?:mini|small|large)/([^/]+)$", last_media)
    if not m:
        return last_media
    head, name = m.groups()
    return head + ("" if quality == "full" else f"{quality}/") + name


def fetch_skaping(url: str, quality: str = "large", retry_wait: float = 30.0) -> Image.Image:
    r = _session().get(url, timeout=TIMEOUT)
    r.raise_for_status()
    media = skaping_media_url(skaping_last_media(r.text, url), quality)
    if media.startswith("//"):
        media = "https:" + media
    errors = []
    fallback = re.sub(r"^(https?://)[^/]+", r"\g<1>" + SKAPING_FALLBACK_HOST, media)
    # Une image tout juste publiée peut ne pas être encore servie (403) :
    # on réessaie une fois après une courte pause.
    for attempt in range(2):
        if attempt:
            time.sleep(retry_wait)
        for cand in (media, fallback):
            try:
                return fetch_image_url(cand, referer=url)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{cand} : {e}")
    raise FetchError("Skaping : image injoignable\n  " + "\n  ".join(errors))


# --- webcam-hd (Skol) --------------------------------------------------------
# app.webcam-hd.com/<groupe>/<vue> : l'API publique de la page donne l'URL du
# dernier panorama (tailles small / 1080 / mega) hébergé sur
# skol-webcams-archives.s3.fr-par.scw.cloud.


def fetch_webcamhd(cam_id: str, quality: str = "1080") -> Image.Image:
    api = f"https://app.webcam-hd.com/api/webcam/{cam_id}/{cam_id}/live-panorama"
    r = _session().get(api, timeout=TIMEOUT)
    r.raise_for_status()
    live = (r.json() or {}).get("live") or {}
    src = live.get(quality) or live.get("1080") or live.get("small")
    if not src:
        raise FetchError(f"webcam-hd {cam_id} : pas d'image dans {api}")
    return fetch_image_url(src, referer="https://app.webcam-hd.com/")


def fetch_windy(webcam_id: str | int) -> Image.Image:
    key = os.environ.get("WINDY_API_KEY")
    if not key:
        raise FetchError("WINDY_API_KEY non définie (clé gratuite sur https://api.windy.com/keys)")
    r = _session().get(
        f"https://api.windy.com/webcams/api/v3/webcams/{webcam_id}",
        params={"include": "images"},
        headers={"x-windy-api-key": key},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    images = r.json().get("images") or {}
    current = images.get("current") or {}
    for size in ("preview", "thumbnail", "icon"):
        if current.get(size):
            return fetch_image_url(current[size])
    raise FetchError(f"Windy {webcam_id} : pas d'image courante dans la réponse")


def fetch_cam(cam: Cam, base_dir: Path | None = None) -> Image.Image:
    src = cam.source
    kind = src.get("type", "image")
    if kind == "image":
        return fetch_image_url(src["url"], referer=src.get("referer"))
    if kind == "page":
        return fetch_from_page(src["url"])
    if kind == "browser":
        return fetch_with_browser(src["url"], src.get("selector"), int(src.get("wait_ms", 6000)))
    if kind == "skaping":
        return fetch_skaping(src["url"], src.get("quality", "large"))
    if kind == "webcamhd":
        return fetch_webcamhd(src["cam"], str(src.get("quality", "1080")))
    if kind == "windy":
        return fetch_windy(src["webcam_id"])
    if kind == "file":
        p = Path(src["path"])
        if base_dir and not p.is_absolute():
            p = base_dir / p
        return Image.open(p).convert("RGB")
    raise FetchError(f"type de source inconnu : {kind}")
