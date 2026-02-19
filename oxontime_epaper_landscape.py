#!/usr/bin/env python3
"""
OxonTime -> Waveshare 2.13" ePaper HAT (B) Red/Black/White (250x122), landscape.

Features:
- Shows 3 departures.
- Emphasizes (bigger + red) the soonest bus you're likely to catch given WALK_MIN.
- Quiet hours (22:00–06:00 by default): shows a funny message and pauses updates.
- Adaptive refresh: normal (DAY_REFRESH) vs fast (FAST_REFRESH) when the catchable bus is soon.

Environment variables:
  OXON_STOP        ATCO code (default: 340000022GEO)
  WALK_MIN         minutes to walk to stop (default: 5)
  DAY_REFRESH      normal refresh seconds (default: 180)
  FAST_REFRESH     fast refresh seconds (default: 60)
  FAST_WINDOW_MIN  if catchable ETA <= this, use FAST_REFRESH (default: 10)
  QUIET_START      hour (0-23) quiet start (default: 22)
  QUIET_END        hour (0-23) quiet end (default: 6)
  QUIET_REFRESH    seconds between night redraws (default: 1800)
"""

import os
import time
import datetime as dt
import requests
from PIL import Image, ImageDraw, ImageFont

from waveshare_epd import epd2in13b_V4


# ----------------------------
# Config
# ----------------------------
STOP = os.environ.get("OXON_STOP", "340000022GEO")
URL = f"https://oxontime.com/pwi/departureBoard/{STOP}"

WALK_MIN = int(os.environ.get("WALK_MIN", "5"))

DAY_REFRESH = int(os.environ.get("DAY_REFRESH", "180"))
FAST_REFRESH = int(os.environ.get("FAST_REFRESH", "60"))
FAST_WINDOW_MIN = int(os.environ.get("FAST_WINDOW_MIN", "10"))

QUIET_START = int(os.environ.get("QUIET_START", "22"))      # 22:00
QUIET_END = int(os.environ.get("QUIET_END", "6"))           # 06:00
QUIET_REFRESH = int(os.environ.get("QUIET_REFRESH", "1800"))  # 30 min

# Canvas (landscape)
W, H = 250, 122


# ----------------------------
# Helpers
# ----------------------------
def load_fonts():
    try:
        big = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22
        )
        sm = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14
        )
        hdr = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12
        )
        return big, sm, hdr
    except Exception:
        f = ImageFont.load_default()
        return f, f, f


def in_quiet_hours(now: dt.datetime) -> bool:
    # window may wrap midnight
    if QUIET_START < QUIET_END:
        return QUIET_START <= now.hour < QUIET_END
    return (now.hour >= QUIET_START) or (now.hour < QUIET_END)


def parse_minutes(display_time: str):
    if not display_time:
        return None
    t = display_time.strip().lower()
    if "min" in t:
        try:
            return int(t.split()[0])
        except Exception:
            return None
    return None  # e.g. "21:47"


def fmt_call(call):
    route = (call.get("route_code") or "").strip()
    dest = (call.get("destination_name") or "").strip()
    disp = (call.get("display_time") or "").strip()
    eta = parse_minutes(disp)
    return route, dest, disp, eta


def fit_text(draw: ImageDraw.ImageDraw, text: str, max_w: float, font):
    if draw.textlength(text, font=font) <= max_w:
        return text
    ell = "…"
    t = text
    while t and draw.textlength(t + ell, font=font) > max_w:
        t = t[:-1]
    return (t + ell) if t else ell


def choose_catchable(top_calls):
    """
    Choose the earliest bus whose ETA (minutes) is >= WALK_MIN.
    If none has parseable minutes, fall back to first.
    """
    best_idx = None
    best_eta = None
    for i, c in enumerate(top_calls):
        _, _, _, eta = fmt_call(c)
        if eta is None:
            continue
        if eta >= WALK_MIN and (best_eta is None or eta < best_eta):
            best_eta, best_idx = eta, i
    return best_idx if best_idx is not None else 0


def choose_refresh_seconds(top_calls, catch_idx):
    if not top_calls:
        return DAY_REFRESH
    _, _, _, eta = fmt_call(top_calls[catch_idx])
    if eta is not None and eta <= FAST_WINDOW_MIN:
        return FAST_REFRESH
    return DAY_REFRESH


# ----------------------------
# Rendering
# ----------------------------
def draw_departures(epd, font_big, font_sm, font_hdr, stop_obj, top3, catch_idx):
    black = Image.new("1", (W, H), 255)
    red = Image.new("1", (W, H), 255)
    db = ImageDraw.Draw(black)
    dr = ImageDraw.Draw(red)

    now = dt.datetime.now().strftime("%H:%M")
    title = (stop_obj.get("description") or STOP).strip()
    header = f"{title}  {now}"
    db.text((4, 2), fit_text(db, header, W - 8, font_hdr), font=font_hdr, fill=0)

    # Layout positions
    y_big = 22
    y_sm1 = 60
    y_sm2 = 84
    margin = 4

    def draw_line(call, y, font, emphasize=False):
        route, dest, disp, eta = fmt_call(call)

        left = route[:3]
        right = disp

        time_w = db.textlength(right, font=font) + 6
        route_w = db.textlength(left + " ", font=font)
        max_dest_w = W - (margin * 2) - time_w - route_w

        dest_txt = fit_text(db, dest, max_dest_w, font)
        main_txt = f"{left} {dest_txt}".strip()

        d = dr if emphasize else db
        d.text((margin, y), main_txt, font=font, fill=0)

        right_x = W - margin - int(db.textlength(right, font=font))
        d.text((right_x, y), right, font=font, fill=0)

        # If emphasized and "walkable", add a small red dot indicator near the time
        if emphasize and eta is not None and eta >= WALK_MIN:
            dr.ellipse((W - margin - 6, y + 6, W - margin - 2, y + 10), fill=0)

    # Order: big = catchable, then the other two in their original order
    big_call = top3[catch_idx]
    other_calls = [top3[i] for i in range(len(top3)) if i != catch_idx]

    # Ensure 2 others exist
    while len(other_calls) < 2:
        other_calls.append({})

    draw_line(big_call, y_big, font_big, emphasize=True)
    draw_line(other_calls[0], y_sm1, font_sm, emphasize=False)
    draw_line(other_calls[1], y_sm2, font_sm, emphasize=False)

    footer = f"Walk {WALK_MIN} min • refresh auto"
    db.text((4, 106), fit_text(db, footer, W - 8, font_hdr), font=font_hdr, fill=0)

    epd.display(epd.getbuffer(black), epd.getbuffer(red))
    epd.sleep()


def draw_quiet_screen(epd, font_big, font_sm, font_hdr):
    black = Image.new("1", (W, H), 255)
    red = Image.new("1", (W, H), 255)
    db = ImageDraw.Draw(black)
    dr = ImageDraw.Draw(red)

    now = dt.datetime.now().strftime("%H:%M")
    db.text((4, 2), f"Night mode  {now}", font=font_hdr, fill=0)

    dr.text((4, 30), "Buses are sleeping.", font=font_big, fill=0)
    db.text((4, 68), "So are we. 😴", font=font_sm, fill=0)
    db.text((4, 92), "Back at 06:00.", font=font_sm, fill=0)

    epd.display(epd.getbuffer(black), epd.getbuffer(red))
    epd.sleep()


# ----------------------------
# Main loop
# ----------------------------
def main():
    epd = epd2in13b_V4.EPD()
    epd.init()

    font_big, font_sm, font_hdr = load_fonts()

    while True:
        now_dt = dt.datetime.now()
        if in_quiet_hours(now_dt):
            try:
                draw_quiet_screen(epd, font_big, font_sm, font_hdr)
            except Exception:
                pass
            time.sleep(QUIET_REFRESH)
            continue

        try:
            r = requests.get(URL, timeout=10)
            r.raise_for_status()
            data = r.json()
            stop_obj = data.get(STOP) or next(iter(data.values()))
            calls = (stop_obj.get("calls") or [])[:10]

            top3 = calls[:3]
            if not top3:
                top3 = [{}]

            catch_idx = choose_catchable(top3)
            draw_departures(epd, font_big, font_sm, font_hdr, stop_obj, top3, catch_idx)

            time.sleep(choose_refresh_seconds(top3, catch_idx))

        except Exception:
            # On failure, back off a bit
            time.sleep(DAY_REFRESH)


if __name__ == "__main__":
    main()
