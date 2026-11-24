"""How important is an object?  One place for the rules, used by the sender, the web map and /api/packets.

Every object gets a LEVEL and a SCORE.

  level   critical, high, medium or low.  It depends only on what the object is and how its text
          describes it, so it stays the same for the life of the object.  The minimum level for
          sending to APRS-IS, and min= on /api/packets, compare levels.
  score   0-100, for putting objects in order.  It starts from the level and moves a little
          within the level's band: up while the object is new or has just changed (fading over
          two hours), down when its position is only approximate (text ending in "?").

  critical  80-100   tornado warning; entrapment, extrication, explosion, collapse, hazmat, fatal crash
  high      60-79    building fires, rescues, crashes with serious injury or a closed road,
                     severe thunderstorm / flash flood / marine / snow squall / tropical warnings
  medium    40-59    other fires, crashes, road closures, medical calls, other weather warnings,
                     watches for the severe kinds
  low        0-39    alarms, investigations, controlled burns, hazards, disabled vehicles, delays,
                     roadwork, events, weather watches, advisories and statements

The rules read the object's name tag (FIRE, TRAF, EMS, WORK, EVNT, INFO, or a weather tag such as
TOR or WND) and the start of its text, which is the call or event type.  They are only as good as
that wording.  An object with a name the rules do not know (one made by hand on the web map) is
"unrated": it is shown as medium and is never held back.

The minimum level for sending is kept in sender_settings.json in the data folder; the web map
writes it and aprs_send.py reads it.
"""
import json
import os
import re

LEVELS = ["low", "medium", "high", "critical"]
BASE = {"low": 20, "medium": 45, "high": 65, "critical": 90}
BAND = {"low": (0, 39), "medium": (40, 59), "high": (60, 79), "critical": (80, 100)}
FRESH_POINTS, FRESH_MINUTES = 8, 120
APPROX_POINTS = 5
SETTINGS_FILE = "sender_settings.json"
DEFAULT_MIN = "low"                                  # send everything

# Weather name tags (aprs_nws.py): the first three letters of the name, then the forecast office.
WX_TAGS = {"TOR", "TOA", "SVR", "SVA", "SMW", "SQW", "TRP", "FFW", "FFA", "FLD", "ICE", "WIN", "CLD", "WND",
           "FOG", "HOT", "FIR", "DST", "SPS", "WXA"}
WX_SEVERE = {"SVR", "SMW", "SQW", "FFW", "TRP"}      # a warning of these kinds is "high"
WX_WATCH_TAGS = {"TOA": "tornado watch", "SVA": "severe thunderstorm watch", "FFA": "flash flood watch"}

# Words in the call or event type.  Checked in this order; the first list that matches decides.
LIFE = re.compile(r"ENTRAP|EXTRICAT|EXPLOSION|COLLAPSE|MAYDAY|WORKING FIRE|HAZ ?MAT|FATAL|MASS CASUALTY|"
                  r"AIRCRAFT|PLANE CRASH|TRAIN ACCIDENT|WATER RESCUE|\d(ND|RD|TH) ALARM")
MINOR = re.compile(r"ALARM|INVEST|CONTROLLED BURN|BURNING COMPLAINT|NOTIFY|RELOCATE|ASSIST|FIRE POLICE|"
                   r"PUBLIC SERVICE|ELEVATOR|WIRES OUTSIDE|POLES|ODOR|DISABLED|STALLED|HAZARD|OBSTRUCTION|DEBRIS|"
                   r"SIGNAL|PROPERTY DAMAGE|NON-?CRITICAL|EXTINGUISHED|MISC|STANDBY|STAND BY|DELAY|CONGESTION|"
                   r"LOCK ?OUT|TREE DOWN")
STRUCTURE = re.compile(r"\b(BUILDING|DWELLING|HOUSE|APARTMENT|STRUCTURE|COMMERCIAL|BARN|GARAGE|RESCUE)\b")
SERIOUS = re.compile(r"PEDESTRIAN|ALS ACCIDENT|AIR MEDICAL|ROLLOVER|OVERTURN|JACK ?KNIF|MULTI.?VEHICLE|"
                     r"ALL LANES CLOSED|CARDIAC ARREST|NOT BREATHING|UNRESPONSIVE")
CLOSED = re.compile(r"ALL LANES CLOSED|ROAD CLOSED|ROAD CLOSURE|CLOSED")
HEAD = 34                                            # the type is at the start; the rest is the location
TYPE = 20                                            # building words count only this near the start: streets
                                                     # are called Barn Rd and Commercial Dr too


def kind_of(name, source=""):
    """('fire' | 'traffic' | 'ems' | 'work' | 'event' | 'info' | 'weather' | 'tropical' | '', tag)."""
    name = (name or "").strip().upper()
    if len(name) > 3 and name[2] == "-":
        return "tropical", name[:2]
    if source == "nws" and name[:3] in WX_TAGS:
        return "weather", name[:3]
    for tag, kind in (("FIRE", "fire"), ("TRAF", "traffic"), ("WORK", "work"), ("EVNT", "event"),
                      ("INFO", "info"), ("EMS", "ems")):
        if name.startswith(tag) and len(name) > len(tag):
            return kind, tag
    if name[:3] in WX_TAGS and len(name) >= 6 and name[3:6].isalpha():
        return "weather", name[:3]
    return "", ""


def level_of(name, comment, source=""):
    """-> (level, [reasons], rated)."""
    kind, tag = kind_of(name, source)
    text = re.split(r" \}", str(comment or ""))[0].upper()        # without any outline carried in the text
    head = text[:HEAD]
    if kind == "tropical":
        return "high", ["tropical cyclone"], True
    if kind == "weather":
        watch = "WATCH" in text or tag in WX_WATCH_TAGS
        mild = bool(re.search(r"\bADV\b|ADVISORY|STMT|STATEMENT", text))
        if tag == "TOR" and not watch:
            return "critical", ["tornado warning"], True
        if tag in ("TOR", "TOA"):
            return "high", ["tornado watch"], True
        if "EMERGENCY" in text:
            return "critical", ["weather emergency"], True
        if tag in WX_SEVERE and not watch and not mild:
            return "high", ["severe weather warning"], True
        if tag in WX_SEVERE or tag in WX_WATCH_TAGS:
            return "medium", [WX_WATCH_TAGS.get(tag, "watch for severe weather")], True
        if watch or mild or tag in ("SPS", "WXA"):
            return "low", ["weather watch, advisory or statement"], True
        return "medium", ["weather warning"], True
    if kind in ("fire", "traffic", "ems"):
        what = {"fire": "fire call", "traffic": "traffic incident", "ems": "medical call"}[kind]
        m = LIFE.search(text)
        if m:
            return "critical", [what, f"life at risk: {m.group(0).lower()}"], True
        m = MINOR.search(head)
        if m:
            return "low", [what, f"minor: {m.group(0).lower()}"], True
        m = (kind == "fire" and STRUCTURE.search(text[:TYPE])) or SERIOUS.search(text if kind == "traffic" else head)
        if m:
            return "high", [what, f"serious: {m.group(0).lower()}"], True
        return "medium", [what], True
    if kind == "work":
        if CLOSED.search(text) and "RAMP" not in text:
            return "medium", ["roadwork with the road closed"], True
        return "low", ["roadwork or lane restriction"], True
    if kind == "event":
        return "low", ["venue or special event traffic"], True
    if kind == "info":
        return "low", ["travel information"], True
    return "medium", ["not from a known feed: unrated"], False


def rate(name, comment, source="", age_minutes=None):
    """{level, score, reasons, rated} for one object.  age_minutes: since it was new or last changed."""
    level, reasons, rated = level_of(name, comment, source)
    score = BASE[level]
    if age_minutes is not None and age_minutes < FRESH_MINUTES:
        score += round(FRESH_POINTS * (1 - max(0.0, age_minutes) / FRESH_MINUTES))
        if age_minutes < 30:
            reasons = reasons + ["new or just changed"]
    if re.split(r" \}", str(comment or ""))[0].rstrip().endswith("?"):
        score -= APPROX_POINTS
        reasons = reasons + ["position approximate"]
    lo, hi = BAND[level]
    return {"level": level, "score": max(lo, min(hi, score)), "reasons": reasons, "rated": rated}


def rate_packet(packet, source="", age_minutes=None):
    """rate() for an object packet (';NAME.....*DDHHMMzLAT/LON$text'); None for anything else."""
    if not (isinstance(packet, str) and len(packet) >= 37 and packet[0] == ";" and packet[10] in "*_"):
        return None
    return rate(packet[1:10], packet[37:], source, age_minutes)


def at_least(level, minimum):
    return LEVELS.index(level) >= LEVELS.index(minimum)


def parse_level(text, default=None):
    """A level name from user input ('High', ' critical '), else `default`."""
    t = str(text or "").strip().lower()
    return t if t in LEVELS else default


# --------------------------------------------------------------------------
# The minimum level for sending to APRS-IS (written by the web map, read by the sender)
# --------------------------------------------------------------------------
def settings_path(folder=None):
    return os.path.join(folder or os.environ.get("APRS_DATA_DIR") or ".", SETTINGS_FILE)


def load_min_level(default=DEFAULT_MIN, folder=None):
    try:
        with open(settings_path(folder)) as f:
            return parse_level(json.load(f).get("min_level"), default)
    except (OSError, ValueError, AttributeError):
        return default


def save_min_level(level, folder=None):
    level = parse_level(level)
    if not level:
        raise ValueError("level must be one of: " + ", ".join(LEVELS))
    path = settings_path(folder)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"min_level": level}, f)
    os.replace(tmp, path)
    return level
