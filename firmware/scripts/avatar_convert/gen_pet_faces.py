#!/usr/bin/env python3
"""Generate the "Pet Kitten" StackChan face set (programmatic render).

A tamagotchi-style virtual-pet cat for the robot's LCD, built for the
pixel-art arcade pipeline:
  - big tan cat head with pointy ears + pink inner ears
  - LED-style rounded eyes with a dark glint (readable at 160x120)
  - cat "omega" mouth, pink triangle nose, 3 whiskers per cheek
  - brows carry emotion (happy / sad / surprised / thinking)
  - blush cheeks on happy / embarrassed faces
  - mouths shaped for chat animation (closed / half / open / e / u)

Contract (matches firmware/scripts/avatar_convert/convert_avatars.py):
  14 PNGs at 320x240, RGB, opaque background:
    faces: idle happy thinking sad surprised embarrassed
    eyes:  eyes_open eyes_half eyes_closed
    mouth: mouth_closed mouth_half mouth_open mouth_e mouth_u
  Each eyes_/mouth_ image is the FULL neutral face with ONLY that part
  changed (the firmware swaps full frames, no alpha blending).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

W, H = 320, 240

# ---- Window + palette ------------------------------------------------------
WINDOW = (52, 16, 268, 224)          # "tamagotchi screen" window
WINDOW_R = 56
WINDOW_FILL = "#101A2B"
WINDOW_LINE = "#274059"

HEAD_C = (160, 136)                  # head ellipse center
HEAD_RX, HEAD_RY = 104, 88
HEAD_FILL = "#E0BC8D"                # warm tan
HEAD_LINE = "#8A5F38"                # darker tan outline

INK = "#16202E"                      # near-black feature ink (on tan)
EYE_FILL = "#EDF3FB"                 # light eye
GLINT = "#16202E"
PINK = "#E0709A"                     # nose / tongue / inner ear
BLUSH = "#E57B9E"
WHISK = "#9FB3C8"

EYE_L = (124, 116)                   # eye centers
EYE_R = (196, 116)
EYE_W, EYE_H = 64, 72
BROW_L = (124, 66)                   # brow anchors
BROW_R = (196, 66)

NOSE_PTS = ((152, 146), (168, 146), (160, 160))
MOUTH_Y = 160                        # omega mouth baseline
MOUTH_SPAN = (128, 192)

BLUSH_L = (72, 168)
BLUSH_R = (248, 168)
BLUSH_RR = 18


def new_canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    im = Image.new("RGB", (W, H), "#0E1524")          # deep blue-black backdrop
    d = ImageDraw.Draw(im)
    d.rounded_rectangle(WINDOW, radius=WINDOW_R, fill=WINDOW_FILL,
                        outline=WINDOW_LINE, width=5)
    return im, d


# ---- Head / features -------------------------------------------------------

def draw_head(d: ImageDraw.ImageDraw) -> None:
    """Cat head: outline + fill, then ears on top (draw ears after head)."""
    cx, cy = HEAD_C
    bbox = (cx - HEAD_RX, cy - HEAD_RY, cx + HEAD_RX, cy + HEAD_RY)
    d.ellipse(bbox, fill=HEAD_FILL, outline=HEAD_LINE, width=7)

    def ear(tip, base1, base2, inner_frac=0.38, inner_len=0.55):
        d.polygon([tip, base1, base2], fill=HEAD_FILL, outline=HEAD_LINE)
        # inner ear triangle inset from the outer triangle
        i1 = (tip[0] + (base1[0] - tip[0]) * inner_frac,
              tip[1] + (base1[1] - tip[1]) * inner_frac)
        i2 = (tip[0] + (base2[0] - tip[0]) * inner_frac,
              tip[1] + (base2[1] - tip[1]) * inner_frac)
        i3 = ((base1[0] + base2[0]) / 2 + (tip[0] - (base1[0] + base2[0]) / 2) * inner_len,
              (base1[1] + base2[1]) / 2 + (tip[1] - (base1[1] + base2[1]) / 2) * inner_len)
        d.polygon([i1, i3, i2], fill=PINK)

    # left ear tips up-left, right ear tips up-right
    ear((44, 22), (56, 104), (122, 46))
    ear((276, 22), (264, 104), (198, 46))


def whiskers(d: ImageDraw.ImageDraw) -> None:
    left = (((64, 120), (24, 111)), ((64, 139), (23, 140)), ((66, 158), (30, 168)))
    for (x0, y0), (x1, y1) in left:
        d.line((x0, y0, x1, y1), fill=WHISK, width=6)
        d.line((W - x0, y0, W - x1, y1), fill=WHISK, width=6)


def nose(d: ImageDraw.ImageDraw) -> None:
    d.polygon(NOSE_PTS, fill=PINK)


# ---- Eyes ------------------------------------------------------------------

def eye_open(d: ImageDraw.ImageDraw, cx: int, cy: int,
             w: int = EYE_W, h: int = EYE_H) -> None:
    x0, y0 = cx - w // 2, cy - h // 2
    d.rounded_rectangle((x0, y0, x0 + w, y0 + h), radius=w // 3, fill=EYE_FILL)
    d.ellipse((cx - 14, cy - 18, cx, cy - 4), fill=GLINT)     # upper-left glint


def eye_half(d: ImageDraw.ImageDraw, cx: int, cy: int,
             w: int = EYE_W, h: int = EYE_H) -> None:
    x0, y0 = cx - w // 2, cy - h // 2
    d.rounded_rectangle((x0, y0, x0 + w, y0 + h), radius=w // 3, fill=EYE_FILL)
    d.rounded_rectangle((x0, y0 + h // 2, x0 + w, y0 + h), radius=w // 3,
                        fill=HEAD_FILL)                        # lid = head color


def eye_closed(d: ImageDraw.ImageDraw, cx: int, cy: int,
               w: int = EYE_W, h: int = EYE_H) -> None:
    r = w // 2 - 4
    d.arc((cx - r, cy - r, cx + r, cy + r), 195, 345, fill=INK, width=9)


def eye_look_up(d: ImageDraw.ImageDraw, cx: int, cy: int) -> None:
    """Thinking gaze: smaller eyes raised toward top right."""
    eye_open(d, cx + 6, cy - 16, w=52, h=44)


def eye_surprised(d: ImageDraw.ImageDraw, cx: int, cy: int) -> None:
    r = 26
    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=EYE_FILL, width=9)


# ---- Brows -----------------------------------------------------------------

def brow(d: ImageDraw.ImageDraw, x: int, y: int, dy: int,
         w: int = 52, width: int = 8) -> None:
    d.line((x - w // 2, y, x + w // 2, y + dy), fill=INK, width=width)


def brows_neutral(d: ImageDraw.ImageDraw) -> None:
    brow(d, BROW_L[0], BROW_L[1], 0)
    brow(d, BROW_R[0], BROW_R[1], 0)


def brows_happy(d: ImageDraw.ImageDraw) -> None:
    for x, y in (BROW_L, BROW_R):
        d.arc((x - 34, y - 20, x + 34, y + 22), 180, 360, fill=INK, width=8)


def brows_sad(d: ImageDraw.ImageDraw) -> None:
    brow(d, BROW_L[0], BROW_L[1] + 6, -10)      # inner ends up
    brow(d, BROW_R[0], BROW_R[1] + 6, 10)


def brows_surprised(d: ImageDraw.ImageDraw) -> None:
    brow(d, BROW_L[0], BROW_L[1] - 14, 4, w=40, width=8)
    brow(d, BROW_R[0], BROW_R[1] - 14, 4, w=40, width=8)


def brows_thinking(d: ImageDraw.ImageDraw) -> None:
    brow(d, BROW_L[0], BROW_L[1] - 4, 0)
    brow(d, BROW_R[0], BROW_R[1] - 18, 12, w=42)   # right brow raised


# ---- Mouths ----------------------------------------------------------------

def mouth_omega(d: ImageDraw.ImageDraw, width: int = 8) -> None:
    """Cat's closed 'omega' mouth: two arcs meeting under the nose."""
    d.arc((MOUTH_SPAN[0] - 30, MOUTH_Y - 24, MOUTH_SPAN[0] + 2, MOUTH_Y + 8),
          35, 145, fill=INK, width=width)
    d.arc((MOUTH_SPAN[1] - 2, MOUTH_Y - 24, MOUTH_SPAN[1] + 30, MOUTH_Y + 8),
          35, 145, fill=INK, width=width)


def mouth_smile_open(d: ImageDraw.ImageDraw) -> None:
    """Happy cat smile: omega arcs + open fill + tongue."""
    d.ellipse((138, MOUTH_Y - 16, 182, MOUTH_Y + 20), fill=INK)
    d.ellipse((150, MOUTH_Y + 4, 170, MOUTH_Y + 16), fill=PINK)   # tongue
    d.arc((MOUTH_SPAN[0] - 30, MOUTH_Y - 24, MOUTH_SPAN[0] + 2, MOUTH_Y + 8),
          35, 145, fill=INK, width=8)
    d.arc((MOUTH_SPAN[1] - 2, MOUTH_Y - 24, MOUTH_SPAN[1] + 30, MOUTH_Y + 8),
          35, 145, fill=INK, width=8)


def mouth_frown(d: ImageDraw.ImageDraw, width: int = 8) -> None:
    d.arc((134, MOUTH_Y + 10, 186, MOUTH_Y + 44), 20, 160, fill=INK, width=width)


def mouth_half(d: ImageDraw.ImageDraw) -> None:
    d.ellipse((150, MOUTH_Y - 6, 170, MOUTH_Y + 10), fill=INK)


def mouth_open(d: ImageDraw.ImageDraw, big: bool = True) -> None:
    if big:
        d.ellipse((138, MOUTH_Y - 16, 182, MOUTH_Y + 20), fill=INK)
        d.ellipse((150, MOUTH_Y + 4, 170, MOUTH_Y + 16), fill=PINK)
    else:
        d.ellipse((144, MOUTH_Y - 12, 176, MOUTH_Y + 12), fill=INK)


def mouth_e(d: ImageDraw.ImageDraw) -> None:
    d.rounded_rectangle((128, MOUTH_Y - 8, 192, MOUTH_Y + 8), radius=8, fill=INK)


def mouth_u(d: ImageDraw.ImageDraw) -> None:
    d.ellipse((154, MOUTH_Y - 8, 166, MOUTH_Y + 16), fill=INK)


def mouth_wavy(d: ImageDraw.ImageDraw, width: int = 8) -> None:
    pts = []
    for i in range(9):
        x = MOUTH_SPAN[0] + (MOUTH_SPAN[1] - MOUTH_SPAN[0]) * i / 8
        y = MOUTH_Y + (7 if i % 2 else -7)
        pts.append((x, y))
    d.line(pts, fill=INK, width=width, joint="curve")


# ---- Cheeks ----------------------------------------------------------------

def blush(d: ImageDraw.ImageDraw, rr: int = BLUSH_RR) -> None:
    for cx, cy in (BLUSH_L, BLUSH_R):
        d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=BLUSH)


# ---- Faces -----------------------------------------------------------------

def _base_cat() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    im, d = new_canvas()
    draw_head(d)
    whiskers(d)
    nose(d)
    return im, d


def face_idle() -> Image.Image:
    im, d = _base_cat()
    brows_neutral(d)
    eye_open(d, *EYE_L)
    eye_open(d, *EYE_R)
    mouth_omega(d)
    return im


def face_happy() -> Image.Image:
    im, d = _base_cat()
    brows_happy(d)
    eye_closed(d, *EYE_L)
    eye_closed(d, *EYE_R)
    mouth_smile_open(d)
    blush(d)
    return im


def face_thinking() -> Image.Image:
    im, d = _base_cat()
    brows_thinking(d)
    eye_look_up(d, *EYE_L)
    eye_look_up(d, *EYE_R)
    mouth_u(d)
    return im


def face_sad() -> Image.Image:
    im, d = _base_cat()
    brows_sad(d)
    eye_half(d, *EYE_L)
    eye_half(d, *EYE_R)
    mouth_frown(d)
    return im


def face_surprised() -> Image.Image:
    im, d = _base_cat()
    brows_surprised(d)
    eye_surprised(d, *EYE_L)
    eye_surprised(d, *EYE_R)
    mouth_open(d, big=True)
    return im


def face_embarrassed() -> Image.Image:
    im, d = _base_cat()
    brows_neutral(d)
    eye_half(d, *EYE_L)
    eye_half(d, *EYE_R)
    mouth_wavy(d)
    blush(d, rr=22)
    return im


# ---- Part variants (full neutral face, only the named part changed) -------

def part_eyes(name: str) -> Image.Image:
    im, d = _base_cat()
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
    mouth_omega(d)
    return im


def part_mouth(name: str) -> Image.Image:
    im, d = _base_cat()
    brows_neutral(d)
    eye_open(d, *EYE_L)
    eye_open(d, *EYE_R)
    if name == "mouth_closed":
        mouth_omega(d)
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
