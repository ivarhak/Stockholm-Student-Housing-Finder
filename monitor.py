#!/usr/bin/env python3
"""
Stockholm Student Housing Finder
================================

Scrapes SSSB's currently available student housing (minasidor.sssb.se) plus
Bostadsförmedlingen's public student ads, works out how far each place is from
your campus (both a rough straight-line estimate and a real public-transit time
via Trafiklab's Resrobot API), diffs against the last run to spot newly-published
listings, fires a desktop notification when something new shows up, and serves
it all to the dashboard (index.html) over a tiny local API.

NO LOGIN NEEDED: SSSB's vacancy list is public — confirmed 2026-08-06,
queue days ("Ködagar") included. Nothing here asks for credentials by
default. `--with-login` still exists as an escape hatch if SSSB ever puts the
list back behind a login, in which case `--login` stores credentials in your
OS keychain (macOS Keychain / Windows Credential Locker / Linux Secret
Service via `keyring`) rather than any file that could end up in a commit.

WHY SELENIUM: the listings page renders its content client-side (the raw HTML
is just template placeholders until their JS app runs), so a real browser is
used to render the page before parsing it. Whether the underlying data is
reachable as plain JSON — which would drop the browser requirement entirely —
is still an open question; see CLAUDE.md.

Usage:
    ./start.command                             # easiest: sets up the venv if needed, then serves
    python monitor.py                  # serve the dashboard and open it in a browser
    python monitor.py --once           # one scrape, save + notify, exit
    python monitor.py --no-browser     # serve, but don't open a browser
    python monitor.py --http-only      # no browser: fail instead of falling back to Selenium
    python monitor.py --debug          # also dump rendered HTML to debug_page.html
    python monitor.py --with-login     # only if SSSB starts requiring a login again
"""

import argparse
import getpass
import json
import math
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    import keyring
    KEYRING_AVAILABLE = True
except ImportError:
    KEYRING_AVAILABLE = False

# ── Config ────────────────────────────────────────────────────────────────

KEYRING_SERVICE = "sssb-kth-tool"


def _keyring_get(username: str):
    """Read one secret from the OS keychain, or None.

    `keyring` importing successfully doesn't mean it can *work*: with no backend
    available (a headless Linux box with no Secret Service, a bare container) the
    first call raises `NoKeyringError`. This used to run bare at import time, so
    the whole tool died on line 58 with a keyring stack trace before doing
    anything — while trying to read an optional API key that isn't needed at all.
    Nothing here requires credentials, so a missing keychain must degrade to None.
    """
    if not KEYRING_AVAILABLE:
        return None
    try:
        return keyring.get_password(KEYRING_SERVICE, username)
    except Exception:
        return None


RESROBOT_API_KEY = os.environ.get("RESROBOT_API_KEY") or _keyring_get("resrobot_api_key")

LOGIN_URL = "https://minasidor.sssb.se/en/login/"
# Ivar found that SSSB's listings page takes `pagination`/`paginationantal`
# query params directly — requesting a page size of 200 (there are ~76
# listings total) returns everything in one render, so we don't need to
# click through a numbered pager at all.
LISTINGS_URL = "https://minasidor.sssb.se/lediga-bostader/?pagination=1&paginationantal=200"

# Bostadsförmedlingen (Stockholm's city housing agency) publishes all current
# ads as JSON, no login needed. Svenska Bostäder's student apartments are
# advertised THROUGH this same system (their own site links every listing to
# bostad.stockholm.se/bostad/<id>), so this one feed covers both of Ivar's
# requested extra sources.
#
# CONFIRMED BROKEN 2026-08-08: /Lista/AllaAnnonser now 404s — it was carried
# over from a community scraper and never verified. The candidates below are
# tried in order and the first one returning a JSON list wins; set
# BF_ADS_URL in the environment to override without editing this file.
# To find the real one: open bostad.stockholm.se's search page, DevTools →
# Network → Fetch/XHR, and copy the request that returns the ad list.
BF_ALL_ADS_URLS = [u for u in [
    os.environ.get("BF_ADS_URL"),
    # Confirmed from the real site's own network traffic (2026-08-08): the
    # search page's JS bundle XHRs this and gets ~554 kB of JSON back. The
    # trailing slash matters, and the old path had a spurious /Lista/ prefix.
    "https://bostad.stockholm.se/AllaAnnonser/",
    "https://bostad.stockholm.se/AllaAnnonser",
    "https://bostad.stockholm.se/Lista/AllaAnnonser",
] if u]
BF_ALL_ADS_URL = BF_ALL_ADS_URLS[0]  # kept for anything referencing the old name

# Real cycling directions, no API key: FOSSGIS's public Valhalla instance,
# which routes over OSM's actual bike network instead of pretending a straight
# line is a road. Community-run, so results are cached to disk and requests are
# paced — and every failure degrades to the old haversine estimate rather than
# breaking the run. The response shape below is per Valhalla's documented
# `trip.summary` and has NOT been verified against the live service from here.
BIKE_ROUTER_URL = "https://valhalla1.openstreetmap.de/route"

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
CURRENT_FILE = DATA_DIR / "current_listings.json"
GEOCODE_CACHE_FILE = DATA_DIR / "geocode_cache.json"
BIKE_ROUTE_CACHE_FILE = DATA_DIR / "bike_route_cache.json"
GROCERY_CACHE_FILE = DATA_DIR / "grocery_cache.json"
DEBUG_HTML_FILE = Path(__file__).parent / "debug_page.html"

PORT = int(os.environ.get("PORT", 5055))

# Stockholm campuses you can centre the map on, picked from the dashboard's
# dropdown. These were already in the dashboard as reference pins; they live
# here now so the server can work out the commute to each one and the two lists
# can't drift apart. Keys are the short names the dashboard shows.
SCHOOLS = {
    # KTH main campus, Valhallavägen 79 — well-established coordinates.
    "KTH":       {"name": "KTH Royal Institute of Technology", "coords": (59.3467, 18.0716)},
    "SU":        {"name": "Stockholm University",              "coords": (59.36397, 18.06002)},
    "KI":        {"name": "Karolinska Institutet",             "coords": (59.34848, 18.02790)},
    "SSE":       {"name": "Stockholm School of Economics",     "coords": (59.34170, 18.05726)},
    "Konstfack": {"name": "Konstfack",                         "coords": (59.29977, 17.99421)},
    "KMH":       {"name": "Royal College of Music",            "coords": (59.34447, 18.08172)},
    "KKH":       {"name": "Royal Institute of Art",            "coords": (59.32458, 18.08213)},
}
DEFAULT_SCHOOL = "KTH"
# Kept as its own name because it's referenced all over, and because KTH stays
# the default centre — this started life as a KTH tool.
KTH_COORDS = SCHOOLS[DEFAULT_SCHOOL]["coords"]

# The 26 SSSB housing areas, grouped exactly the way SSSB groups them on
# sssb.se/en/our-homes/ (North / South / City).
AREAS = {
    "North": ["Freja", "Frösunda", "Kungshamra", "Lappkärrsberget", "Pax", "Strix"],
    "South": ["Balder", "Birka", "Embla", "Flemingsberg", "Skärmarbrink"],
    "City": [
        "Apeln", "Domus", "Forum", "Fyrtalet", "Hugin & Munin", "Idun",
        "Jerum", "Kurland", "Lucidor", "Marieberg", "Mjölner", "Nyponet",
        "Roslagstull", "Tanto", "Vätan",
    ],
}
ALL_AREAS = [a for group in AREAS.values() for a in group]

# SSSB's own name for an area doesn't always match what its listing cards print.
# Confirmed live (2026-08-08): the cards for Öregrundsgatan say "Munin" (and
# presumably "Hugin"), never the combined "Hugin & Munin" that SSSB uses on its
# area pages, so those listings were coming back area="Unknown" and dropping off
# the map. Longest needle wins, so a plain area name always beats an alias.
AREA_ALIASES = {
    "Hugin": "Hugin & Munin",
    "Munin": "Hugin & Munin",
}
# (needle, canonical area) pairs, longest needle first
_AREA_NEEDLES = sorted(
    [(a, a) for a in ALL_AREAS] + list(AREA_ALIASES.items()),
    key=lambda pair: len(pair[0]), reverse=True,
)

# Real street addresses (pulled from each area's page on sssb.se/en/) used to
# geocode precisely — geocoding on the bare area name alone (e.g. "Balder,
# Stockholm, Sweden") is unreliable since several of these are common Norse
# names/words that Nominatim can match to an unrelated place; this caused
# real, confirmed bad pins (Balder resolving ~30km south near Nynäshamn,
# Birka resolving near Mariefred, Strix resolving to the wrong Stockholm
# location) and some empty results (Jerum, Domus, Lucidor, Nyponet). If a pin
# still looks wrong, hand-correct the `[lat, lon]` in data/geocode_cache.json
# directly rather than editing the address here.
AREA_ADDRESSES = {
    "Freja": "Gärdesvägen 2, 183 30 Täby, Sweden",
    "Frösunda": "Gustav III:s Boulevard 2, 169 72 Solna, Sweden",
    "Kungshamra": "Kungshamra 1, 170 70 Solna, Sweden",
    "Lappkärrsberget": "Professorsslingan 9, 114 17 Stockholm, Sweden",
    "Pax": "Emmylundsvägen 1, 171 72 Solna, Sweden",
    "Strix": "Armégatan 32, 171 59 Solna, Sweden",
    "Balder": "Edinsvägen 22, 131 47 Nacka, Sweden",
    "Birka": "Simrishamnsvägen 15, 121 53 Johanneshov, Sweden",
    "Embla": "Maltgatan 4, 120 79 Stockholm, Sweden",
    "Flemingsberg": "Röntgenvägen 1, 141 52 Huddinge, Sweden",
    "Skärmarbrink": "Nathorstvägen 46, 121 37 Johanneshov, Sweden",
    "Apeln": "Drottninggatan 67, 111 36 Stockholm, Sweden",
    "Domus": "Körsbärsvägen 3, 114 23 Stockholm, Sweden",
    "Forum": "Körsbärsvägen 2, 114 23 Stockholm, Sweden",
    "Fyrtalet": "Värtavägen 66, 115 38 Stockholm, Sweden",
    "Hugin & Munin": "Öregrundsgatan 9, 115 59 Stockholm, Sweden",
    "Idun": "Norra Stationsgatan 99, 113 64 Stockholm, Sweden",
    "Jerum": "Studentbacken 21, 115 57 Stockholm, Sweden",
    "Kurland": "Holländargatan 21, 111 60 Stockholm, Sweden",
    "Lucidor": "Skomakargatan 24, 111 29 Stockholm, Sweden",
    "Marieberg": "Fyrverkarbacken 23, 112 60 Stockholm, Sweden",
    "Mjölner": "Löjtnantsgatan 11, 115 50 Stockholm, Sweden",
    "Nyponet": "Körsbärsvägen 9, 114 23 Stockholm, Sweden",
    "Roslagstull": "Roslagstullsbacken 5, 114 22 Stockholm, Sweden",
    "Tanto": "Tantogatan 59, 118 42 Stockholm, Sweden",
    "Vätan": "David Bagares gata 6, 111 38 Stockholm, Sweden",
}


# ── Credentials ───────────────────────────────────────────────────────────
# Nothing here ever gets written to a file inside this project folder — so
# there's nothing here for a `git add .` / accidental push to leak.

_cred_cache = {}


def _prompt_and_store():
    print("\nSSSB login (this is not written to any file in this folder):")
    username = input("  Username (personnummer or p-number): ").strip()
    password = getpass.getpass("  Password: ")

    if KEYRING_AVAILABLE:
        save = input(
            "  Save to this computer's secure keychain so you're not asked again? [y/N]: "
        ).strip().lower()
        if save == "y":
            try:
                keyring.set_password(KEYRING_SERVICE, "username", username)
                keyring.set_password(KEYRING_SERVICE, "password", password)
                print("  Saved to your OS keychain. Run --forget-login later to remove it.")
            except Exception as e:
                print(f"  Couldn't write to the keychain ({type(e).__name__}: {e}).")
                print("  Continuing with these credentials for this run only.")
    else:
        print("  (install the 'keyring' package to save this for next time)")

    return username, password


def get_credentials() -> tuple[str, str]:
    """Resolve SSSB credentials, in order: already-prompted this run →
    OS keychain (if saved via --login) → SSSB_USERNAME/SSSB_PASSWORD env vars
    (for cron/unattended setups where the keychain isn't reachable — set
    these directly in your crontab/task, not in a file in this folder) →
    interactive prompt.
    """
    if _cred_cache:
        return _cred_cache["username"], _cred_cache["password"]

    kr_user = _keyring_get("username")
    kr_pass = _keyring_get("password") if kr_user else None
    if kr_user and kr_pass:
        _cred_cache.update(username=kr_user, password=kr_pass)
        return kr_user, kr_pass

    env_user, env_pass = os.environ.get("SSSB_USERNAME"), os.environ.get("SSSB_PASSWORD")
    if env_user and env_pass:
        _cred_cache.update(username=env_user, password=env_pass)
        return env_user, env_pass

    if not sys.stdin.isatty():
        raise SystemExit(
            "No saved credentials, and this doesn't look like an interactive terminal "
            "(likely a cron/scheduled run). Run `python monitor.py --login` "
            "once by hand first to store credentials in your OS keychain, then "
            "unattended runs will pick them up automatically."
        )

    username, password = _prompt_and_store()
    _cred_cache.update(username=username, password=password)
    return username, password


def forget_credentials():
    if not KEYRING_AVAILABLE:
        print("keyring isn't installed — there's nothing stored to remove.")
        return
    for key in ("username", "password", "resrobot_api_key"):
        try:
            keyring.delete_password(KEYRING_SERVICE, key)
        except Exception:
            pass   # not stored, or no working backend — either way, nothing to remove
    print("Removed any saved credentials from your OS keychain.")


# ── Geocoding (OpenStreetMap Nominatim — free, no key) ──────────────────────

def _load_geocode_cache():
    if GEOCODE_CACHE_FILE.exists():
        return json.loads(GEOCODE_CACHE_FILE.read_text())
    return {}


def _save_geocode_cache(cache):
    GEOCODE_CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False))


def geocode_area(name: str, cache: dict) -> tuple | None:
    """Look up (lat, lon) for an SSSB area name, cached to disk.

    You can hand-correct any entry by editing data/geocode_cache.json directly
    — e.g. if Nominatim resolves "Pax" to the wrong Pax somewhere in Sweden.
    """
    if name in cache and cache[name]:
        return tuple(cache[name])

    query = AREA_ADDRESSES.get(name, f"{name}, Stockholm, Sweden")
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1},
            headers={"User-Agent": "sssb-kth-commute-tool/1.0 (personal use)"},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json()
        if results:
            coords = (float(results[0]["lat"]), float(results[0]["lon"]))
            cache[name] = list(coords)
            _save_geocode_cache(cache)
            time.sleep(1)  # respect Nominatim's 1 req/sec usage policy
            return coords
    except requests.RequestException as e:
        print(f"  ! geocoding failed for {name}: {e}")

    cache[name] = None
    _save_geocode_cache(cache)
    return None


# ── Per-listing addresses ────────────────────────────────────────────────
#
# SSSB listings carry no coordinates, only an area — which is fine for a city
# view but wrong once you zoom into somewhere like Lappkärrsberget, where one
# roundel stands for a dozen buildings spread over half a kilometre. Geocoding
# each card's street address lets the map break that roundel apart.
#
# Cheap in practice: the ~76 listings share far fewer buildings than that, and
# the buildings never move, so this converges to a warm cache after a run or two
# and costs nothing thereafter. Anything that fails falls back to the area
# centre, which is exactly where it sat before.

ADDRESS_CACHE_FILE = DATA_DIR / "address_cache.json"


def _load_address_cache() -> dict:
    if ADDRESS_CACHE_FILE.exists():
        try:
            return json.loads(ADDRESS_CACHE_FILE.read_text())
        except ValueError:
            print("  ! address cache unreadable — starting a fresh one")
    return {}


def _save_address_cache(cache: dict):
    ADDRESS_CACHE_FILE.write_text(json.dumps(cache, indent=1, ensure_ascii=False))


def geocode_listing_address(address: str, area: str, cache: dict) -> list | None:
    """(lat, lon) for one street address, cached to disk. None if unresolvable.

    Queried with the area's own postcode/city tail where we have one, because
    "Forskarbacken 10, Sweden" is ambiguous nationally while
    "Forskarbacken 10, 114 17 Stockholm, Sweden" is not. Hand-correct
    data/address_cache.json if a dot ever lands somewhere silly, same as the
    area cache.
    """
    if address in cache:
        return cache[address]

    # Reuse the area's verified city/postcode tail so the search is anchored to
    # the right municipality — several of these areas aren't in Stockholm proper
    # (Solna, Täby, Nacka, Huddinge).
    area_query = AREA_ADDRESSES.get(area, "")
    tail = ", ".join(area_query.split(", ")[1:]) if ", " in area_query else "Stockholm, Sweden"
    query = f"{address}, {tail}"
    coords = None
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1},
            headers={"User-Agent": "sssb-kth-commute-tool/1.0 (personal use)"},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json()
        if results:
            coords = [float(results[0]["lat"]), float(results[0]["lon"])]
        time.sleep(1)  # Nominatim's 1 req/sec usage policy
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"  ! address lookup failed for {address!r}: {e}")
        # Not cached as a failure: a network blip shouldn't permanently blacklist
        # a real address. A *resolved-to-nothing* answer is cached below.
        return None

    cache[address] = coords
    _save_address_cache(cache)
    return coords


def area_spread_m(coords_list: list) -> int:
    """How far apart an area's listings actually are — the widest gap between any
    two of them, in metres.

    This is what decides whether an area is worth breaking apart on the map. A
    single building comes out near zero and stays one roundel; a campus like
    Lappkärrsberget comes out in the hundreds.
    """
    if len(coords_list) < 2:
        return 0
    widest = 0.0
    for i, a in enumerate(coords_list):
        for b in coords_list[i + 1:]:
            widest = max(widest, haversine_km(tuple(a), tuple(b)))
    return int(round(widest * 1000))


# ── Grocery stores (OpenStreetMap Overpass — free, no key) ───────────────
#
# "Is there a supermarket near this place?" is a real question when picking
# somewhere to live, and it's the one thing neither housing source says anything
# about. OSM has it, so this pulls the big chains once and caches them: shops
# don't move, so re-fetching on every scrape would be pointless load on a free
# community service. Same convention as the geocode and bike-route caches.

OVERPASS_URL = os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
# Greater Stockholm, comfortably covering every SSSB area and the BF ads —
# Flemingsberg in the south, Kista in the north-west.
GROCERY_BBOX = (59.15, 17.75, 59.50, 18.35)   # south, west, north, east
GROCERY_MAX_AGE_DAYS = 30

# Only the chains, deliberately. Matching every `shop=convenience` would triple
# the count with corner shops and kiosks, which isn't what "grocery store" means
# when you're working out whether you can do a weekly shop nearby.
GROCERY_CHAINS = {
    "ICA": ("ica",),
    "Coop": ("coop",),
    "Willys": ("willys",),
    "Hemköp": ("hemköp", "hemkop"),
    "Lidl": ("lidl",),
    "City Gross": ("city gross", "citygross"),
}


def _grocery_chain(*names) -> str | None:
    """Which chain a store belongs to, by brand or name, or None for an
    independent. Checked against `brand` first since that's the tag that's
    actually meant to carry it; `name` is the fallback because plenty of Swedish
    entries only fill in "ICA Nära Something"."""
    for value in names:
        if not value:
            continue
        low = str(value).lower()
        for chain, needles in GROCERY_CHAINS.items():
            if any(n in low for n in needles):
                return chain
    return None


def _load_grocery_cache() -> dict | None:
    """Cached stores, or None if missing/stale/unreadable."""
    if not GROCERY_CACHE_FILE.exists():
        return None
    try:
        cached = json.loads(GROCERY_CACHE_FILE.read_text())
        fetched = datetime.fromisoformat(cached["fetched_at"])
    except (ValueError, KeyError, TypeError, OSError):
        return None
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - fetched).total_seconds() / 86400
    if age_days > GROCERY_MAX_AGE_DAYS:
        print(f"  grocery cache is {age_days:.0f} days old — refetching")
        return None
    print(f"  {len(cached['stores'])} grocery store(s) from cache "
          f"({age_days:.0f} days old)")
    return cached


def fetch_grocery_stores() -> list[dict]:
    """Chain supermarkets in greater Stockholm, as [{name, chain, coords}].

    Never fatal: the map is perfectly usable without shop dots, so any failure
    degrades to an empty list with a printed reason rather than stopping a scrape.
    """
    cached = _load_grocery_cache()
    if cached:
        return cached["stores"]

    south, west, north, east = GROCERY_BBOX
    bbox = f"{south},{west},{north},{east}"
    # `out center` so ways (a supermarket mapped as a building outline rather
    # than a point) still come back with a single coordinate.
    query = f"""
[out:json][timeout:90];
(
  node["shop"="supermarket"]({bbox});
  way["shop"="supermarket"]({bbox});
);
out center tags;
"""
    print(f"fetching grocery stores from OpenStreetMap (Overpass, cached "
          f"{GROCERY_MAX_AGE_DAYS} days)...")
    try:
        resp = requests.post(
            OVERPASS_URL,
            data={"data": query},
            headers={"User-Agent": "personal student-housing monitor"},
            timeout=120,
        )
        resp.raise_for_status()
        elements = resp.json().get("elements", [])
    except (requests.RequestException, ValueError) as e:
        print(f"  ! grocery lookup failed ({type(e).__name__}: {e}) — "
              "continuing without shop markers")
        return []

    stores, skipped, no_coords = [], 0, 0
    for el in elements:
        tags = el.get("tags") or {}
        chain = _grocery_chain(tags.get("brand"), tags.get("operator"), tags.get("name"))
        if not chain:
            skipped += 1
            continue
        centre = el.get("center") or el
        lat, lon = centre.get("lat"), centre.get("lon")
        if lat is None or lon is None:
            # A way whose geometry Overpass didn't resolve. Counted rather than
            # dropped silently, so the printed numbers add up to the input.
            no_coords += 1
            continue
        stores.append({
            "name": tags.get("name") or chain,
            "chain": chain,
            "coords": [round(float(lat), 6), round(float(lon), 6)],
        })

    from collections import Counter
    counts = Counter(s["chain"] for s in stores)
    print(f"  {len(elements)} supermarkets in the box → kept {len(stores)} chain "
          f"store(s), skipped {skipped} independent/unbranded"
          + (f", {no_coords} without coordinates" if no_coords else ""))
    print("  " + ", ".join(f"{c} ({n})" for c, n in counts.most_common()))

    if stores:
        GROCERY_CACHE_FILE.write_text(json.dumps(
            {"fetched_at": datetime.now(timezone.utc).isoformat(), "stores": stores},
            ensure_ascii=False, indent=1))
    return stores


def nearest_grocery(coords: tuple, stores: list[dict]) -> dict | None:
    """The closest chain store to a point, as {name, chain, distance_m}.

    Straight-line, not walking distance — a routed figure per area per store
    would be hundreds of requests for a number whose job is "is there one
    nearby, roughly". The dashboard labels it as a crow-flies distance.
    """
    best, best_km = None, None
    for store in stores:
        km = haversine_km(coords, tuple(store["coords"]))
        if best_km is None or km < best_km:
            best, best_km = store, km
    if best is None:
        return None
    return {"name": best["name"], "chain": best["chain"],
            "distance_m": int(round(best_km * 1000))}


# ── Commute calculations ─────────────────────────────────────────────────

def haversine_km(a: tuple, b: tuple) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, [*a, *b])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def straight_line_estimate(coords: tuple, target: tuple | None = None) -> dict:
    """Rough, no-API-needed estimate. Not a real route — just a sanity check."""
    km = haversine_km(coords, target or KTH_COORDS)
    return {
        "distance_km": round(km, 2),
        # crude rule of thumb for Stockholm: biking ~15km/h + 3 min overhead,
        # walking ~5km/h. Treat as ballpark only.
        "bike_min": round(km / 15 * 60 + 3),
        "walk_min": round(km / 5 * 60),
    }


_bike_cache = None
# Circuit breaker: if the router is down, stop after a few failures instead of
# waiting on ~180 timeouts. Reset per process, not per scrape.
_bike_failures = 0
_BIKE_GIVE_UP_AFTER = 3


def _load_bike_cache() -> dict:
    global _bike_cache
    if _bike_cache is None:
        _bike_cache = (json.loads(BIKE_ROUTE_CACHE_FILE.read_text())
                       if BIKE_ROUTE_CACHE_FILE.exists() else {})
    return _bike_cache


def _save_bike_cache():
    if _bike_cache is not None:
        BIKE_ROUTE_CACHE_FILE.write_text(json.dumps(_bike_cache, indent=2))


def bike_route(origin: tuple, target: tuple) -> dict | None:
    """Real cycling time + distance between two points, or None if unavailable.

    Cached to data/bike_route_cache.json keyed by rounded coordinates: the 26
    SSSB areas never move, so after the first run this is free. Delete that
    file to re-route from scratch.
    """
    global _bike_failures
    if _bike_failures >= _BIKE_GIVE_UP_AFTER:
        return None

    cache = _load_bike_cache()
    key = (f"{origin[0]:.5f},{origin[1]:.5f}>{target[0]:.5f},{target[1]:.5f}")
    if key in cache:
        return cache[key]

    try:
        resp = requests.post(
            BIKE_ROUTER_URL,
            json={
                "locations": [
                    {"lat": origin[0], "lon": origin[1]},
                    {"lat": target[0], "lon": target[1]},
                ],
                "costing": "bicycle",
                "directions_options": {"units": "kilometers"},
            },
            headers={"User-Agent": "sssb-kth-commute-tool/1.0 (personal use)"},
            timeout=20,
        )
        resp.raise_for_status()
        summary = resp.json()["trip"]["summary"]
        result = {
            "minutes": round(float(summary["time"]) / 60),
            "distance_km": round(float(summary["length"]), 2),
        }
    except (requests.RequestException, KeyError, TypeError, ValueError) as e:
        _bike_failures += 1
        print(f"  ! bike routing failed ({e}) — falling back to the straight-line estimate"
              + (f"; giving up on routing for this run after {_bike_failures} failures"
                 if _bike_failures >= _BIKE_GIVE_UP_AFTER else ""))
        return None

    cache[key] = result
    _save_bike_cache()
    time.sleep(0.4)  # be polite to a free community service
    return result


def commute_to_all_schools(coords: tuple, with_transit: bool = True,
                           with_bike_routes: bool = True) -> dict:
    """Distance/bike/walk (and transit, if a Resrobot key is set) from `coords`
    to every campus in SCHOOLS, keyed by short name.

    The dashboard's campus dropdown reads this, so switching schools re-filters
    and re-sorts against real numbers rather than re-using KTH's.

    NOTE ON API COST: the straight-line half is pure maths and free, but the
    transit half costs one Resrobot trip lookup per school per location — so
    setting RESROBOT_API_KEY multiplies request volume by len(SCHOOLS). Raise
    `--interval` if you start hitting Trafiklab's quota.
    """
    out = {}
    for sid, school in SCHOOLS.items():
        target = school["coords"]
        est = straight_line_estimate(coords, target)
        route = bike_route(coords, target) if with_bike_routes else None
        out[sid] = {
            "distance_km": est["distance_km"],       # straight line, always present
            "bike_min": route["minutes"] if route else est["bike_min"],
            "bike_km": route["distance_km"] if route else None,
            # "route" = real cycling directions; "estimate" = haversine + a
            # crude speed guess. Surfaced so the dashboard can avoid presenting
            # a guess as though it were measured.
            "bike_source": "route" if route else "estimate",
            "walk_min": est["walk_min"],
            "transit_min": real_transit_time(coords, target) if with_transit else None,
        }
    return out


_resrobot_stop_cache = {}


def _nearest_stop_id(coords: tuple) -> str | None:
    if coords in _resrobot_stop_cache:
        return _resrobot_stop_cache[coords]
    try:
        resp = requests.get(
            "https://api.resrobot.se/v2.1/location.nearbystops",
            params={
                "accessId": RESROBOT_API_KEY,
                "originCoordLat": coords[0],
                "originCoordLong": coords[1],
                "format": "json",
                "maxNo": 1,
            },
            timeout=10,
        )
        resp.raise_for_status()
        stops = resp.json().get("stopLocationOrCoordLocation", [])
        stop_id = stops[0]["StopLocation"]["extId"] if stops else None
        _resrobot_stop_cache[coords] = stop_id
        return stop_id
    except (requests.RequestException, KeyError, IndexError) as e:
        print(f"  ! resrobot nearbystops failed: {e}")
        return None


def real_transit_time(coords: tuple, target: tuple | None = None) -> int | None:
    """Real public-transit journey time (minutes) to `target` (default KTH)
    via Resrobot. Returns None if RESROBOT_API_KEY isn't set or the lookup
    fails — the dashboard falls back to the straight-line estimate then.
    """
    if not RESROBOT_API_KEY:
        return None
    origin_id = _nearest_stop_id(coords)
    dest_id = _nearest_stop_id(target or KTH_COORDS)
    if not origin_id or not dest_id:
        return None
    try:
        resp = requests.get(
            "https://api.resrobot.se/v2.1/trip",
            params={
                "accessId": RESROBOT_API_KEY,
                "originId": origin_id,
                "destId": dest_id,
                "format": "json",
                "numF": 1,
            },
            timeout=15,
        )
        resp.raise_for_status()
        trip = resp.json()["Trip"][0]
        origin_time = trip["Origin"]["time"]
        origin_date = trip["Origin"]["date"]
        dest_time = trip["Destination"]["time"]
        dest_date = trip["Destination"]["date"]
        fmt = "%Y-%m-%d %H:%M:%S"
        t0 = datetime.strptime(f"{origin_date} {origin_time}", fmt)
        t1 = datetime.strptime(f"{dest_date} {dest_time}", fmt)
        return round((t1 - t0).total_seconds() / 60)
    except (requests.RequestException, KeyError, IndexError) as e:
        print(f"  ! resrobot trip failed: {e}")
        return None


# ── Selenium scraping ────────────────────────────────────────────────────

def init_driver(headless: bool = True):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager

    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1400,1000")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=opts)


def _dismiss_cookie_banner(driver):
    """Best-effort dismissal of a cookie-consent overlay, which is the most
    common cause of 'element not interactable' on Swedish sites — it sits on
    top of the form and blocks clicks even though the form itself is fine.
    Safe no-op if nothing matches.
    """
    from selenium.webdriver.common.by import By

    candidates = [
        "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZÅÄÖ', 'abcdefghijklmnopqrstuvwxyzåäö'), 'godkänn')]",
        "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZÅÄÖ', 'abcdefghijklmnopqrstuvwxyzåäö'), 'acceptera')]",
        "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'accept')]",
        "//button[contains(., 'OK')]",
        "#onetrust-accept-btn-handler",
        ".cookie-consent button",
        "[id*='cookie'] button",
    ]
    for sel in candidates:
        try:
            by = By.XPATH if sel.startswith("//") else By.CSS_SELECTOR
            el = driver.find_element(by, sel)
            if el.is_displayed():
                el.click()
                time.sleep(0.5)
                return True
        except Exception:
            continue
    return False


def _click(driver, element):
    """Click, scrolling into view first and falling back to a JS click if
    Selenium's own interactability check fails (covered element, mid-animation,
    just-off-viewport, etc.) — all common and all harmless to work around.
    """
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    time.sleep(0.3)
    try:
        element.click()
    except Exception:
        driver.execute_script("arguments[0].click();", element)


def login(driver):
    """Log into minasidor.sssb.se. NOT called unless you pass --with-login.

    Ivar confirmed (2026-08-06) that the vacancy list renders fine in a
    logged-out private tab, queue days ("Ködagar") included, so scraping needs
    no credentials at all and this is skipped by default. It's kept only as an
    escape hatch in case SSSB puts the list back behind a login.

    CONFIGURABLE, AND STILL UNVERIFIED: the field selectors below are a best
    guess (SSSB commonly uses a personnummer + password form) and have never
    been checked against real markup — nobody has needed to run this path. If
    it fails, run with --debug --with-login, open debug_page.html, right-click
    the username/password fields → Inspect, and update the `By.CSS_SELECTOR`
    values below to match.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    username, password = get_credentials()

    driver.get(LOGIN_URL)
    wait = WebDriverWait(driver, 20)
    _dismiss_cookie_banner(driver)

    # Best-guess selectors — adjust if SSSB's form differs:
    username_field = wait.until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, "input[name='username'], input#username, input[type='text']"))
    )
    _click(driver, username_field)
    username_field.send_keys(username)

    password_field = driver.find_element(By.CSS_SELECTOR, "input[name='password'], input#password, input[type='password']")
    password_field.send_keys(password)

    submit_button = driver.find_element(By.CSS_SELECTOR, "button[type='submit'], input[type='submit']")
    _click(driver, submit_button)

    # Wait for the login form to disappear (i.e. we've navigated away from /login/)
    try:
        wait.until(lambda d: "/login" not in d.current_url)
    except Exception:
        raise SystemExit(
            "Still on the login page after submitting — either the credentials "
            "were rejected, or the submit button selector is wrong. Run with "
            "--debug (visible browser) to see which."
        )



def _click_next_or_load_more(driver) -> bool:
    """Best-effort click on whatever 'next page' / 'load more' control exists.
    Tries English and Swedish button text, plus common aria-labels. Returns
    True if something was clicked, False if no such control was found —
    treat False as "reached the end".
    """
    from selenium.webdriver.common.by import By

    UPPER_EN = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    LOWER_EN = "abcdefghijklmnopqrstuvwxyz"
    UPPER_SV = "ABCDEFGHIJKLMNOPQRSTUVWXYZÅÄÖ"
    LOWER_SV = "abcdefghijklmnopqrstuvwxyzåäö"

    phrases_sv = ["nästa", "visa fler", "fler bostäder", "ladda fler"]
    phrases_en = ["next", "load more", "show more", "more results"]

    xpaths = []
    for p in phrases_sv:
        xpaths.append(f"//button[contains(translate(normalize-space(.), '{UPPER_SV}', '{LOWER_SV}'), '{p}')]")
        xpaths.append(f"//a[contains(translate(normalize-space(.), '{UPPER_SV}', '{LOWER_SV}'), '{p}')]")
    for p in phrases_en:
        xpaths.append(f"//button[contains(translate(normalize-space(.), '{UPPER_EN}', '{LOWER_EN}'), '{p}')]")
        xpaths.append(f"//a[contains(translate(normalize-space(.), '{UPPER_EN}', '{LOWER_EN}'), '{p}')]")
    xpaths.append("//*[self::button or self::a][contains(@aria-label,'ext') or contains(@aria-label,'ästa')]")

    for xp in xpaths:
        try:
            for el in driver.find_elements(By.XPATH, xp):
                if el.is_displayed() and el.is_enabled():
                    _click(driver, el)
                    return True
        except Exception:
            continue
    return False


def _parse_listing_from_link(link, url: str) -> dict:
    """Given a `refid=` <a> tag and its resolved absolute URL, walk up the
    DOM to find the surrounding card text and pull out area/rent/size/queue-days.
    """
    import re

    card_text = ""
    node = link
    for _ in range(6):
        if node.parent is None:
            break
        node = node.parent
        candidate = node.get_text(" ", strip=True)
        if 15 <= len(candidate) <= 500 and (
            "kr" in candidate or re.search(r"\d\s*(day|days|dag|dagar)", candidate, re.IGNORECASE)
        ):
            card_text = candidate
            break
    if not card_text:
        card_text = node.get_text(" ", strip=True)[:500]

    area = "Unknown"
    lowered = card_text.lower()
    for needle, canonical in _AREA_NEEDLES:
        if needle.lower() in lowered:
            area = canonical
            break

    (housing_type, queue_days, rent_sek, size_sqm, floor,
     max_years, el_included) = _parse_card_fields(card_text)

    # Street address, e.g. "Forskarbacken 10" out of "Forskarbacken 10 / 1002".
    # The apartment number is dropped: it's no use for geocoding, and several
    # listings in the same building should share one cache entry.
    addr_match = _CARD_ADDRESS_RE.search(card_text)
    address = None
    if addr_match:
        address = f"{addr_match.group('street')} {addr_match.group('number')}"
        if addr_match.group("entrance"):
            address += f" {addr_match.group('entrance')}"   # "Armégatan 32 A"

    return {
        "id": url,
        "area": area,
        "address": address,
        "raw_text": card_text[:300],
        "type": housing_type,
        "queue_days": queue_days,
        "rent_sek": rent_sek,
        "size_sqm": size_sqm,
        "floor": floor,
        "max_years": max_years,      # contract cap in years; None = none stated
        "el_included": el_included,  # True = "Elström ingår"; None = not stated
        "url": url,
    }


# Confirmed live (2026-07-09) that a real card's text is a labeled table, not
# free-flowing prose — e.g.:
#   "Previous Next Rum i korridor Studentbacken 23 / 1313 10 mån hyra Elström
#    ingår Område: Boyta: Hyra: Inflyttning: Ködagar: Våning: Jerum 17 m²
#    4 968 kr 2026-08-01 91 (3st) 3 Previous Next"
# i.e. the labels (Område/Boyta/Hyra/Inflyttning/Ködagar/Våning) are listed
# first, then the values follow in the same order. The old approach (search
# for any "<number> kr" / "<number> dagar" anywhere in the text) silently
# returned None for queue_days here, because the value never actually sits
# next to the word "dagar" in this format — hence dashboard showing "--" for
# every listing. Parsing the label block's value run directly fixes that and
# is far less guessable-content-dependent than the old free text regexes.
_CARD_VALUES_RE = re.compile(
    r"(?P<size>\d{1,3})\s*m²\s*"
    r"(?P<rent>[\d\s]{3,7})\s*kr\s*"
    r"\d{4}-\d{2}-\d{2}\s*"  # move-in date — not currently surfaced
    r"(?P<queue>[\d\s]{1,6}?)\s*\(\d+\s*st\)"
)

# "Våning" (floor) is the last value in that same run, right after the queue
# figure's "(Nst)" token — either a number or "Bottenvåning" (ground floor).
# Confirmed against all 76 listings in a real scrape: values 1–11 plus
# "Bottenvåning". Anything else (e.g. the card's trailing "Previous") parses
# to None rather than a wrong number.
_CARD_FLOOR_RE = re.compile(r"\(\d+\s*st\)\s*(\S+)")

# Two optional badges SSSB puts in the card body, before the labeled table:
#   "Max 4 år"      — a cap on how long you may hold the contract. Confirmed
#                     on 18 of 76 listings (all "4"); absent on the rest,
#                     which means no cap is stated (i.e. the better case).
#   "Elström ingår" — electricity included in the rent. Confirmed on 48 of 76.
# Absence of either is "not stated", NOT a known negative — hence None rather
# than 0/False, so the dashboard can tell the difference.
_CARD_MAX_YEARS_RE = re.compile(r"Max\s+(\d+)\s*år", re.IGNORECASE)
_CARD_EL_RE = re.compile(r"Elström\s+ingår", re.IGNORECASE)


def _parse_floor(card_text: str) -> int | None:
    """Floor as an int, with ground floor = 0. None if it isn't stated."""
    m = _CARD_FLOOR_RE.search(card_text)
    if not m:
        return None
    raw = m.group(1).strip()
    if raw.isdigit():
        return int(raw)
    if raw.lower().startswith("botten"):  # "Bottenvåning" = ground floor
        return 0
    return None


def _parse_max_years(card_text: str) -> int | None:
    m = _CARD_MAX_YEARS_RE.search(card_text)
    return int(m.group(1)) if m else None


def _parse_el_included(card_text: str) -> bool | None:
    """True if the card says electricity is included; None if it says nothing
    (deliberately not False — we don't actually know it's excluded)."""
    return True if _CARD_EL_RE.search(card_text) else None

# The housing type ("Rum i korridor" = corridor/dorm room, "2 rum och kök" =
# 2-room + kitchen, etc.) is whatever text sits between "Previous Next" and
# the start of the street address (a word immediately followed by "<number>
# / <number>", e.g. "Studentbacken 23 / 1313").
_CARD_TYPE_RE = re.compile(r"^(?:Previous\s+Next\s+)?(.*?)\s+\S+\s+\d+\s*/\s*\d+\s")

# The same anchor, read the other way round: the street name and house number
# that precede the apartment number.
#
# Three shapes appear in real cards, and a single-token street only handles the
# first: "Forskarbacken 10 / 1002", "Körsbärsvägen 4 C / 1202" (letter suffix on
# the entrance), and "Gustav III:s Boulevard 2 / 1408" (multi-word street). So
# take the run of up-to-three capitalised words immediately before the number —
# the housing type that precedes it always ends in a lowercase word ("rum i
# korridor", "1 rum & kök"), which is what stops the run from swallowing it.
_CARD_ADDRESS_RE = re.compile(
    r"(?P<street>(?:[A-ZÅÄÖ][\wåäöÅÄÖ:.\-]*\s+){0,2}[A-ZÅÄÖ][\wåäöÅÄÖ:.\-]*)"
    r"\s+(?P<number>\d+)\s*(?P<entrance>[A-ZÅÄÖ])?\s*/\s*\d+"
)

_TYPE_TRANSLATIONS = {
    "rum i korridor": "Corridor room (dorm)",
    "korridorrum": "Corridor room (dorm)",
    "studentlägenhet": "Studio",
}


def _translate_housing_type(raw: str) -> str:
    key = raw.strip().lower()
    if key in _TYPE_TRANSLATIONS:
        return _TYPE_TRANSLATIONS[key]
    m = re.match(r"(\d+)\s*rum\s*och\s*(kök|kokvrå)", key)
    if m:
        n, kitchen_word = m.groups()
        return f"{n} room + {'kitchen' if kitchen_word == 'kök' else 'kitchenette'}"
    return raw.strip()


def _parse_card_fields(card_text: str):
    """Returns (housing_type, queue_days, rent_sek, size_sqm, floor,
    max_years, el_included), any of which may be None if the card text doesn't
    match the expected shape (falls back to the older, looser regexes so a
    format change degrades rather than silently returning nothing).
    """
    housing_type = None
    type_match = _CARD_TYPE_RE.match(card_text)
    if type_match and type_match.group(1).strip():
        housing_type = _translate_housing_type(type_match.group(1))

    floor = _parse_floor(card_text)
    extras = (_parse_max_years(card_text), _parse_el_included(card_text))

    values_match = _CARD_VALUES_RE.search(card_text)
    if values_match:
        size_sqm = int(values_match.group("size"))
        rent_sek = int(re.sub(r"\s", "", values_match.group("rent")))
        queue_days = int(re.sub(r"\s", "", values_match.group("queue")))
        return (housing_type, queue_days, rent_sek, size_sqm, floor, *extras)

    # Fallback: older free-text heuristics, in case SSSB's card layout has
    # drifted from the labeled-table format confirmed above.
    queue_match = re.search(r"(\d[\d\s]{0,6})\s*(day|days|dag|dagar)", card_text, re.IGNORECASE)
    queue_days = int(re.sub(r"\s", "", queue_match.group(1))) if queue_match else None

    rent_match = re.search(r"(\d[\d\s]{2,6})\s*kr", card_text)
    rent_sek = int(re.sub(r"\s", "", rent_match.group(1))) if rent_match else None

    size_match = re.search(r"(\d{1,3})\s*m²", card_text)
    size_sqm = int(size_match.group(1)) if size_match else None

    return (housing_type, queue_days, rent_sek, size_sqm, floor, *extras)



def _decode_response(resp) -> str:
    """Decode an HTTP response to text, preferring UTF-8 over requests' default.

    Necessary, not cosmetic: when a server sends `Content-Type: text/html` with
    no charset, requests falls back to ISO-8859-1 per the HTTP spec, which turns
    this page's Swedish into mojibake — "kök" → "kÃ¶k", "m²" → "mÂ²", and the
    non-breaking space inside "7 218 kr" into "Â ". That silently broke the
    card regexes and produced a rent of 218 kr instead of 7218. Confirmed
    against a fixture built from real scraped cards.
    """
    declared = "charset" in (resp.headers.get("content-type") or "").lower()
    if declared and resp.encoding:
        return resp.text
    for enc in ("utf-8", resp.apparent_encoding, "iso-8859-1"):
        if not enc:
            continue
        try:
            return resp.content.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return resp.content.decode("utf-8", "replace")


def _refid_links_from_html(html: str) -> dict:
    """Every real-listing link in a page of HTML, keyed by absolute URL.

    Shared by the Selenium path (which passes `driver.page_source`) and the
    browserless path (which passes the raw response body) so both get
    identical parsing.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for a in soup.select("a[href]"):
        href = a["href"]
        if "refid=" not in href:
            continue
        url = href if href.startswith("http") else "https://minasidor.sssb.se" + href
        out.setdefault(url, a)
    return out


def _expected_total_from_html(html: str) -> int | None:
    """SSSB shows "Shown X - Y of Z vacant homes" (Swedish: "Visas X - Y av Z
    lediga bostäder") — grab Z if we can, purely to report whether we got
    everything."""
    try:
        m = re.search(r"(?:of|av)\s+(\d+)\s+(?:vacant|lediga)", html, re.IGNORECASE)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def _listings_from_links(links_by_url: dict, expected_total: int | None) -> list[dict]:
    """Parse + report on a set of `refid=` links. Kept verbose on purpose —
    this project gets debugged from terminal output alone."""
    listing_links = list(links_by_url.items())
    print(f"  found {len(listing_links)} unique link(s) containing 'refid='")

    listings = [_parse_listing_from_link(link, url) for url, link in listing_links]
    unknown_area_count = sum(1 for l in listings if l["area"] == "Unknown")

    print(f"  parsed {len(listings)} listing(s):")
    for l in listings:
        print(f"    [{l['area']}] queue_days={l['queue_days']} rent={l['rent_sek']} "
              f"size={l['size_sqm']} floor={l['floor']} max_years={l['max_years']} "
              f"el={l['el_included']} :: {l['raw_text'][:90]}")

    if len(listing_links) == 0:
        print("  ! No 'refid=' links found at all — either 0 listings are published right "
              "now, or SSSB's link format changed. Run --debug and grep debug_page.html "
              "for 'refid=' to confirm which.")
    if expected_total and len(listing_links) < expected_total:
        print(f"  ! Only found {len(listing_links)} of an expected ~{expected_total}.")
    if unknown_area_count:
        print(f"  ! {unknown_area_count} listing(s) didn't match a known area name — the "
              "surrounding-text heuristic may be grabbing the wrong ancestor for those. Check "
              "the raw_text above.")

    missing_queue = sum(1 for l in listings if l["queue_days"] is None)
    if listings and missing_queue > len(listings) // 2:
        print(f"  ! {missing_queue} of {len(listings)} listing(s) have no queue-days figure. The "
              "'Ködagar' column was confirmed public in Aug 2026, so if this run wasn't already "
              "using --with-login, try that — SSSB may have moved it back behind a login (the "
              "dashboard sorts SSSB rows by queue days, so without it that ordering is meaningless).")

    return listings


# URL fragments worth reporting if the raw HTML turns out to be a JS shell —
# whatever endpoint the page fetches its data from is the thing to scrape next.
_ENDPOINT_HINT_RE = re.compile(
    r"[\"\x27(]([^\"\x27()\s]*(?:api|json|ajax|handler|\.asmx|/Lista/|sok|search)"
    r"[^\"\x27()\s]*)", re.IGNORECASE)


_reported_js_shell = False


def fetch_sssb_http(debug: bool = False) -> list[dict] | None:
    """Read the SSSB vacancy list with a plain HTTP GET — no browser at all.

    This is what makes the tool runnable somewhere Chrome doesn't exist (a
    phone, a small server). It only works if the listings are present in the
    raw HTML rather than being drawn in later by SSSB's JS; returns None if
    they aren't, so the caller can fall back to Selenium. On that failure it
    prints any API-ish URLs found in the page, since one of them is likely the
    endpoint the JS calls — which would be the better thing to scrape.
    """
    print("fetching SSSB vacancy list over plain HTTP (no browser)...")
    try:
        resp = requests.get(
            LISTINGS_URL,
            headers={
                # SSSB serves the list to logged-out visitors; a browser-ish UA
                # just avoids being treated as a bot.
                "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"),
                "Accept-Language": "sv,en;q=0.8",
            },
            timeout=30,
        )
        resp.raise_for_status()
        html = _decode_response(resp)
    except requests.RequestException as e:
        print(f"  ! HTTP fetch failed ({e})")
        return None

    print(f"  got {len(html):,} chars (HTTP {resp.status_code}, final URL {resp.url})")
    if debug:
        DEBUG_HTML_FILE.write_text(html, encoding="utf-8")
        print(f"  wrote raw HTML to {DEBUG_HTML_FILE} for inspection")

    if "/login" in resp.url:
        print("  ! redirected to a login page — the list isn't public after all; "
              "use --with-login (which needs Chrome).")
        return None

    links = _refid_links_from_html(html)
    if not links:
        global _reported_js_shell
        placeholders = html.count("{{")
        print(f"  ! no 'refid=' links in the raw HTML ({placeholders} '{{{{' template "
              "placeholder(s) found) — the page is drawn by JavaScript, so this "
              "browserless path can't read it; falling back to the browser.")
        # The candidate-endpoint dump is a wall of text and it's the same every
        # time, so print it once per process (and on --debug) rather than on
        # every background poll.
        if not _reported_js_shell or debug:
            _reported_js_shell = True
            candidates = sorted(set(_ENDPOINT_HINT_RE.findall(html)))[:25]
            if candidates:
                print("  Candidate data endpoints spotted in the page — one of these is probably "
                      "what its JS calls for the listings:")
                for c in candidates:
                    print(f"    {c}")
                print("  Targeting it directly would drop the browser requirement for good.")
        return None

    expected_total = _expected_total_from_html(html)
    if expected_total:
        print(f"  page reports ~{expected_total} vacant home(s) in total")
    return _listings_from_links(links, expected_total)


def scrape_listings(driver, debug: bool = False) -> list[dict]:
    """Scrape currently published listings.

    LISTINGS_URL already requests a 200-per-page size via
    `?pagination=1&paginationantal=200`, so normally everything renders in
    one go and the page-click loop below never has anything to click (it
    breaks immediately once `expected_total` is reached). It's kept as a
    fallback in case SSSB caps `paginationantal` below the real listing
    count some day.

    Real SSSB listings all link to a URL containing `refid=` in the query
    string (confirmed against an actual booking link), so rather than
    guessing CSS class names for a "card" wrapper — which kept matching
    unrelated page chrome — this anchors on that instead: find every
    `refid=` link, then walk a few levels up the DOM from each one to find
    the surrounding text (rent, size, queue days, area).
    """
    from selenium.webdriver.support.ui import WebDriverWait

    driver.get(LISTINGS_URL)

    # Wait until the Angular/Vue template placeholders have been replaced
    # with real numbers (the un-rendered page literally contains "{{alla}}").
    wait = WebDriverWait(driver, 25)
    try:
        wait.until(lambda d: "{{" not in d.page_source)
    except Exception:
        pass  # proceed anyway; page may just have 0 listings right now

    time.sleep(2)  # small buffer for any trailing async rendering

    expected_total = _expected_total_from_html(driver.page_source)

    all_links_by_url = {}
    for page_num in range(1, 26):  # hard cap so a broken "next" click can't loop forever
        page_links = _refid_links_from_html(driver.page_source)

        new_count = 0
        for url, a in page_links.items():
            if url not in all_links_by_url:
                all_links_by_url[url] = a
                new_count += 1

        print(f"  page {page_num}: {len(page_links)} link(s) visible, {new_count} new "
              f"(total so far: {len(all_links_by_url)}"
              + (f" of ~{expected_total}" if expected_total else "") + ")")

        if debug and page_num == 1:
            DEBUG_HTML_FILE.write_text(driver.page_source, encoding="utf-8")
            print(f"  wrote rendered HTML (page 1) to {DEBUG_HTML_FILE} for inspection")

        if expected_total and len(all_links_by_url) >= expected_total:
            break
        if new_count == 0 and page_num > 1:
            break  # clicking next/load-more stopped producing anything new

        if not _click_next_or_load_more(driver):
            break
        time.sleep(2)  # let new content render before the next pass

    return _listings_from_links(all_links_by_url, expected_total)



# ── Diff + notifications ─────────────────────────────────────────────────

def load_previous() -> dict:
    if CURRENT_FILE.exists():
        return json.loads(CURRENT_FILE.read_text())
    return {"listings": [], "generated_at": None}


def saved_data_age_minutes() -> float | None:
    """How old the saved listings are, or None if there aren't any / the
    timestamp is unreadable.

    `--serve` uses this to decide whether to scrape before serving. It used to
    check only whether the file existed, which meant a checkout carrying an old
    data file would serve those listings as current until the first background
    poll came round — up to `--interval` minutes of quietly showing stale
    listings with a timestamp that looked fine.
    """
    if not CURRENT_FILE.exists():
        return None
    try:
        generated_at = json.loads(CURRENT_FILE.read_text()).get("generated_at")
        saved = datetime.fromisoformat(generated_at)
    except (ValueError, TypeError, OSError):
        return None
    if saved.tzinfo is None:
        saved = saved.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - saved).total_seconds() / 60


def _notify_via_os(title: str, message: str) -> str | None:
    """Fire a desktop notification using tools the OS already ships. Returns
    None on success, or a short reason string on failure.

    Preferred over plyer because plyer's macOS backend needs `pyobjus`, a
    compiled extension that frequently won't install — confirmed on Ivar's Mac,
    where it raised ModuleNotFoundError and killed the notification. `osascript`
    is present on every macOS install and needs nothing.

    It returns a *reason* rather than False because when this path failed on
    Ivar's Mac all he saw was plyer's downstream `NotImplementedError`, which
    said nothing about why the osascript attempt before it didn't work. The
    usual cause is macOS notification permission for the terminal app —
    osascript reports that on stderr and exits non-zero, so surfacing its stderr
    turns a dead end into an actionable message.
    """
    import shutil
    import subprocess

    if sys.platform == "darwin":
        # json.dumps gives a correctly-escaped double-quoted AppleScript string.
        script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
        cmd = ["osascript", "-e", script]
    elif sys.platform.startswith("linux") and shutil.which("notify-send"):
        cmd = ["notify-send", title, message]
    else:
        return f"no notification command for platform {sys.platform!r}"
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=10)
        return None
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or b"").decode(errors="replace").strip().splitlines()
        return (f"{cmd[0]} exited {e.returncode}"
                + (f": {detail[0]}" if detail else "")
                + ("\n    On macOS this is usually notification permission: System "
                   "Settings → Notifications → your terminal app (or Script Editor)."
                   if sys.platform == "darwin" else ""))
    except FileNotFoundError:
        return f"{cmd[0]} not found on PATH"
    except Exception as e:
        return f"{cmd[0]} failed ({type(e).__name__}: {e})"


def _summarise_areas(listings: list[dict], limit: int = 5) -> str:
    """"Lappkärrsberget (16), Kungshamra (5), …" — a count per area rather than
    one entry per listing, which for a 63-listing first run was a wall of
    repeated names in the terminal."""
    from collections import Counter
    counts = Counter(l["area"] for l in listings)
    shown = ", ".join(f"{area} ({n})" for area, n in counts.most_common(limit))
    if len(counts) > limit:
        shown += f", +{len(counts) - limit} more area(s)"
    return shown


def notify_new(new_listings: list[dict]):
    if not new_listings:
        return
    # Not "SSSB:" — new_listings covers Bostadsförmedlingen ads too.
    title = f"Student housing: {len(new_listings)} new listing(s)"
    message = _summarise_areas(new_listings)

    os_reason = _notify_via_os(title, message)
    if os_reason is None:
        return
    try:  # plyer covers Windows, and anything the branch above doesn't
        from plyer import notification
        notification.notify(title=title, message=message, timeout=15)
    except Exception as e:
        # Both paths failed, so print the listings and both reasons. The
        # per-listing summary is the real point of the notification anyway; the
        # popup is just a nicety.
        print(f"  ! desktop notification unavailable — {len(new_listings)} new: {message}")
        print(f"    · OS notifier: {os_reason}")
        print(f"    · plyer: {type(e).__name__}: {e}")


# ── Main pipeline ────────────────────────────────────────────────────────

_scrape_lock = threading.Lock()


def _bf_field(ad: dict, *names, default=None):
    """Tolerant field getter — the AllaAnnonser JSON's exact key names have
    shifted over the years (community scrapers show several variants), so try
    each candidate name case-insensitively rather than hard-failing.
    """
    lower_map = {k.lower(): v for k, v in ad.items()}
    for n in names:
        if n.lower() in lower_map and lower_map[n.lower()] is not None:
            return lower_map[n.lower()]
    return default


# Per-ad link. **The feed publishes one itself, in `Url`** — confirmed from a
# real run's printed field list — so use that first and don't guess. Everything
# below it is fallback for if that field ever disappears.
#
# The route is bostad.stockholm.se/bostad/<n>/ — confirmed against a real
# working URL, .../bostad/202612197/ — but <n> is NOT `AnnonsId`. That field
# held a 6-digit number (299744) which 404s; the id in the URL is a
# year-prefixed 9-digit "annonsnummer". Since we don't know which key carries
# it, find it by shape: prefer likely names, then fall back to scanning every
# field for a value that looks like one. Anything unresolvable degrades to a
# search-page link zoomed on the ad rather than a dead detail page.
BF_SITE_ROOT = "https://bostad.stockholm.se"
BF_LISTING_URL_TEMPLATE = os.environ.get("BF_LISTING_URL",
                                         BF_SITE_ROOT + "/bostad/{id}/")
_BF_SEARCH_URL = BF_SITE_ROOT + "/bostad/"
_BF_AD_NUMBER_RE = re.compile(r"^20\d{7}$")   # e.g. 202612197
_BF_AD_NUMBER_KEYS = ("AnnonsNummer", "Annonsnummer", "AnnonsNr", "Annonsnr",
                      "AnnonsNummerVisning", "Nummer", "AnnonsId", "Id",
                      "BostadId", "ObjektId", "Referens")


def _bf_ad_number(ad: dict):
    """The number that appears in a /bostad/<n>/ URL, or None."""
    for key in _BF_AD_NUMBER_KEYS:
        value = _bf_field(ad, key)
        if value is not None and _BF_AD_NUMBER_RE.match(str(value).strip()):
            return str(value).strip()
    # Named guesses exhausted — any field holding a value of that shape will do.
    for value in ad.values():
        if isinstance(value, (str, int)) and _BF_AD_NUMBER_RE.match(str(value).strip()):
            return str(value).strip()
    return None


def _bf_feed_url(ad: dict) -> str | None:
    """The link the feed itself gives for this ad, absolutised, or None.

    Preferred over `_bf_ad_number()` because it needs no guessing: the
    shape-based search reported a 100% hit rate, but "found a 9-digit number
    starting with 20" is not the same claim as "found the ad's number", and
    nothing in that heuristic would notice if it started matching an unrelated
    field.
    """
    raw = _bf_field(ad, "Url", "url", "Lank", "Länk", "Link", "DetaljUrl")
    if not isinstance(raw, str) or not raw.strip():
        return None
    raw = raw.strip()
    if raw.startswith(("http://", "https://")):
        return raw
    return BF_SITE_ROOT + "/" + raw.lstrip("/")


def _bf_listing_url(ad: dict, coords) -> str:
    from_feed = _bf_feed_url(ad)
    if from_feed:
        return from_feed
    number = _bf_ad_number(ad)
    if number:
        return BF_LISTING_URL_TEMPLATE.format(id=number)
    if coords:
        lat, lon = coords
        # ~200m box, so their map opens on this address with the ad visible.
        return (f"{_BF_SEARCH_URL}?s={lat - 0.002:.5f}&n={lat + 0.002:.5f}"
                f"&w={lon - 0.004:.5f}&e={lon + 0.004:.5f}"
                "&sort=annonserad-fran-desc&student=1")
    return f"{_BF_SEARCH_URL}?student=1"


def _bf_truthy(value) -> bool:
    """Coerce a feed value to a boolean. Needed because a JSON "false" or "Nej"
    is a non-empty string, and so truthy in Python — which would have quietly
    let non-student ads through."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "0", "nej", "no", "none", "null")
    return bool(value)


def _bf_tristate(ad: dict, *names) -> bool | None:
    """A yes/no field from the feed, or None when the feed doesn't say.

    Same convention as `_parse_el_included()` on the SSSB side: absence has to
    stay distinguishable from a stated "no", because the dashboard only prints
    features a listing actually claims. Collapsing None to False here would
    silently turn "didn't mention a lift" into "has no lift".
    """
    for name in names:
        value = _bf_field(ad, name)
        if value is not None:
            return _bf_truthy(value)
    return None


def _bf_is_student(ad: dict) -> bool | None:
    """Is this ad student housing? None means the feed didn't say in any way we
    recognise — kept distinct from False so a wrong field name shows up as a
    loud diagnostic instead of silently filtering every ad away."""
    for key in ("Student", "student", "IsStudent", "StudentBostad",
                "Studentbostad", "ArStudentbostad", "StudentApartment"):
        value = _bf_field(ad, key)
        if value is not None:
            return _bf_truthy(value)
    # Some feeds carry a category/type string rather than a flag.
    category = _bf_field(ad, "Kategori", "Category", "Typ", "Type",
                         "BostadTyp", "Bostadstyp", default="")
    if isinstance(category, str) and category:
        return "student" in category.lower()
    return None


def fetch_bostadsformedlingen() -> list[dict]:
    """Fetch current STUDENT ads from Bostadsförmedlingen's public JSON feed.

    Every ad carries its own coordinates and landlord (hyresvärd) — e.g.
    Svenska Bostäder — so these get precise per-listing pins on the map
    rather than SSSB-style area dots, plus a provider tag.

    No credentials involved; plain GET. If the feed's shape changes, this
    prints the first ad's keys so the field mapping is fixable from terminal
    output alone.
    """
    print("fetching Bostadsförmedlingen ads (bostad.stockholm.se)...")
    ads = None
    for url in BF_ALL_ADS_URLS:
        try:
            resp = requests.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (personal student-housing monitor)",
                    "Accept": "application/json",
                    # The site's own page fetches this via XHR; some setups
                    # reject requests that don't look like they came from there.
                    "Referer": "https://bostad.stockholm.se/bostad/",
                    "X-Requested-With": "XMLHttpRequest",
                },
                timeout=25,
            )
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as e:
            reason = getattr(getattr(e, "response", None), "status_code", None) or type(e).__name__
            print(f"  · {url} → {reason}")
            continue
        # Some endpoints wrap the list in an envelope rather than returning it bare.
        if isinstance(payload, dict):
            for key in ("annonser", "results", "items", "data", "Annonser"):
                if isinstance(payload.get(key), list):
                    payload = payload[key]
                    break
        if isinstance(payload, list):
            print(f"  · {url} → OK")
            ads = payload
            break
        print(f"  · {url} → unexpected shape ({type(payload).__name__})")

    if ads is None:
        print("  ! No Bostadsförmedlingen endpoint worked — continuing with SSSB only.\n"
              "    Their old /Lista/AllaAnnonser feed is gone. To fix: open the search page on\n"
              "    bostad.stockholm.se, DevTools → Network → Fetch/XHR, find the request that\n"
              "    returns the ad list, and either set BF_ADS_URL=<that url> in your environment\n"
              "    or add it to BF_ALL_ADS_URLS at the top of this file.")
        return []

    if not isinstance(ads, list):
        print(f"  ! unexpected response shape ({type(ads).__name__}) — continuing with SSSB only")
        return []
    print(f"  feed contains {len(ads)} total ads")
    if ads:
        # Full list, not a truncated slice — a missing field can't be diagnosed
        # from the first 20 keys alphabetically.
        print(f"  first ad's fields: {sorted(ads[0].keys())}")

    # Student housing only — the rest of this feed is the general Stockholm
    # rental queue, which isn't what this tool is for.
    unknown_flag = 0
    links_from_feed = 0
    listings = []
    for ad in ads:
        is_student = _bf_is_student(ad)
        if is_student is None:
            unknown_flag += 1
            continue
        if not is_student:
            continue

        ad_id = _bf_field(ad, "AnnonsId", "annonsid", "Id")
        lat = _bf_field(ad, "KoordinatLatitud", "Latitud", "lat")
        lon = _bf_field(ad, "KoordinatLongitud", "Longitud", "lng", "lon")
        # There is no landlord/hyresvärd field in this feed — checked against a
        # real run's full key list. `KoNamn` is the name of the queue the ad
        # belongs to, which for the external queues (`Externko`) is the landlord
        # running it, and for BF's own stock is just "Bostadsförmedlingen" —
        # which the dashboard already treats as "no specific landlord". The
        # hyresvärd names stay tried first in case the field ever appears.
        landlord = _bf_field(ad, "Hyresvard", "Hyresvärd", "Uthyrare",
                             "KoNamn", "Konamn", default="")
        district = _bf_field(ad, "Stadsdel", "Omrade", "Område", default="") or ""
        kommun = _bf_field(ad, "Kommun", default="") or ""

        try:
            coords = [float(lat), float(lon)] if lat and lon else None
        except (TypeError, ValueError):
            coords = None

        if _bf_feed_url(ad):
            links_from_feed += 1

        listings.append({
            "id": f"bf-{ad_id}",
            "provider": "Bostadsförmedlingen",
            "landlord": landlord or None,
            "area": district or kommun or "Stockholm",
            "address": _bf_field(ad, "Gatuadress", "Adress", default=""),
            "raw_text": "",
            "queue_days": None,  # BF doesn't publish required queue time up front
            "rent_sek": _bf_field(ad, "Hyra", "Manadshyra"),
            "size_sqm": _bf_field(ad, "Yta", "Kvm"),
            "rooms": _bf_field(ad, "AntalRum", "Rum"),
            # `Lagenhetstyp` is this feed's equivalent of the SSSB card's type
            # line ("Korridorrum", "1 rum och kök"), so it renders in the same
            # slot on the row.
            "type": _bf_field(ad, "Lagenhetstyp", "Lägenhetstyp", "BostadTyp") or None,
            # `Vaning` is confirmed present, so the dashboard's floor sliders do
            # reach BF ads. None still means "not stated", which never hides one.
            "floor": _bf_field(ad, "Vaning", "Våning", "Floor", "Etage"),
            # Display only, never filtered — same rule as SSSB's `el_included`.
            # BF publishes a lift flag that SSSB's list page never states, so
            # filtering on it would hide every SSSB listing rather than narrow
            # anything. See "No elevator data exists" in the project notes.
            "elevator": _bf_tristate(ad, "Hiss", "hiss"),
            "balcony": _bf_tristate(ad, "Balkong", "balkong"),
            "deadline": _bf_field(ad, "AnnonseradTill", "SistaAnsokan", "AnmalanSenast"),
            "coords": coords,
            "url": _bf_listing_url(ad, coords),
        })

    with_coords = sum(1 for l in listings if l["coords"])
    print(f"  kept {len(listings)} student ad(s) of {len(ads)} total "
          f"({with_coords} with coordinates)")

    # If the feed never told us whether an ad is student housing, the field name
    # is wrong — say so instead of just reporting zero, which looks identical to
    # "there are no student ads right now".
    if unknown_flag:
        print(f"  ! {unknown_flag} ad(s) had no recognisable student flag, so they were "
              "skipped. If that's most of the feed, the field name has changed — "
              "add it to _bf_is_student(). Keys on the first ad:")
        if ads:
            print(f"    {sorted(ads[0].keys())}")
    if listings and with_coords == 0:
        print("  ! none had parseable coordinates — the lat/lon field names likely "
              "changed; check the printed field list above and update _bf_field calls.")

    # Per-ad links come from the feed's own `Url` where it has one, and from the
    # shape-matched ad number otherwise. Report the split rather than a single
    # "worked" count: a drop in the first number is the early warning that the
    # feed renamed that field and we're back to guessing.
    if listings:
        # Counted by which branch produced the link, not by whether it has a
        # query string — a feed-supplied Url is free to carry one.
        fallback = sum(1 for l in listings if l["url"].startswith(_BF_SEARCH_URL + "?"))
        guessed = len(listings) - links_from_feed - fallback
        print(f"  links: {links_from_feed} of {len(listings)} from the feed's own Url field"
              + (f", {guessed} from a shape-matched ad number" if guessed else "")
              + (f", {fallback} fell back to a search-page link" if fallback else ""))
        # These four come from fields the feed was only recently confirmed to
        # carry, so show how many ads actually filled them in.
        stated = {name: sum(1 for l in listings if l[name] is not None)
                  for name in ("type", "floor", "elevator", "balcony")}
        print("  stated by feed: "
              + ", ".join(f"{name} {n}/{len(listings)}" for name, n in stated.items()))
    for l in listings[:5]:
        print(f"    [{l['area']}] {l['address']} rent={l['rent_sek']} size={l['size_sqm']} "
              f"floor={l['floor']} lift={l['elevator']} queue={l['landlord']}")
    if len(listings) > 5:
        print(f"    ... and {len(listings) - 5} more")
    return listings


def run_scrape(debug: bool = False, use_login: bool = False,
               http_only: bool = False, bike_routes: bool = True,
               bf_bike_routes: bool = False) -> dict:
    with _scrape_lock:
        return _run_scrape_impl(debug=debug, use_login=use_login, http_only=http_only,
                                bike_routes=bike_routes, bf_bike_routes=bf_bike_routes)


def _run_scrape_impl(debug: bool = False, use_login: bool = False,
                     http_only: bool = False, bike_routes: bool = True,
                     bf_bike_routes: bool = False) -> dict:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] starting scrape...")
    previous = load_previous()
    previous_ids = {l["id"] for l in previous["listings"]}

    geocode_cache = _load_geocode_cache()
    grocery_stores = fetch_grocery_stores()
    print("geocoding areas (cached after first run)...")
    if bike_routes:
        print(f"working out cycling routes to {len(SCHOOLS)} campuses "
              "(real bike directions, cached to data/bike_route_cache.json — "
              "the first run is slow, later ones aren't)...")
    area_info = {}
    for group, names in AREAS.items():
        for name in names:
            coords = geocode_area(name, geocode_cache)
            per_school = (commute_to_all_schools(coords, with_bike_routes=bike_routes)
                          if coords else None)
            area_info[name] = {
                "group": group,
                "coords": coords,
                # Per-campus numbers drive the dashboard's campus dropdown.
                "per_school": per_school,
                # Kept at the top level too, pointing at the default campus, so
                # older saved files and any code reading the old shape still work.
                "straight_line": straight_line_estimate(coords) if coords else None,
                "transit_min": per_school[DEFAULT_SCHOOL]["transit_min"] if per_school else None,
                # One number beats 300 map dots for "can I buy food here" — the
                # dots are for browsing, this is for deciding.
                "nearest_grocery": (nearest_grocery(coords, grocery_stores)
                                    if coords and grocery_stores else None),
            }

    sssb_listings = None
    # Try the plain-HTTP path first: it's much faster than launching Chrome and
    # doesn't depend on a working driver. Falls through to Selenium if the raw
    # HTML turns out to be a JS shell.
    if not use_login:
        sssb_listings = fetch_sssb_http(debug=debug)
        if sssb_listings is None and http_only:
            raise SystemExit(
                "--http-only was requested but the vacancy list couldn't be read without a "
                "browser (see the diagnostics above). Drop --http-only to fall back to Selenium."
            )
    elif http_only:
        raise SystemExit("--http-only and --with-login are contradictory: logging in needs a browser.")

    if sssb_listings is None:
        print("falling back to the browser..." if not use_login
              else "launching browser + logging in...")
        driver = init_driver(headless=not debug)
        try:
            if use_login:
                login(driver)
            print("scraping listings...")
            sssb_listings = scrape_listings(driver, debug=debug)
        finally:
            driver.quit()

    for l in sssb_listings:
        l["provider"] = "SSSB"
        l["landlord"] = "SSSB"

    # Place each SSSB listing at its own building, so the map can break an area
    # roundel apart when you zoom into it. Distinct addresses only — a dozen rooms
    # in one building share a lookup — and cached forever, since buildings don't
    # move. Anything unresolved keeps `coords: None` and stays at the area centre.
    addr_cache = _load_address_cache()
    wanted = sorted({(l["address"], l["area"]) for l in sssb_listings if l.get("address")})
    fresh = [a for a, _ in wanted if a not in addr_cache]
    if fresh:
        print(f"geocoding {len(fresh)} new building address(es) "
              f"({len(wanted) - len(fresh)} already cached, ~1s each)...")
    for address, area in wanted:
        geocode_listing_address(address, area, addr_cache)
    for l in sssb_listings:
        l["coords"] = addr_cache.get(l.get("address")) if l.get("address") else None

    located = sum(1 for l in sssb_listings if l["coords"])
    print(f"  {located} of {len(sssb_listings)} SSSB listing(s) placed at their own "
          f"building ({len(wanted)} distinct address(es))")

    # How spread out each area's listings actually are. The dashboard uses this to
    # decide which roundels are worth dissolving into per-building dots: a single
    # block comes out near zero, a campus like Lappkärrsberget in the hundreds.
    for name, area in area_info.items():
        here = [l["coords"] for l in sssb_listings
                if l["area"] == name and l["coords"]]
        area["spread_m"] = area_spread_m(here)
        area["located_listings"] = len(here)
    spread_areas = {n: a["spread_m"] for n, a in area_info.items() if a["spread_m"] >= 120}
    if spread_areas:
        print("  spread out enough to split on the map: "
              + ", ".join(f"{n} ({m} m)" for n, m in
                          sorted(spread_areas.items(), key=lambda kv: -kv[1])))

    bf_listings = fetch_bostadsformedlingen()
    # Bostadsförmedlingen ads get straight-line estimates rather than routed
    # bike times by default. There are ~100 of them and they churn, so routing
    # them costs ~100 x len(SCHOOLS) requests — around 700, i.e. ~9 minutes of
    # paced requests — and most of that work is thrown away as ads rotate. The
    # 26 SSSB areas are a fixed set worth routing once; these aren't.
    # `--bike-routes-bf` opts in when you want the accuracy anyway.
    if bf_listings:
        print(f"working out commutes for {len(bf_listings)} Bostadsförmedlingen ad(s)"
              + (" with real cycling routes (slow)..." if bf_bike_routes
                 else " using straight-line estimates (--bike-routes-bf for real routes)..."))
    for l in bf_listings:
        if l["coords"]:
            l["per_school"] = commute_to_all_schools(
                tuple(l["coords"]), with_bike_routes=bike_routes and bf_bike_routes)
            l["straight_line"] = straight_line_estimate(tuple(l["coords"]))
            l["transit_min"] = l["per_school"][DEFAULT_SCHOOL]["transit_min"]

    listings = sssb_listings + bf_listings

    new_listings = [l for l in listings if l["id"] not in previous_ids]
    print(f"found {len(listings)} listings total — {len(sssb_listings)} SSSB, "
          f"{len(bf_listings)} Bostadsförmedlingen ({len(new_listings)} new)")

    routed = sum(1 for a in area_info.values()
                 if a["per_school"] and a["per_school"][DEFAULT_SCHOOL]["bike_source"] == "route")
    if bike_routes:
        print(f"bike times: {routed} of {len(area_info)} area(s) from real cycling routes, "
              f"{len(area_info) - routed} from the straight-line estimate")

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kth_coords": KTH_COORDS,
        # The dashboard builds its campus dropdown from this, so the two lists
        # can't drift apart.
        "schools": {sid: {"name": sc["name"], "coords": list(sc["coords"])}
                    for sid, sc in SCHOOLS.items()},
        "default_school": DEFAULT_SCHOOL,
        "areas": area_info,
        "listings": listings,
        # Chain supermarkets, for the map's optional shop dots. Sent as a flat
        # list rather than per-area because they're their own map layer.
        "groceries": grocery_stores,
        "new_listing_ids": [l["id"] for l in new_listings],
    }
    CURRENT_FILE.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    notify_new(new_listings)
    return result


# ── Local API + dashboard server ─────────────────────────────────────────

def _background_poll_loop(interval_minutes: float, use_login: bool = False,
                          http_only: bool = False, bike_routes: bool = True,
                          bf_bike_routes: bool = False):
    """Runs for the lifetime of `--serve`, re-scraping on its own so you
    don't have to sit there clicking Refresh. Any failure (SSSB hiccup,
    network blip) is logged and skipped rather than killing the loop.
    """
    while True:
        time.sleep(interval_minutes * 60)
        try:
            print(f"[{datetime.now().isoformat(timespec='seconds')}] auto-check...")
            run_scrape(use_login=use_login, http_only=http_only, bike_routes=bike_routes,
                       bf_bike_routes=bf_bike_routes)
        except SystemExit as e:
            print(f"  ! auto-check stopped early: {e}")
        except Exception as e:
            print(f"  ! auto-check failed, will retry next interval: {e}")


def _port_in_use(port: int = None) -> bool:
    """Is something already listening on our port?

    Worth checking before doing anything else, because double-clicking the
    launcher twice is an easy thing to do, and the alternative is a startup
    scrape followed by a Flask "Address already in use" traceback.
    """
    import socket
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port or PORT)) == 0


def _open_browser_when_ready(timeout: float = 20.0):
    """Open the dashboard once Flask is actually accepting connections.

    Waiting for the port rather than sleeping a fixed amount matters because the
    startup scrape runs first and takes about a minute with Chrome — opening the
    browser on a timer would land on "connection refused" and need a manual
    reload, which is exactly the friction this is meant to remove.
    """
    import socket
    import webbrowser

    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", PORT)) == 0:
                webbrowser.open(f"http://localhost:{PORT}")
                return
        time.sleep(0.25)
    # Not fatal: the URL is printed either way, so this is a convenience that
    # failed, not a broken run.
    print(f"  · couldn't open a browser automatically — open http://localhost:{PORT} yourself")


def serve(interval_minutes: float, use_login: bool = False,
          http_only: bool = False, bike_routes: bool = True,
          bf_bike_routes: bool = False, open_browser: bool = True):
    from flask import Flask, jsonify, send_from_directory
    from flask_cors import CORS

    app = Flask(__name__)
    CORS(app)  # local dev tool — fine to allow any origin

    static_dir = Path(__file__).parent

    @app.route("/")
    def index():
        return send_from_directory(static_dir, "index.html")

    @app.route("/api/listings")
    def api_listings():
        if CURRENT_FILE.exists():
            data = json.loads(CURRENT_FILE.read_text())
            data["poll_interval_min"] = interval_minutes
            return jsonify(data)
        return jsonify(run_scrape(use_login=use_login, http_only=http_only,
                                  bike_routes=bike_routes, bf_bike_routes=bf_bike_routes))

    @app.route("/api/status")
    def api_status():
        """Just the timestamp, so the dashboard can check whether anything
        changed without pulling the whole listing set (~100 KB) every minute.
        Data only changes once per --interval, so the vast majority of those
        polls used to transfer and re-render an identical payload.
        """
        generated_at = None
        if CURRENT_FILE.exists():
            try:
                generated_at = json.loads(CURRENT_FILE.read_text()).get("generated_at")
            except ValueError:
                pass
        return jsonify({"generated_at": generated_at,
                        "poll_interval_min": interval_minutes,
                        # Whether a scrape is running right now, so the dashboard
                        # can disable its Refresh button honestly. Client-side
                        # state alone isn't enough for two real cases: reloading
                        # the page mid-refresh, and the *background* auto-check —
                        # clicking Refresh during either just queues behind
                        # `_scrape_lock` for a minute with nothing to show for it.
                        "scraping": _scrape_lock.locked()})

    @app.route("/api/refresh", methods=["POST"])
    def api_refresh():
        return jsonify(run_scrape(use_login=use_login, http_only=http_only,
                                  bike_routes=bike_routes, bf_bike_routes=bf_bike_routes))

    threading.Thread(target=_background_poll_loop,
                     args=(interval_minutes, use_login, http_only, bike_routes, bf_bike_routes),
                     daemon=True).start()

    if open_browser:
        threading.Thread(target=_open_browser_when_ready, daemon=True).start()

    print(f"\nDashboard running → http://localhost:{PORT}")
    print(f"Auto-checking SSSB every {interval_minutes:g} min in the background (Ctrl+C to stop)\n")
    app.run(port=PORT, debug=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--login", action="store_true", help="prompt for SSSB credentials and store them in your OS keychain")
    parser.add_argument("--forget-login", action="store_true", help="remove saved credentials from your OS keychain")
    parser.add_argument("--once", action="store_true", help="scrape once, save, notify, exit")
    parser.add_argument("--serve", action="store_true",
                        help="start local dashboard + API server. This is what you want almost "
                             "always, so it's also what running with no arguments at all does")
    parser.add_argument("--no-browser", action="store_true",
                        help="don't open the dashboard in your browser when --serve starts")
    parser.add_argument("--interval", type=float, default=15, help="minutes between auto-checks in --serve mode (default: 15)")
    parser.add_argument("--bike-routes-bf", action="store_true",
                        help="also compute real cycling routes for Bostadsförmedlingen ads. Off by "
                             "default: there are ~100 of them and they rotate, so it costs roughly "
                             "700 paced requests (~9 min) per run for work largely discarded as ads "
                             "change. SSSB's 26 fixed areas are always routed")
    parser.add_argument("--no-bike-routes", action="store_true",
                        help="skip real cycling directions and use the old straight-line estimate. "
                             "Faster on a cold cache (the first run otherwise routes every area to "
                             "every campus), and a way out if the routing service is down")
    parser.add_argument("--http-only", action="store_true",
                        help="never launch a browser: read SSSB over plain HTTP and fail loudly if that "
                             "isn't possible, instead of quietly falling back to Selenium. Useful for "
                             "checking whether the fast path still works")
    parser.add_argument("--with-login", action="store_true",
                        help="log in before scraping. Not needed — the vacancy list, queue days included, is "
                             "public (confirmed 2026-08-06). Use this only if SSSB starts hiding listings or "
                             "the Ködagar column behind a login again")
    parser.add_argument("--no-login", action="store_true",
                        help="(now the default; accepted for compatibility and does nothing)")
    parser.add_argument("--debug", action="store_true", help="run visible browser + dump debug_page.html")
    args = parser.parse_args()

    if args.forget_login:
        forget_credentials()
    elif args.login:
        _prompt_and_store()
        print("Done — future runs will use this automatically.")
    elif args.once:
        run_scrape(debug=args.debug, use_login=args.with_login,
                   http_only=args.http_only, bike_routes=not args.no_bike_routes,
                   bf_bike_routes=args.bike_routes_bf)
    # Anything else — `--serve`, or no arguments at all — serves. Bare
    # `python monitor.py` used to print help and exit 1, which made the
    # one mode you want every day the one you had to remember a flag for.
    else:
        if args.interval < 5:
            parser.error("--interval below 5 minutes isn't a great idea — see README on rate limiting.")
        # Already running? Point at it rather than scraping for a minute and then
        # dying on "Address already in use" — double-clicking the launcher twice
        # should land you on the dashboard, not a traceback.
        if _port_in_use():
            print(f"Already running → http://localhost:{PORT}")
            print("(if that isn't this tool, something else has port "
                  f"{PORT}; stop it and try again)")
            if not args.no_browser:
                import webbrowser
                webbrowser.open(f"http://localhost:{PORT}")
            sys.exit(0)
        # Scrape before serving unless the saved listings are still fresh, so a
        # checkout that came with an old data file can't be presented as current.
        age = saved_data_age_minutes()
        if age is None:
            print("no saved listings yet — scraping before serving...")
        elif age > args.interval:
            print(f"saved listings are {age:.0f} min old (older than the "
                  f"{args.interval:g} min interval) — scraping before serving...")
        else:
            print(f"serving saved listings from {age:.0f} min ago; "
                  f"next auto-check in under {args.interval:g} min")
        if age is None or age > args.interval:
            # A failed startup scrape must not stop the dashboard from coming up.
            # It used to abort the process with a raw traceback, so a brief SSSB
            # outage or a missing Chrome meant no dashboard at all — even with
            # perfectly good saved listings on disk. Same policy as the
            # background auto-check, which has always logged and carried on.
            # SystemExit is deliberately not caught: that's --http-only's
            # explicit "fail loudly" contract.
            try:
                run_scrape(debug=args.debug, use_login=args.with_login,
                           http_only=args.http_only, bike_routes=not args.no_bike_routes,
                           bf_bike_routes=args.bike_routes_bf)
            except Exception as e:
                print(f"\n  ! the startup scrape failed ({type(e).__name__}: {e})")
                if age is None:
                    print("    No saved listings either, so the dashboard will start empty.")
                else:
                    print(f"    Serving the saved listings from {age:.0f} min ago instead —"
                          " they'll say so in the top right.")
                print(f"    The background auto-check will try again in "
                      f"{args.interval:g} min, or hit \"Refresh\" in the dashboard.\n")
        serve(interval_minutes=args.interval, use_login=args.with_login,
              http_only=args.http_only, bike_routes=not args.no_bike_routes,
              bf_bike_routes=args.bike_routes_bf, open_browser=not args.no_browser)
