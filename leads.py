#!/usr/bin/env python3
"""Search Google Places for Milan venues, check their websites and score them as leads."""

from __future__ import annotations

import datetime as dt
import json
import gzip
import math
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"

# Whole-city box (Comune di Milano, slightly trimmed at the edges).
MILAN = (45.386, 9.040, 45.536, 9.278)  # south, west, north, east

# One search per category per grid cell. Google matches loosely, so the
# categories overlap; results are de-duplicated by place id.
CATEGORIES = {
    "ristorante": "ristorante",
    "trattoria": "trattoria osteria",
    "pizzeria": "pizzeria",
    "bar": "bar caffetteria",
    "pasticceria": "pasticceria gelateria",
    "enoteca": "enoteca wine bar",
}

# Approximate neighbourhood centres, used to label leads and to restrict a
# run with --zones (each zone searches a ~1.6 km box around its centre).
ZONES = {
    "Duomo": (45.4642, 9.1900),
    "Brera": (45.4719, 9.1873),
    "Porta Venezia": (45.4747, 9.2054),
    "Isola": (45.4880, 9.1890),
    "NoLo": (45.4960, 9.2210),
    "Città Studi": (45.4780, 9.2270),
    "Lambrate": (45.4840, 9.2410),
    "Porta Romana": (45.4510, 9.2040),
    "Navigli": (45.4510, 9.1760),
    "Tortona": (45.4520, 9.1640),
    "Sant'Ambrogio": (45.4620, 9.1750),
    "Sarpi": (45.4800, 9.1750),
    "CityLife": (45.4780, 9.1560),
    "San Siro": (45.4780, 9.1230),
    "Porta Genova": (45.4540, 9.1680),
    "Bicocca": (45.5180, 9.2120),
    "Niguarda": (45.5130, 9.1900),
    "Bovisa": (45.5030, 9.1600),
    "Corvetto": (45.4400, 9.2230),
    "Lorenteggio": (45.4500, 9.1320),
    "Loreto": (45.4860, 9.2160),
    "Garibaldi": (45.4840, 9.1880),
    "Ticinese": (45.4560, 9.1820),
    "Vigentino": (45.4330, 9.1980),
    "Ortica": (45.4720, 9.2500),
}

FIELDS = ",".join(
    "places." + f
    for f in [
        "id",
        "displayName",
        "formattedAddress",
        "location",
        "primaryType",
        "primaryTypeDisplayName",
        "businessStatus",
        "rating",
        "userRatingCount",
        "websiteUri",
        "nationalPhoneNumber",
        "internationalPhoneNumber",
        "regularOpeningHours",
        "photos",
        "googleMapsUri",
        "priceLevel",
    ]
) + ",nextPageToken"

SOCIAL = ("facebook.com", "instagram.com", "linktr.ee", "tiktok.com", "fb.me", "wa.me")
PLATFORMS = (
    "thefork.", "tripadvisor.", "deliveroo.", "justeat.", "glovoapp.", "ubereats.",
    "business.site", "sites.google.com", "qr-menu", "mymenu", "booking.com",
)

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129 Safari/537.36"
THIS_YEAR = dt.date.today().year


# --------------------------------------------------------------------------
# Google Places


class PlacesError(Exception):
    pass


class Budget:
    def __init__(self, limit: int):
        self.limit, self.used = limit, 0

    def take(self) -> bool:
        if self.used >= self.limit:
            return False
        self.used += 1
        return True


def search(key: str, query: str, box, budget: Budget, page_token: str | None = None):
    s, w, n, e = box
    body = {
        "textQuery": query,
        "languageCode": "it",
        "regionCode": "IT",
        "pageSize": 20,
        "locationRestriction": {
            "rectangle": {
                "low": {"latitude": s, "longitude": w},
                "high": {"latitude": n, "longitude": e},
            }
        },
    }
    if page_token:
        body["pageToken"] = page_token
    req = urllib.request.Request(
        PLACES_URL,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": key,
            "X-Goog-FieldMask": FIELDS,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as err:
        detail = err.read().decode(errors="replace")[:400]
        raise PlacesError(f"Google Places returned {err.code}: {detail}") from err


def search_cell(key, query, box, budget, depth, max_depth, found):
    """Search one box; if it fills all three result pages, split it in four."""
    token, pages = None, 0
    while pages < 3 and budget.take():
        data = search(key, query, box, budget, token)
        pages += 1
        for p in data.get("places", []):
            found.setdefault(p["id"], p)
        token = data.get("nextPageToken")
        if not token:
            return
    if token and depth < max_depth:
        s, w, n, e = box
        mlat, mlng = (s + n) / 2, (w + e) / 2
        for sub in [(s, w, mlat, mlng), (s, mlng, mlat, e), (mlat, w, n, mlng), (mlat, mlng, n, e)]:
            search_cell(key, query, sub, budget, depth + 1, max_depth, found)


def grid(box, rows, cols):
    s, w, n, e = box
    dlat, dlng = (n - s) / rows, (e - w) / cols
    return [
        (s + r * dlat, w + c * dlng, s + (r + 1) * dlat, w + (c + 1) * dlng)
        for r in range(rows)
        for c in range(cols)
    ]


def zone_box(name, half_km=0.8):
    lat, lng = ZONES[name]
    dlat = half_km / 111.0
    dlng = half_km / (111.0 * math.cos(math.radians(lat)))
    return (lat - dlat, lng - dlng, lat + dlat, lng + dlng)


def nearest_zone(lat, lng):
    def d(z):
        zl, zg = ZONES[z]
        return (lat - zl) ** 2 + ((lng - zg) * math.cos(math.radians(lat))) ** 2

    return min(ZONES, key=d)


# --------------------------------------------------------------------------
# Website check


def classify_url(url: str | None) -> str | None:
    if not url:
        return "none"
    host = urllib.parse.urlparse(url).netloc.lower()
    if any(s in host for s in SOCIAL):
        return "social"
    if any(p in host for p in PLATFORMS):
        return "platform"
    return None


def check_site(url: str) -> dict:
    kind = classify_url(url)
    if kind:
        return {"status": kind}
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "it-IT,it;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=12, context=ctx) as r:
            final = r.geturl()
            raw = r.read(2_000_000)
            if r.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            html = raw.decode(errors="replace")
    except urllib.error.URLError as err:
        if isinstance(err.reason, ssl.SSLError):
            return {"status": "broken", "error": "Invalid security certificate"}
        return {"status": "broken", "error": type(err.reason).__name__}
    except Exception as err:  # unreachable, DNS gone, 4xx/5xx, timeout
        return {"status": "broken", "error": type(err).__name__}

    final_kind = classify_url(final)
    if final_kind in ("social", "platform"):
        return {"status": final_kind}
    low = html.lower()
    years = [
        int(y)
        for m in re.finditer(r"(?:©|&copy;|copyright)([^<]{0,60})", low)
        for y in re.findall(r"\b(?:19|20)\d\d\b", m.group(1))
        if int(y) <= THIS_YEAR
    ]
    return {
        "status": "site",
        "https": final.startswith("https://"),
        "mobile": bool(re.search(r"<meta[^>]+viewport", low)),
        "year": max(years) if years else None,
        "menu": bool(re.search(r"\bmen[uù]\b|\bla carta\b|\bour menu\b", low)),
        "final": final,
    }


# --------------------------------------------------------------------------
# Scoring


def score(lead: dict, chains: set[str]) -> tuple[int, list[dict]]:
    """0-100: higher means more to sell and a business worth selling to."""
    pts, why = 0, []

    def add(n, code, text):
        nonlocal pts
        pts += n
        why.append({"code": code, "text": text, "pts": n})

    web = lead["web"]
    st = web["status"]
    if st == "none":
        add(35, "no_site", "No website")
    elif st == "social":
        add(30, "social_only", "Only a social page as website")
    elif st == "platform":
        add(28, "platform_only", "Only a booking or delivery page as website")
    elif st == "broken":
        add(30, "broken_site", "Website doesn't load")
    else:
        q = 0
        if not web.get("https"):
            q += 10
            why.append({"code": "no_https", "text": "Website not secure (no HTTPS)", "pts": 10})
        if web.get("mobile") is False:
            q += 12
            why.append({"code": "not_mobile", "text": "Website not mobile-friendly", "pts": 12})
        if web.get("year") and web["year"] <= THIS_YEAR - 3:
            q += 8
            why.append({"code": "stale", "text": f"Website last updated around {web['year']}", "pts": 8})
        if web.get("menu") is False:
            q += 4
            why.append({"code": "no_menu", "text": "No menu on the website", "pts": 4})
        pts += min(q, 25)

    g = 0
    photos = lead.get("photos")
    if photos is None:  # Google didn't report photos; don't guess
        pass
    elif photos < 3:
        g += 12
        why.append({"code": "few_photos", "text": f"Only {photos} photos on Google", "pts": 12})
    elif photos < 10:
        g += 5
        why.append({"code": "some_photos", "text": f"{photos} photos on Google", "pts": 5})
    if not lead["hasHours"]:
        g += 10
        why.append({"code": "no_hours", "text": "No opening hours on Google", "pts": 10})
    if not lead["phone"]:
        g += 6
        why.append({"code": "no_phone", "text": "No phone number on Google", "pts": 6})
    rc = lead["reviews"] or 0
    if rc < 20:
        g += 8
        why.append({"code": "few_reviews", "text": f"Only {rc} Google reviews", "pts": 8})
    elif rc < 50:
        g += 4
        why.append({"code": "low_reviews", "text": f"{rc} Google reviews", "pts": 4})
    pts += min(g, 30)

    r = lead["rating"]
    if r and r >= 4.3 and rc >= 30:
        add(15, "loved", f"Customers love it ({r}★) but it's hard to find online")
    elif r and r >= 4.0:
        add(8, "liked", f"Good rating ({r}★)")
    elif r and r < 3.5:
        add(-10, "low_rating", f"Low rating ({r}★)")

    if lead["name"].lower() in chains:
        add(-40, "chain", "Part of a chain")

    return max(0, min(100, pts)), why


def find_chains(leads: list[dict]) -> set[str]:
    """Names shared by 3+ venues that also share a website domain.

    Name alone isn't enough: Milan has dozens of unrelated "Bar Sport"s.
    """
    seen: dict[tuple[str, str], int] = {}
    for l in leads:
        host = urllib.parse.urlparse(l.get("website") or "").netloc.lower().removeprefix("www.")
        if host:
            key = (l["name"].lower(), host)
            seen[key] = seen.get(key, 0) + 1
    return {name for (name, _), n in seen.items() if n >= 3}


def tier(s: int) -> str:
    return "hot" if s >= 50 else "warm" if s >= 30 else "cool"


def to_lead(p: dict, photos_reported: bool = True) -> dict | None:
    """Flatten a Places result into a lead, or None if it has closed for good.

    Text Search often omits photos for every place; pass photos_reported=False
    then, so a missing list reads as unknown rather than zero.
    """
    if p.get("businessStatus") == "CLOSED_PERMANENTLY":
        return None
    loc = p.get("location", {})
    lat, lng = loc.get("latitude"), loc.get("longitude")
    return {
        "id": p["id"],
        "name": p.get("displayName", {}).get("text", "?"),
        "type": p.get("primaryTypeDisplayName", {}).get("text") or p.get("primaryType", ""),
        "address": p.get("formattedAddress", ""),
        "zone": nearest_zone(lat, lng) if lat is not None else "",
        "lat": lat,
        "lng": lng,
        "phone": p.get("internationalPhoneNumber") or p.get("nationalPhoneNumber") or "",
        "website": p.get("websiteUri") or "",
        "mapsUrl": p.get("googleMapsUri", ""),
        "rating": p.get("rating"),
        "reviews": p.get("userRatingCount", 0),
        "photos": len(p["photos"]) if "photos" in p else (0 if photos_reported else None),
        "hasHours": bool(p.get("regularOpeningHours")),
        "price": p.get("priceLevel", ""),
        "temporarilyClosed": p.get("businessStatus") == "CLOSED_TEMPORARILY",
    }
