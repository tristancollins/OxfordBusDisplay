#!/usr/bin/env python3
"""
OxonTime -> Waveshare 2.13" ePaper HAT (B) Red/Black/White (250x122), landscape.

DEFAULT MODE=grid:
- Current time at top-left
- Three ETAs (minutes-only) left-to-right across the screen
- Rendered as large 7-segment digits (high legibility at ~1m)
- "Most catchable" (soonest ETA >= WALK_MIN) highlighted in RED:
    * red border + corner chevrons (pops anywhere in grid)
    * normal segment thickness (not extra thick)

Non-highlight (black) digits:
- Rendered at 95% size, centered in the column (adds whitespace around)

Optional MODE=list:
- Route / destination / display_time lines (legacy)

Quiet hours:
- Between QUIET_START and QUIET_END, show a message and pause updates.

Refresh:
- DAY_REFRESH normally
- FAST_REFRESH when highlighted ETA <= FAST_WINDOW_MIN
"""

from __future__ import annotations

import os
import time
import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

import requests
from PIL import Image, ImageDraw, ImageFont

from waveshare_epd import epd2in13b_V4


# ----------------------------
# Config
# ----------------------------
STOP: str = os.environ.get("OXON_STOP", "340000022GEO")
URL: str = f"https://oxontime.com/pwi/departureBoard/{STOP}"

MODE: str = os.environ.get("MODE", "grid").strip().lower()  # "grid" (default) or "list"
WALK_MIN: int = int(os.environ.get("WALK_MIN", "5"))

DAY_REFRESH: int = int(os.environ.get("DAY_REFRESH", "180"))
FAST_REFRESH: int = int(os.environ.get("FAST_REFRESH", "60"))
FAST_WINDOW_MIN: int = int(os.environ.get("FAST_WINDOW_MIN", "10"))

QUIET_START: int = int(os.environ.get("QUIET_START", "22"))
QUIET_END: int = int(os.environ.get("QUIET_END", "6"))
QUIET_REFRESH: int = int(os.environ.get("QUIET_REFRESH", "1800"))

# Panel canvas (landscape)
W, H = 250, 122

# Non-highlight digits scale
NON_HIGHLIGHT_SCALE = 0.95  # requested 95%


# ----------------------------
# Fonts (header + list mode + quiet screen only)
# ----------------------------
def _ttf(path: str, size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def load_fonts() -> Dict[str, ImageFont.ImageFont]:
    base = "/usr/share/fonts/truetype/dejavu/"
    return {
        "hdr": _ttf(base + "DejaVuSans.ttf", 12),
        "list_big": _ttf(base + "DejaVuSans-Bold.ttf", 22),
        "list_sm": _ttf(base + "DejaVuSans.ttf", 14),
    }


# ----------------------------
# Time + parsing helpers
# ----------------------------
def in_quiet_hours(now: dt.datetime) -> bool:
    if QUIET_START < QUIET_END:
        return QUIET_START <= now.hour < QUIET_END
    return (now.hour >= QUIET_START) or (now.hour < QUIET_END)


def parse_minutes(display_time: str) -> Optional[int]:
    t = (display_time or "").strip().lower()
    if not t:
        return None
    if "min" in t:
        try:
            return int(t.split()[0])
        except Exception:
            return None
    return None


def minutes_until_clock(hhmm: str, now: dt.datetime) -> Optional[int]:
    try:
        hh, mm = hhmm.split(":")
        target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if target < now:
            target += dt.timedelta(days=1)
        return max(0, int((target - now).total_seconds() // 60))
    except Exception:
        return None


def minutes_only(call: Dict[str, Any], now: dt.datetime) -> Tuple[str, Optional[int]]:
    """
    Always return minutes display text + numeric eta.
    - "5 min" -> ("5", 5)
    - "21:47" -> ("12", 12) computed
    - else -> ("--", None)
    """
    disp = (call.get("display_time") or "").strip()
    if not disp:
        return "--", None

    eta = parse_minutes(disp)
    if eta is not None:
        return ("99+" if eta > 99 else str(eta)), eta

    if ":" in disp:
        eta2 = minutes_until_clock(disp, now)
        if eta2 is not None:
            return ("99+" if eta2 > 99 else str(eta2)), eta2

    return "--", None


def choose_catchable(calls: List[Dict[str, Any]]) -> int:
    """
    Choose earliest ETA >= WALK_MIN among provided calls.
    Fallback to 0 if nothing parseable.
    """
    now = dt.datetime.now()
    best_idx: Optional[int] = None
    best_eta: Optional[int] = None
    for i, c in enumerate(calls):
        _, eta = minutes_only(c, now)
        if eta is None:
            continue
        if eta >= WALK_MIN and (best_eta is None or eta < best_eta):
            best_eta, best_idx = eta, i
    return best_idx if best_idx is not None else 0


def choose_sleep_seconds(calls: List[Dict[str, Any]], catch_idx: int) -> int:
    if not calls:
        return DAY_REFRESH
    now = dt.datetime.now()
    _, eta = minutes_only(calls[min(catch_idx, len(calls) - 1)], now)
    if eta is not None and eta <= FAST_WINDOW_MIN:
        return FAST_REFRESH
    return DAY_REFRESH


# ----------------------------
# OxonTime fetch
# ----------------------------
def fetch_calls() -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    r = requests.get(URL, timeout=10)
    r.raise_for_status()
    data = r.json()
    stop_obj = data.get(STOP) or next(iter(data.values()))
    calls = stop_obj.get("calls") or []
    calls3 = calls[:3]
    while len(calls3) < 3:
        calls3.append({})
    return stop_obj, calls3


# ----------------------------
# 7-segment renderer
# ----------------------------

# Segment names: a(top), b(tr), c(br), d(bottom), e(bl), f(tl), g(mid)
SEGMENTS = {
    "0": "abcedf",
    "1": "bc",
    "2": "abged",
    "3": "abgcd",
    "4": "fgbc",
    "5": "afgcd",
    "6": "afgcde",
    "7": "abc",
    "8": "abcdefg",
    "9": "abfgcd",
    "-": "g",
}


def _seg_rects(x: int, y: int, w: int, h: int, t: int) -> Dict[str, Tuple[int, int, int, int]]:
    t = max(2, min(t, min(w, h) // 4))

    a = (x + t, y, x + w - t, y + t)
    d = (x + t, y + h - t, x + w - t, y + h)
    g = (x + t, y + (h - t) // 2, x + w - t, y + (h + t) // 2)

    gap = max(1, t // 2)
    top_h = max(1, (h - 3 * t) // 2)
    bot_h = top_h

    f = (x, y + t, x + t, y + t + top_h)
    b = (x + w - t, y + t, x + w, y + t + top_h)

    e = (x, y + (h // 2) + gap, x + t, y + (h // 2) + gap + bot_h)
    c = (x + w - t, y + (h // 2) + gap, x + w, y + (h // 2) + gap + bot_h)

    return {"a": a, "b": b, "c": c, "d": d, "e": e, "f": f, "g": g}


def draw_7seg_digit(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int, ch: str, thickness: int, fill: int = 0) -> None:
    rects = _seg_rects(x, y, w, h, thickness)
    for seg in SEGMENTS.get(ch, ""):
        draw.rectangle(rects[seg], fill=fill)


def _base_thickness(char_w: int, box_h: int) -> int:
    # Baseline tuned for 250x122, 3 columns: readable but not chunky.
    base = min(char_w, box_h)
    t = max(6, base // 9)
    return min(t, 14)


def draw_7seg_text(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    w: int,
    h: int,
    text: str,
    *,
    fill: int = 0,
) -> None:
    """
    Draw a short string ('5', '12', '99+') as 7-seg digits filling the box.
    '+' is drawn as a simple plus mark.
    """
    text = (text or "").strip()
    if not text:
        text = "--"

    allowed = "0123456789-+"
    text = "".join([c for c in text if c in allowed])[:3]
    if not text:
        text = "--"

    n = len(text)

    gap = max(3, w // 30)
    total_gap = gap * (n - 1)
    cw = max(12, (w - total_gap) // n)

    t = _base_thickness(cw, h)
    inset = max(2, t // 2)

    for i, ch in enumerate(text):
        cx = x + i * (cw + gap)
        box_w = min(cw, x + w - cx)
        box_h = h

        if ch == "+":
            bar = max(4, t)
            midx = cx + box_w // 2
            midy = y + box_h // 2
            draw.rectangle((midx - bar // 2, y + inset, midx + bar // 2, y + box_h - inset), fill=fill)
            draw.rectangle((cx + inset, midy - bar // 2, cx + box_w - inset, midy + bar // 2), fill=fill)
        else:
            draw_7seg_digit(
                draw,
                cx + inset,
                y + inset,
                max(6, box_w - 2 * inset),
                max(6, box_h - 2 * inset),
                ch,
                t,
                fill=fill,
            )


def draw_pop_frame(dr: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int) -> None:
    bt = 2
    dr.rectangle((x, y, x + w, y + h), outline=0, width=bt)

    s = 8
    dr.polygon([(x, y), (x + s, y), (x, y + s)], fill=0)
    dr.polygon([(x + w, y), (x + w - s, y), (x + w, y + s)], fill=0)
    dr.polygon([(x, y + h), (x + s, y + h), (x, y + h - s)], fill=0)
    dr.polygon([(x + w, y + h), (x + w - s, y + h), (x + w, y + h - s)], fill=0)


# ----------------------------
# Rendering
# ----------------------------
def draw_grid(epd, fonts: Dict[str, ImageFont.ImageFont], calls3: List[Dict[str, Any]], catch_idx: int) -> None:
    """
    Big 7-seg minutes-only display.
    - Catchable column: red border + red digits (normal thickness)
    - Non-catchable columns: black digits at 95% size, centered (more whitespace)
    """
    black = Image.new("1", (W, H), 255)
    red = Image.new("1", (W, H), 255)
    db = ImageDraw.Draw(black)
    dr = ImageDraw.Draw(red)

    now_dt = dt.datetime.now()
    db.text((4, 1), now_dt.strftime("%H:%M"), font=fonts["hdr"], fill=0)

    margin_x = 2
    gap = 3
    col_w = (W - margin_x * 2 - gap * 2) // 3

    top_y = 12
    box_h = H - top_y - 2

    for i in range(3):
        call = calls3[i] if i < len(calls3) else {}
        txt, _eta = minutes_only(call, now_dt)

        x0 = margin_x + i * (col_w + gap)
        y0 = top_y

        is_catch = (i == catch_idx)

        if is_catch:
            # Red frame to draw attention
            draw_pop_frame(dr, x0, y0, col_w, box_h)

            # Red digits fill the whole column box
            draw_7seg_text(dr, x0, y0, col_w, box_h, txt, fill=0)
        else:
            # Black digits scaled down to 95% and centered
            sw = int(col_w * NON_HIGHLIGHT_SCALE)
            sh = int(box_h * NON_HIGHLIGHT_SCALE)
            sx = x0 + (col_w - sw) // 2
            sy = y0 + (box_h - sh) // 2
            draw_7seg_text(db, sx, sy, sw, sh, txt, fill=0)

    epd.display(epd.getbuffer(black), epd.getbuffer(red))


def draw_list(epd, fonts: Dict[str, ImageFont.ImageFont], stop_obj: Dict[str, Any], calls3: List[Dict[str, Any]], catch_idx: int) -> None:
    black = Image.new("1", (W, H), 255)
    red = Image.new("1", (W, H), 255)
    db = ImageDraw.Draw(black)
    dr = ImageDraw.Draw(red)

    now = dt.datetime.now().strftime("%H:%M")
    title = (stop_obj.get("description") or STOP).strip()
    db.text((4, 2), f"{title} {now}"[:30], font=fonts["hdr"], fill=0)

    ys = [22, 60, 84]
    for i, y in enumerate(ys):
        c = calls3[i]
        route = (c.get("route_code") or "")[:3]
        dest = (c.get("destination_name") or "")
        disp = (c.get("display_time") or "")
        line = f"{route} {dest} {disp}".strip()

        if i == catch_idx:
            dr.text((4, y), line[:28], font=fonts["list_big"], fill=0)
        else:
            db.text((4, y), line[:34], font=fonts["list_sm"], fill=0)

    epd.display(epd.getbuffer(black), epd.getbuffer(red))


def draw_quiet(epd, fonts: Dict[str, ImageFont.ImageFont]) -> None:
    black = Image.new("1", (W, H), 255)
    red = Image.new("1", (W, H), 255)
    db = ImageDraw.Draw(black)
    dr = ImageDraw.Draw(red)

    now = dt.datetime.now().strftime("%H:%M")
    db.text((4, 2), f"Night {now}", font=fonts["hdr"], fill=0)
    dr.text((4, 30), "Buses are sleeping.", font=fonts["list_big"], fill=0)
    db.text((4, 68), "So are we :)", font=fonts["list_sm"], fill=0)
    db.text((4, 92), f"Back {QUIET_END:02d}:00", font=fonts["list_sm"], fill=0)

    epd.display(epd.getbuffer(black), epd.getbuffer(red))
    epd.sleep()


# ----------------------------
# Main loop
# ----------------------------
def main() -> None:
    fonts = load_fonts()
    epd = epd2in13b_V4.EPD()
    epd.init()

    try:
        while True:
            now = dt.datetime.now()

            if in_quiet_hours(now):
                draw_quiet(epd, fonts)
                time.sleep(QUIET_REFRESH)
                epd.init()
                continue

            try:
                stop_obj, calls3 = fetch_calls()
                catch_idx = choose_catchable(calls3)

                if MODE == "list":
                    draw_list(epd, fonts, stop_obj, calls3, catch_idx)
                else:
                    draw_grid(epd, fonts, calls3, catch_idx)

                time.sleep(choose_sleep_seconds(calls3, catch_idx))

            except requests.RequestException:
                time.sleep(DAY_REFRESH)
            except ValueError:
                time.sleep(DAY_REFRESH)

    except KeyboardInterrupt:
        pass
    finally:
        try:
            epd.sleep()
        except Exception:
            pass


if __name__ == "__main__":
    main()
