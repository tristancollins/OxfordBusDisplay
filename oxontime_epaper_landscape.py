#!/usr/bin/env python3
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

MODE = os.environ.get("MODE", "grid").lower()
WALK_MIN = int(os.environ.get("WALK_MIN", "5"))

DAY_REFRESH = int(os.environ.get("DAY_REFRESH", "180"))
FAST_REFRESH = int(os.environ.get("FAST_REFRESH", "60"))
FAST_WINDOW_MIN = int(os.environ.get("FAST_WINDOW_MIN", "10"))

QUIET_START = int(os.environ.get("QUIET_START", "22"))
QUIET_END = int(os.environ.get("QUIET_END", "6"))
QUIET_REFRESH = int(os.environ.get("QUIET_REFRESH", "1800"))

W, H = 250, 122

# ----------------------------
# Helpers
# ----------------------------
def load_fonts():
    try:
        return {
            "hdr": ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12),
            "list_big": ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22),
            "list_sm": ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14),
            "grid_big": ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 34),
            "grid_med": ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28),
        }
    except:
        f = ImageFont.load_default()
        return {"hdr": f, "list_big": f, "list_sm": f, "grid_big": f, "grid_med": f}

def in_quiet_hours(now):
    if QUIET_START < QUIET_END:
        return QUIET_START <= now.hour < QUIET_END
    return now.hour >= QUIET_START or now.hour < QUIET_END

def parse_minutes(display_time):
    if not display_time: return None
    t = display_time.lower()
    if "min" in t:
        try: return int(t.split()[0])
        except: return None
    return None

def minutes_until_clock(hhmm, now):
    try:
        hh, mm = map(int, hhmm.split(":"))
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target < now: target += dt.timedelta(days=1)
        return int((target-now).total_seconds()//60)
    except:
        return None

def minutes_only(call, now):
    disp = (call.get("display_time") or "").strip()
    eta = parse_minutes(disp)
    if eta is not None:
        return str(min(eta,99)) if eta<=99 else "99+", eta
    if ":" in disp:
        eta2 = minutes_until_clock(disp, now)
        if eta2 is not None:
            return str(min(eta2,99)) if eta2<=99 else "99+", eta2
    return "--", None

def choose_catchable(top3):
    now = dt.datetime.now()
    best_i, best_eta = None, None
    for i,c in enumerate(top3):
        _,eta = minutes_only(c, now)
        if eta is None: continue
        if eta>=WALK_MIN and (best_eta is None or eta<best_eta):
            best_i, best_eta = i, eta
    return best_i if best_i is not None else 0

def choose_refresh(top3, idx):
    now = dt.datetime.now()
    _,eta = minutes_only(top3[idx], now)
    if eta and eta<=FAST_WINDOW_MIN: return FAST_REFRESH
    return DAY_REFRESH

# ----------------------------
# Render: GRID (default)
# ----------------------------
def draw_grid(epd, fonts, top3, catch_idx):
    black = Image.new("1",(W,H),255)
    red = Image.new("1",(W,H),255)
    db, dr = ImageDraw.Draw(black), ImageDraw.Draw(red)

    now = dt.datetime.now()
    db.text((4,2), now.strftime("%H:%M"), font=fonts["hdr"], fill=0)

    margin, gap = 6, 6
    col_w = (W-margin*2-gap*2)//3
    y0, box_h = 22, H-28

    for i in range(3):
        call = top3[i] if i<len(top3) else {}
        txt,eta = minutes_only(call, now)
        x = margin + i*(col_w+gap)
        emphasize = i==catch_idx
        font = fonts["grid_big"] if emphasize else fonts["grid_med"]
        d = dr if emphasize else db

        tw = d.textlength(txt,font=font)
        bbox = d.textbbox((0,0),txt,font=font)
        th = bbox[3]-bbox[1]
        cx = x+(col_w-int(tw))//2
        cy = y0+(box_h-th)//2
        d.text((cx,cy),txt,font=font,fill=0)

        if emphasize and eta and eta>=WALK_MIN:
            dr.ellipse((x+col_w-10,y0+6,x+col_w-4,y0+12),fill=0)

    epd.display(epd.getbuffer(black),epd.getbuffer(red))

# ----------------------------
# Render: LIST
# ----------------------------
def draw_list(epd, fonts, top3, catch_idx):
    black = Image.new("1",(W,H),255)
    red = Image.new("1",(W,H),255)
    db, dr = ImageDraw.Draw(black), ImageDraw.Draw(red)

    now = dt.datetime.now().strftime("%H:%M")
    db.text((4,2),now,font=fonts["hdr"],fill=0)

    ys=[22,60,84]
    for i,y in enumerate(ys):
        call = top3[i] if i<len(top3) else {}
        route = (call.get("route_code") or "")[:3]
        dest = (call.get("destination_name") or "")
        disp = call.get("display_time") or ""
        txt = f"{route} {dest} {disp}"
        (dr if i==catch_idx else db).text((4,y),txt,font=fonts["list_big"] if i==catch_idx else fonts["list_sm"],fill=0)

    epd.display(epd.getbuffer(black),epd.getbuffer(red))

# ----------------------------
# Quiet screen
# ----------------------------
def draw_quiet(epd, fonts):
    black = Image.new("1",(W,H),255)
    red = Image.new("1",(W,H),255)
    db, dr = ImageDraw.Draw(black), ImageDraw.Draw(red)

    now = dt.datetime.now().strftime("%H:%M")
    db.text((4,2),f"Night {now}",font=fonts["hdr"],fill=0)
    dr.text((4,30),"Buses are sleeping.",font=fonts["list_big"],fill=0)
    db.text((4,68),"So are we :)",font=fonts["list_sm"],fill=0)
    epd.display(epd.getbuffer(black),epd.getbuffer(red))
    epd.sleep()

# ----------------------------
# Main
# ----------------------------
def main():
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

            data = requests.get(URL,timeout=10).json()
            stop = data.get(STOP) or next(iter(data.values()))
            calls = (stop.get("calls") or [])[:3] or [{}]
            catch = choose_catchable(calls)

            if MODE=="list":
                draw_list(epd,fonts,calls,catch)
            else:
                draw_grid(epd,fonts,calls,catch)

            time.sleep(choose_refresh(calls,catch))

    except KeyboardInterrupt:
        pass
    finally:
        try: epd.sleep()
        except: pass

if __name__=="__main__":
    main()
