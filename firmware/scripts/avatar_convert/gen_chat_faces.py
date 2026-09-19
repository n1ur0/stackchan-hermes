#!/usr/bin/env python3
"""Generate the "Chat Companion" StackChan face set (programmatic render).

A new, more expressive face set for talking with the robot:
  - soft rounded "screen" panel behind a friendly face
  - LED-style rounded eyes with a white glint (readable at 160x120)
  - eyebrows that carry emotion (happy / sad / surprised / thinking)
  - blush cheeks on happy / embarrassed faces
  - mouths shaped for chat animation (closed / half / open / e / u)

Contract (matches firmware/scripts/avatar_convert/convert_avatars.py):
  14 PNGs at 320x240, RGB, opaque white background:
    faces: idle happy thinking sad surprised embarrassed
    eyes:  eyes_open eyes_half eyes_closed
    mouth: mouth_closed mouth_half mouth_open mouth_e mouth_u
  Each eyes_/mouth_ image is the FULL neutral face with ONLY that part
  changed (the firmware swaps full frames, no alpha blending).

Swap any file for custom art later — the firmware only reads these
filenames.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

W, H = 320, 240

# Face geometry
PANEL = (54, 18, 266, 222)          # rounded panel behind the face
PANEL_R = 56
PANEL_FILL = "#1E2A3D"              # dark navy card
PANEL_LINE = "#5E84B8"              # muted steel-blue outline

EYE_L = (118, 98)                   # left eye center
EYE_R = (202, 98)                   # right eye center
EYE_W, EYE_H = 72, 84               # open eye size

BROW_L = (118, 46)                  # eyebrow anchors
BROW_R = (202, 46)

MOUTH_Y = 168                       # mouth baseline
MOUTH_SPAN = (128, 192)             # mouth horizontal span

BLUSH_L = (76, 132)
BLUSH_R = (244, 132)
BLUSH_RR = 24

INK = "#EDF3FB"                     # light features (readable on dark)
GLINT = "#16202E"                   # dark glint inside light eyes


def new_canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    im = Image.new("RGB", (W, H), "#0E1524")   # deep blue-black backdrop
    d = ImageDraw.Draw(im)
    d.rounded_rectangle(PANEL, radius=PANEL_R, fill=PANEL_FILL, outline=PANEL_LINE, width=6)
    return im, d


# ---- Eyes ----------------------------------------------------------------

def eye_open(d: ImageDraw.ImageDraw, cx: int, cy: int, w: int = EYE_W, h: int = EYE_H) -> None:
    """Rounded-rect LED eye with a white glint."""
    x0, y0 = cx - w // 2, cy - h // 2
    d.rounded_rectangle((x0, y0, x0 + w, y0 + h), radius=w // 3, fill=INK)
    gx0, gy0 = cx - w // 4, cy - h // 4
    d.ellipse((gx0, gy0, gx0 + 16, gy0 + 16), fill=GLINT)


def eye_half(d: ImageDraw.ImageDraw, cx: int, cy: int, w: int = EYE_W, h: int = EYE_H) -> None:
    """Half-closed lid (blink mid-way): bottom half covered by the face."""
    x0, y0 = cx - w // 2, cy - h // 2
    d.rounded_rectangle((x0, y0, x0 + w, y0 + h), radius=w // 3, fill=INK)
    d.rounded_rectangle((x0, y0 + h // 2, x0 + w, y0 + h), radius=w // 3, fill=PANEL_FILL)


def eye_closed(d: ImageDraw.ImageDraw, cx: int, cy: int) -> None:
    """Closed lid: gentle downward arc."""
    r = EYE_W // 2 - 6
    d.arc((cx - r, cy - r, cx + r, cy + r), 200, 340, fill=INK, width=10)


def eye_look_up(d: ImageDraw.ImageDraw, cx: int, cy: int) -> None:
    """Thinking gaze: smaller eye raised toward the top right."""
    for dx, dy in ((cx + 6, cy - 16), (cx + 6, cy - 16)):
        eye_open(d, dx, dy, w=64, h=56)


def brow(d: ImageDraw.ImageDraw, x, y, dx, dy, w=44, width=10) -> None:
    """A single eyebrow from (x,y) sloping by (dx,dy)."""
    d.line((x - w // 2, y, x + w // 2, y + dy), fill=INK, width=width)


def brows_neutral(d: ImageDraw.ImageDraw) -> None:
    brow(d, BROW_L[0], BROW_L[1], 0, 0)
    brow(d, BROW_R[0], BROW_R[1], 0, 0)


def brows_happy(d: ImageDraw.ImageDraw) -> None:
    for x, y in (BROW_L, BROW_R):
        d.arc((x - 30, y - 22, x + 30, y + 18), 180, 360, fill=INK, width=10)


def brows_sad(d: ImageDraw.ImageDraw) -> None:
    brow(d, BROW_L[0], BROW_L[1] + 4, 0, -12)      # left brow: inner end up
    brow(d, BROW_R[0], BROW_R[1] + 4, 0, 12)       # right brow: inner end up


def brows_surprised(d: ImageDraw.ImageDraw) -> None:
    for x, y in (BROW_L, BROW_R):
        brow(d, x, y - 16, 0, 6, w=36, width=10)


def brows_thinking(d: ImageDraw.ImageDraw) -> None:
    brow(d, BROW_L[0], BROW_L[1] - 6, 0, 0)
    brow(d, BROW_R[0], BROW_R[1] - 18, 0, 10, w=36)   # one brow raised


# ---- Mouths --------------------------------------------------------------

def mouth_neutral(d: ImageDraw.ImageDraw, width=10) -> None:
    x0, x1 = MOUTH_SPAN
    d.line((x0, MOUTH_Y, x1, MOUTH_Y), fill=INK, width=width)


def mouth_smile(d: ImageDraw.ImageDraw, width=10) -> None:
    x0, x1 = MOUTH_SPAN
    d.arc((x0, MOUTH_Y - 20, x1, MOUTH_Y + 26), 0, 180, fill=INK, width=width)


def mouth_frown(d: ImageDraw.ImageDraw, width=10) -> None:
    x0, x1 = MOUTH_SPAN
    d.arc((x0, MOUTH_Y - 26, x1, MOUTH_Y + 20), 180, 360, fill=INK, width=width)


def mouth_half(d: ImageDraw.ImageDraw) -> None:
    d.ellipse((150, MOUTH_Y - 14, 170, MOUTH_Y + 6), fill=INK)


def mouth_open(d: ImageDraw.ImageDraw, big: bool = True) -> None:
    if big:
        d.ellipse((138, MOUTH_Y - 22, 182, MOUTH_Y + 22), fill=INK)
        d.ellipse((150, MOUTH_Y + 2, 170, MOUTH_Y + 14), fill="#E0709A")  # tongue
    else:
        d.ellipse((144, MOUTH_Y - 18, 176, MOUTH_Y + 14), fill=INK)


def mouth_e(d: ImageDraw.ImageDraw) -> None:
    x0, x1 = MOUTH_SPAN
    d.rectangle((x0, MOUTH_Y - 8, x1, MOUTH_Y + 8), fill=INK)


def mouth_u(d: ImageDraw.ImageDraw) -> None:
    d.ellipse((152, MOUTH_Y - 12, 168, MOUTH_Y + 14), fill=INK)


def mouth_wavy(d: ImageDraw.ImageDraw, width=10) -> None:
    x0, x1 = MOUTH_SPAN
    pts = []
    for i in range(9):
        x = x0 + (x1 - x0) * i / 8
        y = MOUTH_Y + (8 if i % 2 else -8)
        pts.append((x, y))
    d.line(pts, fill=INK, width=width, joint="curve")


# ---- Cheeks --------------------------------------------------------------

def blush(d: ImageDraw.ImageDraw, alpha: str = "#E8A0BA", rr: int = BLUSH_RR) -> None:
    for cx, cy in (BLUSH_L, BLUSH_R):
        d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=alpha)


# ---- Faces ---------------------------------------------------------------

def face_idle() -> Image.Image:
    im, d = new_canvas()
    brows_neutral(d)
    eye_open(d, *EYE_L)
    eye_open(d, *EYE_R)
    mouth_neutral(d)
    return im


def face_happy() -> Image.Image:
    im, d = new_canvas()
    brows_happy(d)
    eye_closed(d, *EYE_L)
    eye_closed(d, *EYE_R)
    mouth_open(d, big=True)
    blush(d)
    return im


def face_thinking() -> Image.Image:
    im, d = new_canvas()
    brows_thinking(d)
    eye_look_up(d, *EYE_L)
    eye_look_up(d, *EYE_R)
    mouth_u(d)
    return im


def face_sad() -> Image.Image:
    im, d = new_canvas()
    brows_sad(d)
    eye_half(d, *EYE_L)
    eye_half(d, *EYE_R)
    mouth_frown(d)
    return im


def face_surprised() -> Image.Image:
    im, d = new_canvas()
    brows_surprised(d)
    for cx, cy in (EYE_L, EYE_R):
        r = 34
        d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=INK, width=10)
    mouth_open(d, big=True)
    return im


def face_embarrassed() -> Image.Image:
    im, d = new_canvas()
    brows_neutral(d)
    eye_half(d, *EYE_L)
    eye_half(d, *EYE_R)
    mouth_wavy(d)
    blush(d, alpha="#E57B9E", rr=30)
    return im


# ---- Part variants (full neutral face, only the named part changed) -------

def part_eyes(name: str) -> Image.Image:
    im, d = new_canvas()
    brows_neutral(d)
    if name == "eyes_open":
        eye_open(d, *EYE_L)
        eye_open(d, *EYE_R)
    elif name == "eyes_half":
        eye_half(d, *EYE_L)
        eye_half(d, *EYE_R)
    else:  # eyes_closed
        eye_closed(d, *EYE_L)
        eye_closed(d, *EYE_R)
    mouth_neutral(d)
    return im


def part_mouth(name: str) -> Image.Image:
    im, d = new_canvas()
    brows_neutral(d)
    eye_open(d, *EYE_L)
    eye_open(d, *EYE_R)
    if name == "mouth_closed":
        mouth_neutral(d)
    elif name == "mouth_half":
        mouth_half(d)
    elif name == "mouth_open":
        mouth_open(d, big=True)
    elif name == "mouth_e":
        mouth_e(d)
    else:  # mouth_u
        mouth_u(d)
    return im


FACES = {
    "idle": face_idle,
    "happy": face_happy,
    "thinking": face_thinking,
    "sad": face_sad,
    "surprised": face_surprised,
    "embarrassed": face_embarrassed,
}
PARTS = {f"eyes_{n}": (lambda n=n: part_eyes(f"eyes_{n}")) for n in ("open", "half", "closed")} | {
    f"mouth_{n}": (lambda n=n: part_mouth(f"mouth_{n}")) for n in ("closed", "half", "open", "e", "u")
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path.home() / ".stackchan" / "avatar")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    for name, fn in {**FACES, **PARTS}.items():
        im = fn()
        p = args.out / f"{name}.png"
        im.save(p)
        print(f"  {name:14s} -> {p}")
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
