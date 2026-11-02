"""Analyse colorimétrique d'une image de webcam.

Principe (inspiré du réseau PhenoCam) : dans les zones de mélèzes (ROI), on
écarte ciel, neige, rochers gris, ombres profondes, puis on classe les pixels
de végétation selon leur teinte :

    vert      : aiguilles d'été
    jaune     : début / pleine coloration (jaune d'or)
    orange    : coloration avancée (orangé, roux)
    brun      : aiguilles sèches, sols, troncs (teinte chaude mais sombre/terne)

On en tire :
    gold_index  part (jaune + orange) / (vert + jaune + orange), 0–100
    gcc, rcc    coordonnées chromatiques vert/rouge (90e percentile)
    veg_share   part de la ROI occupée par de la végétation colorée ; chute
                quand les aiguilles tombent (le mélèze devient gris)
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
from PIL import Image, ImageDraw

from .config import Polygon

MAX_WIDTH = 1200


@dataclass
class ColorStats:
    valid: bool
    reason: str
    gold_index: float
    green: float
    yellow: float
    orange: float
    brown: float
    veg_share: float
    gcc: float
    rcc: float
    mean_rgb: tuple[int, int, int]
    brightness: float
    roi_pixels: int

    def as_dict(self) -> dict:
        d = asdict(self)
        d["mean_rgb"] = "#%02x%02x%02x" % self.mean_rgb
        return d


def prepare(img: Image.Image) -> Image.Image:
    img = img.convert("RGB")
    if img.width > MAX_WIDTH:
        h = round(img.height * MAX_WIDTH / img.width)
        img = img.resize((MAX_WIDTH, h), Image.LANCZOS)
    return img


def roi_mask(size: tuple[int, int], polys: list[Polygon]) -> np.ndarray:
    w, h = size
    if not polys:
        return np.ones((h, w), dtype=bool)
    m = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(m)
    for poly in polys:
        d.polygon([(x * (w - 1), y * (h - 1)) for x, y in poly], fill=255)
    return np.asarray(m) > 0


def rgb_to_hsv(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """rgb float [0,1] (..., 3) -> h en degrés [0,360), s, v dans [0,1]."""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    mx = rgb.max(axis=-1)
    mn = rgb.min(axis=-1)
    delta = mx - mn
    s = np.where(mx > 0, delta / np.maximum(mx, 1e-9), 0.0)
    h = np.zeros_like(mx)
    nz = delta > 1e-9
    rc = (mx == r) & nz
    gc = (mx == g) & nz & ~rc
    bc = nz & ~rc & ~gc
    h[rc] = ((g[rc] - b[rc]) / delta[rc]) % 6
    h[gc] = (b[gc] - r[gc]) / delta[gc] + 2
    h[bc] = (r[bc] - g[bc]) / delta[bc] + 4
    return h * 60.0, s, mx


# Seuils de teinte (degrés). Le jaune d'or du mélèze tombe vers 40–60°,
# l'orangé/roux vers 20–40°, le vert des aiguilles vers 65–160°.
HUE_ORANGE = (18.0, 40.0)
HUE_YELLOW = (40.0, 64.0)
HUE_GREEN = (64.0, 170.0)
MIN_SAT = 0.18      # en dessous : gris (rocher, aiguilles tombées, brouillard)
MIN_VAL = 0.12      # en dessous : ombre / nuit
BROWN_MAX_VAL = 0.38  # teinte chaude mais sombre -> brun
BROWN_MAX_SAT = 0.35  # ... ou terne


def analyze(img: Image.Image, polys: list[Polygon] | None = None) -> ColorStats:
    img = prepare(img)
    arr = np.asarray(img, dtype=np.float32) / 255.0
    mask = roi_mask(img.size, polys or [])
    px = arr[mask]
    n = int(px.shape[0])
    brightness = float(px.mean()) if n else 0.0

    def invalid(reason: str) -> ColorStats:
        return ColorStats(False, reason, float("nan"), 0, 0, 0, 0, 0, float("nan"),
                          float("nan"), (0, 0, 0), brightness, n)

    if n < 500:
        return invalid("ROI trop petite")
    if brightness < 0.10:
        return invalid("image trop sombre (nuit ?)")

    luma = px @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    if float(luma.std()) < 0.035:
        return invalid("image sans contraste (brouillard, nuage ?)")

    h, s, v = rgb_to_hsv(px)
    colored = (s >= MIN_SAT) & (v >= MIN_VAL)
    warm = (h >= HUE_ORANGE[0]) & (h < HUE_YELLOW[1])
    brown = colored & warm & ((v < BROWN_MAX_VAL) | (s < BROWN_MAX_SAT))
    vivid = colored & ~brown
    green = vivid & (h >= HUE_GREEN[0]) & (h < HUE_GREEN[1])
    yellow = vivid & (h >= HUE_YELLOW[0]) & (h < HUE_YELLOW[1])
    orange = vivid & (h >= HUE_ORANGE[0]) & (h < HUE_ORANGE[1])

    ng, ny, no, nb = (int(x.sum()) for x in (green, yellow, orange, brown))
    veg = ng + ny + no
    veg_share = veg / n
    if veg < max(200, 0.03 * n):
        r = invalid("trop peu de végétation visible (neige, brouillard ou ROI mal placée ?)")
        r.veg_share = veg_share
        return r

    vegmask = green | yellow | orange
    vp = px[vegmask]
    tot = np.maximum(vp.sum(axis=1), 1e-6)
    gcc = float(np.percentile(vp[:, 1] / tot, 90))
    rcc = float(np.percentile(vp[:, 0] / tot, 90))
    mean_rgb = tuple(int(round(c * 255)) for c in vp.mean(axis=0))

    return ColorStats(
        valid=True,
        reason="",
        gold_index=round(100.0 * (ny + no) / veg, 1),
        green=round(ng / veg, 3),
        yellow=round(ny / veg, 3),
        orange=round(no / veg, 3),
        brown=round(nb / n, 3),
        veg_share=round(veg_share, 3),
        gcc=round(gcc, 4),
        rcc=round(rcc, 4),
        mean_rgb=mean_rgb,  # type: ignore[arg-type]
        brightness=round(brightness, 3),
        roi_pixels=n,
    )


def classification_overlay(img: Image.Image, polys: list[Polygon] | None = None) -> Image.Image:
    """Image de contrôle : pixels classés vert/jaune/orange, le reste grisé."""
    img = prepare(img)
    arr = np.asarray(img, dtype=np.float32) / 255.0
    mask = roi_mask(img.size, polys or [])
    h, s, v = rgb_to_hsv(arr)
    colored = (s >= MIN_SAT) & (v >= MIN_VAL) & mask
    warm = (h >= HUE_ORANGE[0]) & (h < HUE_YELLOW[1])
    brown = colored & warm & ((v < BROWN_MAX_VAL) | (s < BROWN_MAX_SAT))
    vivid = colored & ~brown
    out = (arr.mean(axis=-1, keepdims=True).repeat(3, axis=-1) * 0.5)
    out[vivid & (h >= HUE_GREEN[0]) & (h < HUE_GREEN[1])] = (0.1, 0.7, 0.2)
    out[vivid & (h >= HUE_YELLOW[0]) & (h < HUE_YELLOW[1])] = (1.0, 0.85, 0.0)
    out[vivid & (h >= HUE_ORANGE[0]) & (h < HUE_ORANGE[1])] = (1.0, 0.45, 0.0)
    out[brown] = (0.45, 0.3, 0.2)
    return Image.fromarray((out * 255).astype(np.uint8))
