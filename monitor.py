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
from urllib.parse import urlsplit

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

# Sent on every outbound request. The contact URL is the point of it: a landlord
# or a free API operator who doesn't want this traffic should be able to find the
# person responsible and say so, rather than having to silently block an
# anonymous scraper. It also satisfies Nominatim's usage policy, which asks for
# an identifiable application with a way to make contact.
PROJECT_URL = "https://github.com/ivarhak/Stockholm-Student-Housing-Finder"
CONTACT_URL = PROJECT_URL + "/issues"
USER_AGENT = (f"Stockholm-Student-Housing-Finder/1.0 (personal, non-commercial; "
              f"contact: {CONTACT_URL})")

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
# Listings are stored per city — see listings_file(). CURRENT_FILE is the old
# single-city name, kept only so _legacy_listings_file() can read it once on the
# way past; nothing writes to it any more.
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


def geocode_area(name: str, cache: dict, addresses: dict | None = None,
                 city_name: str = "Stockholm") -> tuple | None:
    """Look up (lat, lon) for a housing area, cached to disk.

    `addresses` is the city's own name→street-address table. Stockholm has 26
    hand-verified ones because SSSB publishes no coordinates and bare area names
    resolve badly ("Balder" landed 30 km away). A city without such a table falls
    back to the area name plus the city, which is what the cache is for: you can
    hand-correct any entry by editing data/geocode_cache.json directly.
    """
    if name in cache and cache[name]:
        return tuple(cache[name])

    query = (addresses or AREA_ADDRESSES).get(name, f"{name}, {city_name}, Sweden")
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1},
            headers={"User-Agent": USER_AGENT},
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
        # NOT cached. This is the distinction geocode_listing_address() already
        # makes and this function did not: "Nominatim answered, and there is no
        # such place" is a fact worth remembering, but "we couldn't reach
        # Nominatim" is a fact about the network this minute. Caching the second
        # as the first writes `null` for every campus of a new city on one bad
        # minute, and the file is committed and restored from the Actions cache
        # — so a transient outage became a permanent one. With no campus
        # coordinates every commute is unmeasurable, the commute filter hides
        # every listing, and the city renders as empty forever after.
        # Reproduced exactly that way while testing offline: seven
        # `"campus:*": null` entries written from seven proxy errors.
        print(f"  ! geocoding failed for {name}: {e}")
        return None

    # Nominatim answered and had nothing. That one is worth not asking again.
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

# How far a building may sit from its own area's centre before we stop believing
# the geocoder. Every SSSB area is a campus-sized cluster — the widest,
# Lappkärrsberget, is about a kilometre across — so 3 km is generous for a real
# address and nowhere near a same-name street in another municipality.
MAX_ADDRESS_OFFSET_M = 3000

# How long a listing counts as NEW after we first saw it.
#
# This used to be "not present in the previous run's file", which sounds
# equivalent and isn't. That definition makes *everything* new whenever the
# history is missing — a fresh Actions runner, a fresh clone, a cache miss, a
# renamed state file — so the badge fired on all ~170 rows at once and carried no
# information at all. It also expired after exactly one scrape cycle, so a
# listing that appeared 20 minutes after you last looked was already unbadged.
#
# A timestamp fixes both. `first_seen` is carried forward across runs, so the
# badge means "appeared in the last day" no matter how many times we scraped in
# between, and losing the history can no longer invent 170 new listings.
NEW_WINDOW_HOURS = 24


def _load_address_cache() -> dict:
    if ADDRESS_CACHE_FILE.exists():
        try:
            return json.loads(ADDRESS_CACHE_FILE.read_text())
        except ValueError:
            print("  ! address cache unreadable — starting a fresh one")
    return {}


def _save_address_cache(cache: dict):
    ADDRESS_CACHE_FILE.write_text(json.dumps(cache, indent=1, ensure_ascii=False))


def geocode_listing_address(address: str, area: str, cache: dict,
                            hint: str | None = None,
                            addresses: dict | None = None,
                            city_name: str = "Stockholm") -> list | None:
    """(lat, lon) for one street address, cached to disk. None if unresolvable.

    Queried with the area's own postcode/city tail where we have one, because
    "Forskarbacken 10, Sweden" is ambiguous nationally while
    "Forskarbacken 10, 114 17 Stockholm, Sweden" is not. Hand-correct
    data/address_cache.json if a dot ever lands somewhere silly, same as the
    area cache.
    """
    if address in cache:
        return cache[address]

    # A feed that states its own postcode and town (AF Bostäder does) is already
    # unambiguous, so use it verbatim rather than guessing a tail.
    if hint:
        query = hint
    else:
        # Otherwise reuse the area's verified city/postcode tail so the search is
        # anchored to the right municipality — several Stockholm areas aren't in
        # Stockholm proper (Solna, Täby, Nacka, Huddinge).
        area_query = (addresses or AREA_ADDRESSES).get(area, "")
        tail = (", ".join(area_query.split(", ")[1:]) if ", " in area_query
                else f"{city_name}, Sweden")
        query = f"{address}, {tail}"
    coords = None
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1},
            headers={"User-Agent": USER_AGENT},
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


def drop_misplaced_addresses(listings: list, area_info: dict) -> dict:
    """Blank out any listing whose geocoded building sits implausibly far from its
    own area, and return {address: (area, metres_off)} for the ones dropped.

    Mutates `listings` in place, setting `coords` back to None — which is the
    "we don't know the building" value, so the listing falls back to the area
    centre exactly as it did before per-building dots existed.
    """
    misplaced = {}
    for l in listings:
        centre = (area_info.get(l.get("area")) or {}).get("coords")
        if not (l.get("coords") and centre):
            continue
        off_m = haversine_km(tuple(l["coords"]), tuple(centre)) * 1000
        if off_m > MAX_ADDRESS_OFFSET_M:
            misplaced[l.get("address")] = (l.get("area"), off_m)
            l["coords"] = None
    return misplaced


def _seen_recently(first_seen, now, hours: float = NEW_WINDOW_HOURS) -> bool:
    """Was this first seen within the window? None means "already there when we
    started looking" — deliberately not new, so a cold start badges nothing."""
    if not first_seen:
        return False
    try:
        seen = datetime.fromisoformat(first_seen)
    except (TypeError, ValueError):
        return False
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return (now - seen).total_seconds() < hours * 3600


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

# Several public Overpass endpoints, tried in order. The main one is heavily
# loaded and routinely answers 429/504 at busy times, which is the difference
# between the map having shop dots and not — so don't depend on a single mirror.
# Same approach as BF_ALL_ADS_URLS. OVERPASS_URL overrides and is tried first.
#
# kumi.systems is first deliberately, not overpass-api.de. It runs a mirror
# specifically so that automated callers stop hammering the main instance, and
# that instance is the one whose rate limiting we keep losing to. Sparing it is
# both politer and likelier to work.
OVERPASS_URLS = [u for u in (
    os.environ.get("OVERPASS_URL"),
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
) if u]
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


# ── Cities ──────────────────────────────────────────────────────────────
#
# One entry per city the dashboard can show. It lives down here, below the
# grocery constants, purely because it references GROCERY_BBOX — conceptually it
# belongs with the city constants at the top of the file.
#
# Stockholm's entry points at the existing constants rather than copying them,
# so introducing the registry changed nothing about how Stockholm is scraped.
# A second city brings its own areas, schools and providers and adds an entry;
# it does not edit Stockholm's.
#
# Each city carries its own slider defaults, because the three cities are
# genuinely different shapes: a 45-minute commute limit is a real filter in
# Stockholm, where the areas span 30 km, and would hide precisely nothing in
# Lund, where the whole city is a fifteen-minute bike ride.
CITIES = {
    "stockholm": {
        "name": "Stockholm",
        "schools": SCHOOLS,
        "default_school": DEFAULT_SCHOOL,
        "areas": AREAS,
        "area_addresses": AREA_ADDRESSES,
        "area_aliases": AREA_ALIASES,
        "providers": ["SSSB", "Bostadsförmedlingen"],
        "fetchers": ["sssb", "bostadsformedlingen"],
        # What this city's data does and doesn't cover, shown on the page. Every
        # city needs one: none of them is the whole market, and a dashboard that
        # doesn't say so reads as "there is nothing else", which is worse than
        # saying "there is more, but not here". Lund's will have to name LKF,
        # whose student stock has no findable public vacancy list (checked
        # 2026-08-09), and Göteborg's that SGS publishes no queue figure.
        "coverage_note": "Covers SSSB and Bostadsförmedlingen (which is also where "
                         "Svenska Bostäder's student flats are advertised). Other "
                         "Stockholm landlords aren't included.",
        # Links rendered alongside the note. Kept as structured data rather than
        # HTML in the string, so nothing from a data file is ever injected as
        # markup — the dashboard builds real anchor elements from these.
        "coverage_links": [],
        # What a group of areas is called here. Stockholm's areas are grouped by
        # tunnelbana line, so the dashboard says "SSSB lines" and "North line";
        # a city without a metro would say "areas" and read just as naturally.
        "area_group_noun": "line",
        # Draw the tunnelbana under the roundels. Only Stockholm has one, and
        # the dashboard's METRO_LINES are its real station coordinates — drawn
        # over another city they are simply wrong, so this is opt-in per city.
        "transit_overlay": "stockholm-metro",
        # Where a visitor should actually apply. Per city, because a Lund
        # listing pointing at sssb.se would be worse than pointing nowhere.
        "sources": [{"label": "sssb.se", "url": "https://sssb.se"},
                    {"label": "bostad.stockholm.se", "url": "https://bostad.stockholm.se"}],
        "grocery_bbox": GROCERY_BBOX,
        # What the dashboard's max-commute slider opens at.
        "max_commute_default": 45,
    },
    "goteborg": {
        "name": "Göteborg",
        # Live since 2026-08-18, after its pins were checked on a map.
        "enabled": True,
        # Campus ADDRESSES, not coordinates — resolved once through Nominatim and
        # cached, with data/geocode_cache.json as the hand-correct escape hatch.
        # These addresses are written from knowledge and have NOT been checked
        # against a map; the first run's pins are worth eyeballing.
        "schools": {
            # Chalmers' two campuses are separate entries on purpose: they sit
            # across the river from each other and are a real commute apart, so
            # "which campus" genuinely changes the answer.
            "Chalmers":   {"name": "Chalmers tekniska högskola (Johanneberg)",
                           "address": "Chalmersplatsen 4, 412 58 Göteborg, Sweden"},
            "Chalmers-L": {"name": "Chalmers Lindholmen",
                           "address": "Lindholmsplatsen 1, 417 56 Göteborg, Sweden"},
            "GU":         {"name": "Göteborgs universitet (Näckrosen)",
                           "address": "Renströmsgatan 6, 412 55 Göteborg, Sweden"},
            "Handels":    {"name": "Handelshögskolan vid Göteborgs universitet",
                           "address": "Vasagatan 1, 411 24 Göteborg, Sweden"},
            "Sahlgrenska":{"name": "Sahlgrenska akademin",
                           "address": "Medicinaregatan 3, 413 90 Göteborg, Sweden"},
            "HDK-Valand": {"name": "HDK-Valand (konst och design)",
                           "address": "Vasagatan 50, 411 37 Göteborg, Sweden"},
            "HSM":        {"name": "Högskolan för scen och musik",
                           "address": "Fågelsången 1, 412 56 Göteborg, Sweden"},
        },
        "default_school": "Chalmers",
        # No `areas` / `area_addresses`: SGS publishes the area on every listing,
        # so the list builds itself and a new SGS area appears on the map without
        # anyone editing this file.
        "providers": ["SGS"],
        "fetchers": ["sgs"],
        "coverage_note": ("Covers SGS Studentbostad, Göteborg's biggest student "
                          "landlord. SGS publishes no queue figure, so listings show "
                          "when the contract starts rather than a queue length — you "
                          "apply and the longest queue wins. Studentbostad Express "
                          "couldn't be read automatically, and Chalmers "
                          "Studentbostäder and Boplats aren't included yet:"),
        "coverage_links": [
            {"label": "SGS Studentbostad Express",
             "url": "https://minasidor.sgs.se/market/VqcHjmtPBDFwFFTVb4fydPxw"},
            {"label": "Chalmers Studentbostäder",
             "url": "https://www.chalmersstudentbostader.se/sok-ledigt/"},
            {"label": "Boplats Göteborg",
             "url": "https://boplats.se/sok?types=1hand&objecttype=student"},
        ],
        "area_group_noun": "area",
        "sources": [{"label": "sgs.se", "url": "https://sgs.se"}],
        "grocery_bbox": (57.60, 11.75, 57.80, 12.15),
        "max_commute_default": 30,
    },
    "lund": {
        "name": "Lund",
        "enabled": True,    # live since 2026-08-18, pins checked
        "schools": {
            "LU":   {"name": "Lunds universitet",
                     "address": "Paradisgatan 2, 223 50 Lund, Sweden"},
            "LTH":  {"name": "Lunds tekniska högskola",
                     "address": "John Ericssons väg 3, 223 63 Lund, Sweden"},
            "SLU":  {"name": "SLU Alnarp",
                     "address": "Slottsvägen 5, 234 56 Alnarp, Sweden"},
            # These two are Lund University faculties that are physically in
            # MALMÖ. Listing them with a Lund-shaped commute would be actively
            # wrong, so they get their real addresses and the numbers are allowed
            # to say forty minutes.
            "MHM":  {"name": "Musikhögskolan i Malmö (Lunds universitet)",
                     "address": "Ystadvägen 25, 214 45 Malmö, Sweden"},
            "KHM":  {"name": "Konsthögskolan i Malmö (Lunds universitet)",
                     "address": "Ystadvägen 18, 214 45 Malmö, Sweden"},
        },
        "default_school": "LU",
        # AF Bostäder states a full street address and postcode on every listing,
        # so areas and their centres both come out of the feed.
        "providers": ["AF Bostäder"],
        "fetchers": ["afbostader"],
        "coverage_note": ("Covers AF Bostäder, which runs about 6,000 of Lund's "
                          "student rooms. They publish no per-listing queue length, "
                          "so rows show how many people have already applied "
                          "instead. LKF, the municipal landlord, has no public "
                          "vacancy list we could find — check them separately:"),
        "coverage_links": [
            {"label": "LKF", "url": "https://www.lkf.se/"},
            {"label": "Boplats Syd", "url": "https://www.boplatssyd.se/"},
        ],
        "area_group_noun": "area",
        "sources": [{"label": "afbostader.se", "url": "https://www.afbostader.se/lediga-bostader/"}],
        "grocery_bbox": (55.63, 13.05, 55.78, 13.30),
        # Lund is small enough that essentially everything is a short bike ride,
        # so a 45-minute cap would filter nothing at all.
        "max_commute_default": 20,
    },
}
DEFAULT_CITY = "stockholm"


def resolve_schools(conf: dict, cache: dict) -> dict:
    """Fill in coordinates for any campus given only a street address.

    Stockholm's seven are hand-verified literals, which was fine for one city and
    doesn't scale to three. New cities give each campus an address and it is
    resolved through the same Nominatim + geocode_cache.json path the areas use —
    including the hand-correct escape hatch, which is how a silly pin gets fixed
    here. A campus that won't resolve is dropped with a warning rather than
    silently sitting at (0, 0) off the coast of Africa.
    """
    out = {}
    for sid, school in conf["schools"].items():
        coords = school.get("coords")
        if not coords and school.get("address"):
            key = f"campus:{sid}"
            coords = cache.get(key) or geocode_area(
                key, cache, addresses={key: school["address"]}, city_name=conf["name"])
        if not coords:
            print(f"  ! campus {sid} has no coordinates and its address wouldn't "
                  f"resolve — leaving it out of this run")
            continue
        out[sid] = {"name": school["name"], "coords": tuple(coords)}
    return out


def city_conf(city: str) -> dict:
    """A city's registry entry, or a clear error naming the ones that exist."""
    try:
        return CITIES[city]
    except KeyError:
        raise SystemExit(
            f"unknown city {city!r} — known cities: {', '.join(sorted(CITIES))}")


def listings_file(city: str) -> Path:
    return DATA_DIR / f"current_listings_{city}.json"


def _legacy_listings_file() -> Path:
    """The single-city filename, from before the registry existed.

    Read once as a fallback so the first run after this change doesn't find an
    empty history and tag all ~170 listings as NEW — which is exactly what the
    Actions cache exists to prevent. Never written to.
    """
    return DATA_DIR / "current_listings.json"


def write_cities_index():
    """data/cities.json — what the dashboard's city picker reads.

    Built by looking at the city files actually on disk rather than from CITIES
    alone, so a city that has never scraped successfully doesn't appear as an
    empty option. Small on purpose: the picker needs a name and a count, not a
    payload.
    """
    index = {}
    for cid, conf in CITIES.items():
        if not conf.get("enabled", True):
            continue
        path = listings_file(cid)
        if not path.exists() and cid == DEFAULT_CITY:
            path = _legacy_listings_file()
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
        except (ValueError, OSError):
            continue
        index[cid] = {
            "name": conf["name"],
            "providers": conf["providers"],
            "listings": len(data.get("listings") or []),
            "generated_at": data.get("generated_at"),
        }
    (DATA_DIR / "cities.json").write_text(json.dumps(
        {"default": DEFAULT_CITY, "cities": index}, indent=2, ensure_ascii=False))
    return index


def _host(url: str) -> str:
    """Just the hostname, for a summary line that has to stay one line."""
    return urlsplit(url).netloc or url


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


def grocery_cache_file(city: str) -> Path:
    """Stockholm keeps the original filename on purpose: `data/grocery_cache.json`
    is committed (see the .gitignore comment on it) — a hand-gathered 383-store
    list that's the whole reason an Overpass-unreliable Actions runner still
    ships shop dots. Renaming it would orphan that safety net for no reason.

    Every other city gets its own file. Sharing one file across every city was a
    real bug, not a simplification: `_load_grocery_cache()` had no city in it at
    all, so once Stockholm's own fetch had populated the cache, Göteborg and
    Lund's calls to fetch_grocery_stores() — each with their own correct bbox
    passed in — hit the freshness check first and got Stockholm's 383 stores
    handed back unchanged. The bbox argument was being silently ignored. Visibly
    it looked fine (`shops: 383` printed for every city, every run) because
    nothing compared that count against the city it was supposedly for — the
    dots were just never on screen, hundreds of kilometres from wherever the map
    was actually centred.
    """
    return GROCERY_CACHE_FILE if city == DEFAULT_CITY else DATA_DIR / f"grocery_cache_{city}.json"


def _load_grocery_cache(cache_file: Path) -> tuple[list | None, bool]:
    """(stores, is_fresh). `stores` is None only if there's no readable cache.

    Freshness and usability are deliberately separate answers. A stale cache
    still needs refetching, but it is *far* better than nothing if that refetch
    fails — supermarkets don't move, so last month's list is still basically
    right, while an empty list silently removes the feature from the map.
    """
    if not cache_file.exists():
        return None, False
    try:
        cached = json.loads(cache_file.read_text())
        stores = cached["stores"]
        fetched = datetime.fromisoformat(cached["fetched_at"])
    except (ValueError, KeyError, TypeError, OSError):
        return None, False
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - fetched).total_seconds() / 86400
    if age_days > GROCERY_MAX_AGE_DAYS:
        print(f"  grocery cache is {age_days:.0f} days old — refetching "
              f"(keeping the {len(stores)} cached one(s) in case that fails)")
        return stores, False
    print(f"  {len(cached['stores'])} grocery store(s) from cache "
          f"({age_days:.0f} days old)")
    return stores, True


def fetch_grocery_stores(bbox: tuple | None = None, city: str = DEFAULT_CITY
                          ) -> tuple[list[dict], list[str]]:
    """Chain supermarkets in the given city's bbox, as ([{name, chain, coords}], notes).

    Never fatal: the map is perfectly usable without shop dots, so any failure
    degrades to an empty list with a printed reason rather than stopping a scrape.

    `notes` is a one-line-per-endpoint account of how the list was obtained, so
    the caller can repeat it in the run summary. That exists because this runs at
    the very start of a scrape: in an Actions log the reason ends up ~600 lines
    above the end, and a run that shipped no shops read as a normal green build.
    """
    cache_file = grocery_cache_file(city)
    cached, fresh = _load_grocery_cache(cache_file)
    if fresh:
        return cached, [f"{len(cached)} from the on-disk cache"]
    notes = []

    south, west, north, east = bbox or GROCERY_BBOX
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
    elements = None
    for url in OVERPASS_URLS:
        try:
            resp = requests.post(
                url,
                data={"data": query},
                headers={"User-Agent": USER_AGENT},
                timeout=120,
            )
            resp.raise_for_status()
            elements = resp.json().get("elements", [])
            print(f"  · {url} → OK ({len(elements)} element(s))")
            notes.append(f"{_host(url)} → OK")
            break
        except (requests.RequestException, ValueError) as e:
            short, detail = _why(e)
            print(f"  · {url} → {detail}")
            notes.append(f"{_host(url)} → {short}")

    if elements is None:
        # Every mirror refused. Routine enough at busy times that it must not cost
        # the map its shop dots for the next two hours.
        print("  ! no Overpass endpoint answered")
        if cached:
            print(f"    falling back to {len(cached)} store(s) from the stale cache")
            return cached, notes + [f"kept {len(cached)} from the stale cache"]
        print("    and no cache to fall back on — the map's Shops toggle will be\n"
              "    hidden this run. Try again later, or set OVERPASS_URL=<mirror>.")
        return [], notes + ["no cache to fall back on"]

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
        cache_file.write_text(json.dumps(
            {"fetched_at": datetime.now(timezone.utc).isoformat(), "stores": stores},
            ensure_ascii=False, indent=1))
        return stores, notes

    # Answered, but with nothing usable — a changed tag scheme, or a truncated
    # response. Same reasoning as a failed request: keep what we had.
    print("  ! the response contained no chain supermarkets at all")
    if cached:
        print(f"    keeping {len(cached)} store(s) from the previous lookup")
        return cached, notes + [f"answered with no chain stores; kept {len(cached)} cached"]
    return [], notes + ["answered, but with no chain supermarkets in it"]


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
            headers={"User-Agent": USER_AGENT},
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
                           with_bike_routes: bool = True,
                           schools: dict | None = None) -> dict:
    """Distance/bike/walk (and transit, if a Resrobot key is set) from `coords`
    to every campus in `schools` (default: Stockholm's), keyed by short name.

    `schools` is the city's own campus list, deliberately — measuring every area
    against every campus in the country would be both nonsense and quadratic.
    Nobody in Lund wants a commute time to KTH.

    The dashboard's campus dropdown reads this, so switching schools re-filters
    and re-sorts against real numbers rather than re-using KTH's.

    NOTE ON API COST: the straight-line half is pure maths and free, but the
    transit half costs one Resrobot trip lookup per school per location — so
    setting RESROBOT_API_KEY multiplies request volume by len(SCHOOLS). Raise
    `--interval` if you start hitting Trafiklab's quota.
    """
    out = {}
    for sid, school in (schools or SCHOOLS).items():
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
    # The apartment number is dropped here specifically: it's no use for
    # geocoding, and several listings in the same building should share one
    # cache entry. It's not thrown away, though — see `id` below.
    addr_match = _CARD_ADDRESS_RE.search(card_text)
    address = None
    unit = None
    if addr_match:
        address = f"{addr_match.group('street')} {addr_match.group('number')}"
        if addr_match.group("entrance"):
            address += f" {addr_match.group('entrance')}"   # "Armégatan 32 A"
        unit = addr_match.group("unit")

    return {
        # NOT the refid link — confirmed live (2026-08-30) that it doesn't
        # survive between scrapes. Two independent runs 3.5h apart both
        # reported "new: 70 first seen ... (70 of them this run)" for
        # Stockholm's ~70 SSSB listings — the whole set, every time, which
        # SSSB does not actually replace every few hours. `id = url` meant
        # every listing looked brand new on every single scrape: SSSB mints a
        # fresh refid token per page render (this project's own Selenium
        # fallback launches a fresh browser session each run), so the link
        # still resolves fine but never matches its own value from last time.
        # area + address + the apartment/unit number is a real, physical
        # designation of the room that doesn't change between scrapes, which
        # a URL token never was. Falls back to the URL only when the address
        # regex didn't match at all — rare, and no worse than today's always-
        # wrong behavior for exactly those few cards.
        "id": f"sssb-{area}-{address}-{unit}" if address and unit else url,
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
# The trailing `/\s*\d+` was always part of this match — it's the anchor that
# stops the street-name run at the right point — but the apartment number
# itself went uncaptured. That number is a physical unit designation on the
# building (floor + door, by the look of "1512", "1301", "1207"), which makes
# it exactly the stable-per-room key `id` below needed and never had: SSSB's
# listing `id` was the refid link itself, and that link is not a permanent
# identifier for the room, only a valid one — see the `id` comment below.
_CARD_ADDRESS_RE = re.compile(
    r"(?P<street>(?:[A-ZÅÄÖ][\wåäöÅÄÖ:.\-]*\s+){0,2}[A-ZÅÄÖ][\wåäöÅÄÖ:.\-]*)"
    r"\s+(?P<number>\d+)\s*(?P<entrance>[A-ZÅÄÖ])?\s*/\s*(?P<unit>\d+)"
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



# ── Servers that forget to send their intermediate certificate ───────────
#
# afbostader.se does exactly that, on both the apex and www. The live run said
# so precisely once _why() started printing the message instead of the word
# "SSLError":
#
#     [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
#     unable to get local issuer certificate
#
# That phrase means the chain they sent can't be joined to any trusted root —
# the leaf is signed by an intermediate the server never handed over. Browsers
# hide this: every certificate carries an "Authority Information Access"
# extension naming a URL to fetch the issuer from, and browsers quietly follow
# it. OpenSSL does not, so requests fails where Chrome succeeds. It is their
# misconfiguration, but it's ours to work around.
#
# So we do what the browser does. Note what this deliberately is NOT:
# verify=False. Verification stays fully on for the real request — we only
# read the chain the server offered, fetch the issuer it pointed us at, and add
# it to certifi's bundle. If the fetched intermediate doesn't chain to a
# genuinely trusted root, the retry still fails, exactly as it should.
_REPAIRED_BUNDLES: dict[tuple[str, int], str | None] = {}


def _fetch_issuer_certs(host: str, port: int = 443, hops: int = 3) -> list[bytes]:
    """The intermediates a server should have sent, fetched via each cert's AIA."""
    import ssl
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs7

    # Unverified purely to READ the certificate the server presents. Nothing is
    # trusted on the strength of this connection; the verified retry is what
    # decides whether the repair was legitimate.
    leaf = ssl.get_server_certificate((host, port), timeout=15)
    cert, out = x509.load_pem_x509_certificate(leaf.encode()), []

    for _ in range(hops):
        try:
            aia = cert.extensions.get_extension_for_class(x509.AuthorityInformationAccess).value
        except x509.ExtensionNotFound:
            break
        urls = [d.access_location.value for d in aia
                if d.access_method == x509.oid.AuthorityInformationAccessOID.CA_ISSUERS]
        if not urls:
            break
        blob = requests.get(urls[0], headers={"User-Agent": USER_AGENT}, timeout=15).content
        # Issuers are published as bare DER most often, PEM sometimes, and
        # PKCS#7 by a few CAs. Try all three rather than assume.
        for load in (lambda b: [x509.load_der_x509_certificate(b)],
                     lambda b: [x509.load_pem_x509_certificate(b)],
                     lambda b: pkcs7.load_der_pkcs7_certificates(b)):
            try:
                certs = load(blob)
                break
            except Exception:
                certs = None
        if not certs:
            break
        cert = certs[0]
        out += [c.public_bytes(serialization.Encoding.PEM) for c in certs]
        if cert.issuer == cert.subject:      # reached a self-signed root
            break
    return out


def _repaired_ca_bundle(host: str, port: int = 443) -> str | None:
    """certifi's roots plus whatever intermediates `host` failed to send, or None."""
    if (host, port) in _REPAIRED_BUNDLES:
        return _REPAIRED_BUNDLES[(host, port)]
    path = None
    try:
        import certifi
        extra = _fetch_issuer_certs(host, port)
        if extra:
            path = str(DATA_DIR / f"ca-bundle-{re.sub(r'[^A-Za-z0-9.-]', '_', host)}.pem")
            Path(path).write_bytes(Path(certifi.where()).read_bytes() + b"\n" + b"\n".join(extra))
            print(f"  · {host} omitted {len(extra)} intermediate certificate(s); "
                  f"fetched them from the CA and retrying with verification still on")
    except Exception as e:
        print(f"  · couldn't repair {host}'s certificate chain ({_why(e)[0]})")
        path = None
    _REPAIRED_BUNDLES[(host, port)] = path
    return path


def _get(url, **kwargs):
    """requests.get, with one retry for a server that omitted its intermediate."""
    try:
        return requests.get(url, **kwargs)
    except requests.exceptions.SSLError as e:
        if "unable to get local issuer certificate" not in str(e):
            raise
        parts = urlsplit(url)
        # Port matters: without it the probe would read the certificate of
        # whatever answers on 443 instead of the host that actually failed.
        bundle = _repaired_ca_bundle(parts.hostname or "", parts.port or 443)
        if not bundle:
            raise
        return requests.get(url, **{**kwargs, "verify": bundle})


def _why(e, resp=None) -> tuple[str, str]:
    """Why a request failed: (short, detailed). Short is fit for the dashboard,
    detailed for the terminal.

    This replaces a bare `type(e).__name__`, which cost a whole publish. A live
    Actions run reported exactly `AF Bostäder → SSLError` and `SGS category
    residential → 407` — and neither says anything actionable. "SSLError" is an
    expired certificate, a hostname mismatch and a missing intermediate all at
    once until you read its message, and a 407 is somebody's proxy refusing on
    the far end, which names itself in the response headers if you bother to
    print them. Both cities published an empty map off the back of that.

    Same principle as everything else here: the log is the debugger, so what it
    prints has to be enough to act on without reproducing the failure.
    """
    r = resp if resp is not None else getattr(e, "response", None)
    if r is not None and getattr(r, "status_code", None):
        short = f"HTTP {r.status_code}"
        bits = [short + (f" {r.reason}" if r.reason else "")]
        # A 407 or 403 from a WAF, proxy or CDN identifies itself in these; the
        # application behind it never saw the request at all. Printing them is
        # the difference between "blocked" and "blocked by X, which wants Y".
        for h in ("Proxy-Authenticate", "WWW-Authenticate", "Server", "Via",
                  "X-Cache", "CF-Ray", "X-Powered-By"):
            if r.headers.get(h):
                bits.append(f"{h}: {r.headers[h]}")
        try:
            body = " ".join((r.text or "").split())[:200]
        except Exception:          # a body that won't even decode is still a clue
            body = ""
        if body:
            bits.append(f"body: {body}")
        return short, " · ".join(bits)
    msg = " ".join(str(e).split())
    short = type(e).__name__
    return short, f"{short}: {msg}" if msg else short


def _decode_json(resp):
    """Parse a JSON response, decoding it the same careful way as HTML.

    Same trap as _decode_response(): a server that sends `application/json` with
    no charset gets ISO-8859-1 from requests, and these feeds are full of
    Swedish — "Olofshöjd", "Enkelrum med gruppkök", "Kämnärsvägen". JSON is
    UTF-8 by definition (RFC 8259), so prefer it and only fall back if it
    genuinely isn't.
    """
    return json.loads(_decode_response(resp))


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
                # Browser-shaped prefix so naive bot filters don't reject it,
                # with our identity and contact URL appended — the conventional
                # shape for a well-behaved crawler. Looking like a browser and
                # being anonymous are different things, and only the first one is
                # actually needed here.
                "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               f"Chrome/126 Safari/537.36 {USER_AGENT}"),
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

def load_previous(city: str = DEFAULT_CITY) -> dict:
    for path in (listings_file(city),
                 # One-time migration path: see _legacy_listings_file().
                 _legacy_listings_file() if city == DEFAULT_CITY else None):
        if path and path.exists():
            try:
                return json.loads(path.read_text())
            except (ValueError, OSError):
                continue
    return {"listings": [], "generated_at": None}


def saved_data_age_minutes(city: str = DEFAULT_CITY) -> float | None:
    """How old the saved listings are, or None if there aren't any / the
    timestamp is unreadable.

    `--serve` uses this to decide whether to scrape before serving. It used to
    check only whether the file existed, which meant a checkout carrying an old
    data file would serve those listings as current until the first background
    poll came round — up to `--interval` minutes of quietly showing stale
    listings with a timestamp that looked fine.
    """
    try:
        generated_at = load_previous(city).get("generated_at")
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
                    "User-Agent": f"Mozilla/5.0 {USER_AGENT}",
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
            print(f"  · {url} → {_why(e)[1]}")
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


# ── SGS Studentbostäder, Göteborg ────────────────────────────────────────
#
# SGS runs on **Momentum**, a property-management platform — note the host and
# the `/Prod/sgs/` tenant segment. That matters beyond Göteborg: any other
# Swedish student landlord on Momentum exposes this same API at a different
# tenant path, so a second Momentum city is a URL change rather than a new
# scraper. Worth checking before writing one from scratch.
#
# Plain JSON GET, no auth, no cookie — a ~2 second scrape, against the ~1 minute
# Chrome costs for SSSB's JS shell.
SGS_API = "https://sgs-fastighet.momentum.se/Prod/sgs/PmApi/v2/market/objects"
SGS_SITE = "https://minasidor.sgs.se/market/"
# Categories are opaque ids in their system; `residential` (Studentbostad) just
# happens to be readable. The second is "Studentbostad Express", inferred from
# its page route — the main list's route segment is exactly this parameter — and
# **not yet confirmed to work**. A category that fails is reported rather than
# failing the scrape, so Göteborg can say so and link to it instead.
# Deliberately excluded: Parkeringsplats, CIS, Ismo.
#
# Kept as id → label rather than a bare list because the id is what the API
# wants and the label is what a person can read: "VqcHjmtPBDFwFFTVb4fydPxw →
# HTTP 407" is a fine thing to print in a terminal and a terrible thing to show
# someone looking for a flat.
SGS_CATEGORIES = {
    "residential": "Studentbostad",
    "VqcHjmtPBDFwFFTVb4fydPxw": "Studentbostad Express",
}

# The endpoint started answering every request with HTTP 407 InvalidApiKey —
# "Appen är inte registrerad" / "API-nyckel måste anges" — an Azure API
# Management rejection for a missing subscription key, confirmed once _why()
# started printing response bodies instead of a bare status code. These two
# headers are what minasidor.sgs.se's own frontend sends on every request to
# this same endpoint, pulled from a real DevTools Network tab. Neither is a
# login or a personal credential — they're shipped in cleartext to every
# visitor's browser to let the public listings page work at all, the same
# category of thing as SGS_API itself, just delivered as a header instead of
# baked into a URL. If SGS ever rotates them, the fix is a DevTools trip, not
# a code change: the two headers below are the only place they're used.
SGS_API_KEY = "pJnKrR6B3FzRNFsF33xL8LhSs55KPJrm"
SGS_DEVICE_KEY = "217e1014f02547078d060a0a0f47f2ba"


def _dotnet_date(value) -> str | None:
    """`/Date(1790805600000)/` -> `'2026-09-30'`.

    ASP.NET's JSON date format: epoch milliseconds wrapped in a string. Handing
    this to fromisoformat throws, which is the obvious wrong guess.
    """
    m = re.search(r"/Date\((-?\d+)\)/", str(value or ""))
    if not m:
        return None
    try:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, timezone.utc).date().isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def _num(value, cast=float):
    """Feeds arrive with numbers quoted, blank or absent. None means "not stated",
    which the dashboard keeps distinct from zero."""
    if value in (None, "", " "):
        return None
    try:
        return cast(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


def fetch_sgs(categories=None) -> tuple[list[dict], list[str]]:
    """SGS's vacant objects, as our listing dicts. Returns (listings, notes)."""
    listings, notes, seen = [], [], set()
    cats = categories or SGS_CATEGORIES
    for cat in cats:
        label = (SGS_CATEGORIES.get(cat) if isinstance(SGS_CATEGORIES, dict) else None) or cat
        # limit=100 because that is the value the request was actually captured
        # with, and it returned the full list (`count` mirrors the site's own
        # "N annonser", so a truncation would show). 200 was a guess on top of a
        # verified request, which is the wrong way round for this project.
        url = f"{SGS_API}?type={cat}&limit=100"
        try:
            resp = requests.get(url, headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Accept-Language": "sv-SE,sv;q=0.9",
                # The same treatment fetch_bostadsformedlingen() already needs:
                # this is the API the market page itself calls, and an endpoint
                # fronted by a WAF can reject anything that doesn't look like it
                # came from there. The identifying User-Agent above stays — the
                # point is to look like the page's own request, not to look like
                # somebody else.
                "Referer": SGS_SITE,
                "Origin": "https://minasidor.sgs.se",
                # The actual fix — see the comment on SGS_API_KEY above. Header
                # names as sent by the real frontend, lowercase.
                "x-api-key": SGS_API_KEY,
                "x-momentum-device-key": SGS_DEVICE_KEY,
            }, timeout=30)
            resp.raise_for_status()
            payload = _decode_json(resp)
            items = payload.get("items") or []
        except (requests.RequestException, ValueError) as e:
            short, detail = _why(e)
            print(f"  · SGS category {label} ({cat}) → {detail}")
            notes.append(f"({label}) → {short}")
            continue
        print(f"  · SGS category {label} ({cat}) → OK "
              f"({payload.get('count', len(items))} advertised)")
        for it in items:
            oid = it.get("id")
            if not oid or oid in seen:
                continue
            seen.add(oid)
            size = it.get("size") or {}
            area = ((it.get("location") or {}).get("area") or {}).get("displayName")
            listings.append({
                "id": f"sgs-{oid}",
                # The live data has a trailing space on some of these.
                "address": (it.get("displayName") or "").strip() or None,
                "area": (area or "").strip() or "Unknown",
                "type": size.get("roomsDisplayName"),
                # A float like 4745.7200 — rent is shown to the krona.
                "rent_sek": (lambda v: None if v is None else round(v))(
                    _num((it.get("pricing") or {}).get("price"))),
                "size_sqm": _num(size.get("area")),
                # SGS publishes none of these three, and a made-up value would be
                # worse than an honest gap: the dashboard already treats None as
                # "not stated" and never filters a listing out for it.
                "queue_days": None,
                "floor": None,
                "coords": None,
                "max_years": None,
                "el_included": None,
                "available_from": _dotnet_date((it.get("availability") or {}).get("availableFrom")),
                "deadline": None,
                "tagline": it.get("description"),
                "url": SGS_SITE + str(oid),
                "provider": "SGS",
                "landlord": "SGS Studentbostäder",
            })
    print(f"  kept {len(listings)} SGS listing(s)")
    return listings, notes


# ── AF Bostäder, Lund ────────────────────────────────────────────────────
#
# The richest of the three feeds: full street address *and* postcode per listing,
# so Lund gets per-building map dots with no card-text parsing at all, plus a
# floor and a real application deadline.
# Two hosts, tried in order, for a reason: the apex came from the `redimoUrl`
# in an inline `var afb = {...}` on their page, not from a captured request, and
# an apex that only exists to redirect browsers to www often carries a
# certificate or a TLS chain that a browser papers over and `requests` will not.
# The first live run failed here with a bare `SSLError`, which is consistent with
# that and with three other causes — hence _why() above, so the next run says
# which. Same candidate-list idea as fetch_bostadsformedlingen().
AF_API_HOSTS = ["https://afbostader.se", "https://www.afbostader.se"]
AF_API_PATH = "/DiremoApi/redimo/rest/vacantproducts"
AF_SITE = "https://www.afbostader.se/lediga-bostader/"


def fetch_afbostader() -> tuple[list[dict], list[str]]:
    """AF Bostäder's vacant student housing. Returns (listings, notes).

    `type=1` is housing; the site also advertises förråd (storage), which is a
    different type and filtered out in the query rather than in our code.
    """
    payload, failures = None, []
    for base in AF_API_HOSTS:
        try:
            # _get, not requests.get: this host omits its intermediate
            # certificate, which is the whole reason Lund published nothing.
            resp = _get(base + AF_API_PATH, params={"lang": "sv_SE", "type": "1"},
                        headers={"User-Agent": USER_AGENT,
                                 "Accept": "application/json",
                                 "Accept-Language": "sv-SE,sv;q=0.9"},
                        timeout=30)
            resp.raise_for_status()
            payload = _decode_json(resp)
        except (requests.RequestException, ValueError) as e:
            short, detail = _why(e)
            print(f"  · AF Bostäder {base} → {detail}")
            failures.append(short)
            continue
        print(f"  · AF Bostäder {base} → OK")
        break

    if payload is None:
        return [], [failures[0] if failures else "no response"]

    # The envelope carries its own error channel; a 200 with an error set is
    # still a failure and must not be read as "no vacancies".
    if payload.get("error"):
        print(f"  · AF Bostäder returned an error: {payload['error']}")
        return [], [str(payload["error"])]

    products = payload.get("product") or []
    print(f"  · AF Bostäder → OK ({len(products)} product(s))")
    listings = []
    for it in products:
        # Everything arrives as a string, including the numbers.
        zipcode = (it.get("zipcode") or "").strip()
        town = (it.get("city") or "").strip().title()
        address = (it.get("address") or "").strip() or None
        listings.append({
            "id": f"af-{it.get('productId')}",
            "address": address,
            "area": (it.get("area") or "").strip() or "Unknown",
            "type": it.get("shortDescription") or it.get("type"),
            "rent_sek": _num(it.get("rent"), int),
            "size_sqm": _num(it.get("sqrMtrs")),
            "floor": _num(it.get("floor"), int),
            # NOT queueNumber. That field is "Din plats just nu" — your own place
            # in the queue — and comes back "1" on every row for a logged-out
            # caller, so mapping it would print a confident, wrong "1 day" on
            # every Lund listing. AF publishes no per-listing queue figure.
            "queue_days": None,
            # What it publishes instead, and it discriminates well: how many
            # people have already applied. 40 versus 6 is the whole answer.
            "applicants": _num(it.get("numberOfReservations"), int),
            "deadline": (it.get("reserveUntilDate") or None),
            "available_from": (it.get("moveInDate") or None),
            # Reserved for students new to Lund.
            "novice_priority": (it.get("priority") == "Novisch"),
            "contract_months": _num(it.get("rentalPeriods"), int),
            "coords": None,
            "max_years": None,
            "el_included": None,
            # Postcode and town are what make the address unambiguous nationally,
            # the same anchoring geocode_listing_address() does for SSSB.
            "geocode_hint": ", ".join(x for x in (address, zipcode, town, "Sweden") if x),
            "url": AF_SITE,
            "provider": "AF Bostäder",
            "landlord": "AF Bostäder",
        })
    return listings, []


def fetch_sssb(debug: bool = False, use_login: bool = False,
               http_only: bool = False, **_) -> tuple[list[dict], list[str]]:
    """SSSB's vacancy list. Plain HTTP first, Chrome only if that yields nothing.

    Kept as the slow path it is: SSSB's page is a JS shell, so this almost always
    ends up in Selenium and costs about a minute. The cheap attempt stays because
    it costs one request and would start working the moment the page becomes
    server-rendered.
    """
    rows = None
    if not use_login:
        rows = fetch_sssb_http(debug=debug)
        if rows is None and http_only:
            raise SystemExit(
                "--http-only was requested but the vacancy list couldn't be read without a "
                "browser (see the diagnostics above). Drop --http-only to fall back to Selenium."
            )
    elif http_only:
        raise SystemExit("--http-only and --with-login are contradictory: logging in needs a browser.")

    if rows is None:
        print("falling back to the browser..." if not use_login
              else "launching browser + logging in...")
        driver = init_driver(headless=not debug)
        try:
            if use_login:
                login(driver)
            print("scraping listings...")
            rows = scrape_listings(driver, debug=debug)
        finally:
            driver.quit()

    for l in rows:
        l["provider"] = "SSSB"
        l["landlord"] = "SSSB"
    return rows, []


# Which fetchers exist, and — the part that matters — whether each one's listings
# are placed by *area* or by their own coordinates. That single flag decides
# which listings get an address geocoded, which areas exist at all, and which get
# a roundel rather than a pin. The dashboard reaches the same split from the data
# alone; this is the scrape's side of the same distinction.
PROVIDER_FETCHERS = {
    "sssb":                {"fn": fetch_sssb, "provider": "SSSB", "areas": True},
    "bostadsformedlingen": {"fn": lambda **kw: (fetch_bostadsformedlingen(), []),
                            "provider": "Bostadsförmedlingen", "areas": False},
    "sgs":                 {"fn": lambda **kw: fetch_sgs(), "provider": "SGS", "areas": True},
    "afbostader":          {"fn": lambda **kw: fetch_afbostader(),
                            "provider": "AF Bostäder", "areas": True},
}


def run_scrape(debug: bool = False, use_login: bool = False,
               http_only: bool = False, bike_routes: bool = True,
               bf_bike_routes: bool = False, city: str = DEFAULT_CITY) -> dict:
    with _scrape_lock:
        return _run_scrape_impl(debug=debug, use_login=use_login, http_only=http_only,
                                bike_routes=bike_routes, bf_bike_routes=bf_bike_routes,
                                city=city)


def _run_scrape_impl(debug: bool = False, use_login: bool = False,
                     http_only: bool = False, bike_routes: bool = True,
                     bf_bike_routes: bool = False, city: str = DEFAULT_CITY) -> dict:
    conf = city_conf(city)
    print(f"[{datetime.now().isoformat(timespec='seconds')}] starting {conf['name']} scrape...")
    previous = load_previous(city)

    geocode_cache = _load_geocode_cache()
    # Campuses may be given as addresses rather than coordinates; resolve once,
    # then everything downstream sees the same shape Stockholm always had.
    conf = {**conf, "schools": resolve_schools(conf, geocode_cache)}
    grocery_stores, grocery_notes = fetch_grocery_stores(conf["grocery_bbox"], city)
    if not grocery_stores and previous.get("groceries"):
        # Last line of defence: a lookup that failed with no cache on disk would
        # otherwise publish a payload with no shops, and the dashboard hides its
        # Shops toggle when there are none — so one bad Overpass minute would
        # silently remove the feature until the next successful scrape.
        grocery_stores = previous["groceries"]
        print(f"  reusing {len(grocery_stores)} store(s) from the previous run's data")
        grocery_notes.append(f"reused {len(grocery_stores)} from the previous run")

    # ── 1. Listings, from whichever providers this city has ──────────────
    #
    # The order matters and it changed: listings are fetched *before* areas are
    # built, because a city without a hand-written area table derives its areas
    # from what its listings actually report. Stockholm has such a table and is
    # unaffected; Göteborg and Lund would otherwise need 30-odd areas typed out
    # by hand and kept in step with the landlord forever.
    listings, provider_notes = [], []
    for key in conf["fetchers"]:
        spec = PROVIDER_FETCHERS.get(key)
        if not spec:
            raise SystemExit(f"{city}: unknown provider fetcher {key!r} — known: "
                             f"{', '.join(sorted(PROVIDER_FETCHERS))}")
        rows, notes = spec["fn"](debug=debug, use_login=use_login, http_only=http_only)
        # Stamped here rather than trusted from the fetcher. A fetcher that
        # forgot would otherwise have its listings quietly treated as
        # coordinate-based, which skips their address geocoding and leaves them
        # off the map entirely — a silent wrong answer rather than an error.
        # setdefault-style so a real landlord (BF's queue name) still wins.
        for l in rows:
            if not l.get("provider"):
                l["provider"] = spec["provider"]
            if not l.get("landlord"):
                l["landlord"] = spec["provider"]
        listings += rows
        # Tagged with the provider here rather than inside each fetcher, so the
        # dashboard can name the source that failed and link out to it without
        # parsing a sentence. A source that answers with nothing and a source
        # that never answered look identical on a map otherwise — which is
        # exactly how two whole cities published a blank page that read as
        # "no vacancies in Göteborg".
        provider_notes += [{"source": spec["provider"], "detail": n} for n in notes]

    # Providers whose listings are placed by area rather than by their own
    # coordinates. This is what decides which areas exist and which listings get
    # geocoded from an address; the dashboard reaches the same split from the
    # data alone, by checking whether a listing's area is one we have.
    area_providers = {p for key in conf["fetchers"]
                      if PROVIDER_FETCHERS[key]["areas"]
                      for p in [PROVIDER_FETCHERS[key]["provider"]]}
    area_rows = [l for l in listings if l.get("provider") in area_providers]

    # ── 2. Put each area-based listing at its own building ───────────────
    #
    # Distinct addresses only — a dozen rooms in one building share a lookup —
    # and cached forever, since buildings don't move. Anything unresolved keeps
    # `coords: None` and stays at the area centre.
    addr_cache = _load_address_cache()
    wanted, hints = [], {}
    for l in area_rows:
        if not l.get("address"):
            continue
        if l["address"] not in hints:
            wanted.append((l["address"], l.get("area")))
            hints[l["address"]] = l.get("geocode_hint")
    wanted.sort()
    fresh = [a for a, _ in wanted if a not in addr_cache]
    if fresh:
        print(f"geocoding {len(fresh)} new building address(es) "
              f"({len(wanted) - len(fresh)} already cached, ~1s each)...")
    for address, area in wanted:
        geocode_listing_address(address, area, addr_cache, hint=hints.get(address),
                                addresses=conf.get("area_addresses"),
                                city_name=conf["name"])
    for l in area_rows:
        l["coords"] = addr_cache.get(l.get("address")) if l.get("address") else None

    # ── 3. The areas themselves ──────────────────────────────────────────
    if conf.get("areas"):
        area_groups = {name: group for group, names in conf["areas"].items() for name in names}
    else:
        # Derived from the feed, so a new area appears on the map by itself. The
        # single group is deliberate: without a published grouping there is
        # nothing honest to colour by, and the dashboard hides a one-group strip.
        area_groups = {l["area"]: conf["name"] for l in area_rows
                       if l.get("area") and l["area"] != "Unknown"}
        print(f"  {len(area_groups)} area(s) derived from the listings themselves")

    print("geocoding areas (cached after first run)...")
    if bike_routes:
        print(f"working out cycling routes to {len(conf['schools'])} campuses "
              "(real bike directions, cached to data/bike_route_cache.json — "
              "the first run is slow, later ones aren't)...")

    area_coords = {}
    for name, group in area_groups.items():
        coords = None
        if (conf.get("area_addresses") or {}).get(name) or conf.get("areas"):
            coords = geocode_area(name, geocode_cache,
                                  addresses=conf.get("area_addresses"),
                                  city_name=conf["name"])
        if not coords:
            # No table entry: take the area's own listings as the answer. The
            # *median* rather than the mean, so one address that resolved to
            # another municipality can't drag the centre — which matters because
            # this centre is then the anchor the outlier check below trusts.
            here = sorted(tuple(l["coords"]) for l in area_rows
                          if l.get("area") == name and l.get("coords"))
            if here:
                mid = len(here) // 2
                coords = (sorted(c[0] for c in here)[mid], sorted(c[1] for c in here)[mid])
        if not coords:
            coords = geocode_area(name, geocode_cache,
                                  addresses=conf.get("area_addresses"),
                                  city_name=conf["name"])
        area_coords[name] = coords

    # ── 4. Reject buildings that landed implausibly far from their area ──
    #
    # A geocoder will happily hand back a street of the same name in a different
    # municipality, and nothing downstream would notice: the dot lands miles
    # away, and worse, that fake distance pushes the area over the split
    # threshold so its roundel dissolves into per-building dots for a reason that
    # isn't real. Confirmed live on 2026-08-09 — Domus, a single block on
    # Körsbärsvägen, reported a 15,948 m spread.
    #
    # Checked here rather than inside geocode_listing_address() on purpose: this
    # way it also catches entries already sitting in the cache (including the one
    # the Actions runner restores), so a bad answer stops mattering immediately
    # instead of needing the cache purged first.
    misplaced = drop_misplaced_addresses(
        area_rows, {n: {"coords": c} for n, c in area_coords.items()})
    if misplaced:
        print(f"  ! {len(misplaced)} address(es) resolved more than "
              f"{MAX_ADDRESS_OFFSET_M / 1000:g} km from their own area and were dropped "
              "back to the area centre — most likely the same street name in another "
              "municipality. Correct them in data/address_cache.json to place them "
              "properly:")
        for address, (area, off_m) in sorted(misplaced.items(), key=lambda kv: -kv[1][1]):
            print(f"    · {address!r} ({area}) — {off_m / 1000:.1f} km away")
        # Where the city has a hand-verified table the centre is the trusted
        # anchor, so a single stray address means a bad address lookup. If
        # *every* address in one area shows up, suspect the anchor instead.
        from collections import Counter
        by_area = Counter(area for area, _ in misplaced.values())
        for area, n in by_area.items():
            in_area = len({l["address"] for l in area_rows
                           if l.get("area") == area and l.get("address")})
            if n == in_area and in_area > 1:
                print(f"    ! that's every address in {area} — more likely its own centre "
                      "is wrong in data/geocode_cache.json than all of its buildings")

    located = sum(1 for l in area_rows if l.get("coords"))
    print(f"  {located} of {len(area_rows)} area-based listing(s) placed at their own "
          f"building ({len(wanted)} distinct address(es))")

    # ── 5. Commutes, per area and per pin ────────────────────────────────
    area_info = {}
    for name, group in area_groups.items():
        coords = area_coords.get(name)
        per_school = (commute_to_all_schools(coords, with_bike_routes=bike_routes,
                                             schools=conf["schools"])
                      if coords else None)
        area_info[name] = {
            "group": group,
            "coords": list(coords) if coords else None,
            # Per-campus numbers drive the dashboard's campus dropdown.
            "per_school": per_school,
            # Kept at the top level too, pointing at the default campus, so
            # older saved files and any code reading the old shape still work.
            "straight_line": straight_line_estimate(coords) if coords else None,
            "transit_min": (per_school[conf["default_school"]]["transit_min"]
                            if per_school else None),
            # One number beats 300 map dots for "can I buy food here" — the
            # dots are for browsing, this is for deciding.
            "nearest_grocery": (nearest_grocery(coords, grocery_stores)
                                if coords and grocery_stores else None),
        }

    # How spread out each area's listings actually are. The dashboard uses this to
    # decide which roundels are worth dissolving into per-building dots: a single
    # block comes out near zero, a campus like Lappkärrsberget in the hundreds.
    for name, area in area_info.items():
        here = [l["coords"] for l in area_rows if l.get("area") == name and l.get("coords")]
        area["spread_m"] = area_spread_m(here)
        area["located_listings"] = len(here)
    spread_areas = {n: a["spread_m"] for n, a in area_info.items() if a["spread_m"] >= 120}
    if spread_areas:
        print("  spread out enough to split on the map: "
              + ", ".join(f"{n} ({m} m)" for n, m in
                          sorted(spread_areas.items(), key=lambda kv: -kv[1])))

    # Pin-based ads get straight-line estimates rather than routed bike times by
    # default. There are ~100 of them and they churn, so routing them costs
    # ~100 x len(SCHOOLS) requests — around 700, i.e. ~9 minutes of paced
    # requests — and most of that work is thrown away as ads rotate. A city's
    # areas are a fixed set worth routing once; these aren't.
    # `--bike-routes-bf` opts in when you want the accuracy anyway.
    pin_rows = [l for l in listings if l.get("provider") not in area_providers]
    if pin_rows:
        print(f"working out commutes for {len(pin_rows)} per-coordinate ad(s)"
              + (" with real cycling routes (slow)..." if bf_bike_routes
                 else " using straight-line estimates (--bike-routes-bf for real routes)..."))
    for l in pin_rows:
        if l.get("coords"):
            l["per_school"] = commute_to_all_schools(
                tuple(l["coords"]), with_bike_routes=bike_routes and bf_bike_routes,
                schools=conf["schools"])
            l["straight_line"] = straight_line_estimate(tuple(l["coords"]))
            l["transit_min"] = l["per_school"][conf["default_school"]]["transit_min"]

    # Carry each listing's first-seen timestamp forward, then derive NEW from it.
    # With no usable history at all, every listing is recorded as first_seen=None
    # — "was already there when we started looking" — because never having looked
    # before is not the same thing as 170 listings having just appeared. That is
    # the case that used to badge, and notify about, the entire list.
    now = datetime.now(timezone.utc)
    had_history = bool(previous.get("generated_at"))
    prev_seen = {l["id"]: l.get("first_seen") for l in previous["listings"]}
    for l in listings:
        if not had_history:
            l["first_seen"] = None
        elif l["id"] in prev_seen:
            # Whatever was recorded stands — *including* None, which means "was
            # already listed before we started keeping track". Testing the value
            # rather than the key here (`prev_seen.get(id) or now`) is a live bug
            # I wrote and the test caught: None is falsy, so every cold-start
            # listing got restamped "first seen now" on the very next run and the
            # badge came back on all 63 rows.
            l["first_seen"] = prev_seen[l["id"]]
        else:
            l["first_seen"] = now.isoformat()
    new_listings = [l for l in listings if _seen_recently(l["first_seen"], now)]
    from collections import Counter as _C
    by_provider = _C(l.get("provider") or "?" for l in listings)
    print(f"found {len(listings)} listings total — "
          + ", ".join(f"{n} {p}" for p, n in by_provider.most_common()))
    if provider_notes:
        # A provider or category that failed is reported rather than silently
        # missing, and the city's coverage note can then link out to it.
        print("  ! some sources didn't answer: "
              + "; ".join(f"{n['source']} {n['detail']}" for n in provider_notes))
    if not had_history:
        print(f"  no previous run to compare against, so none are marked new — "
              f"all {len(listings)} are recorded as first seen now")
    else:
        first_time = sum(1 for l in listings if l["first_seen"] == now.isoformat())
        print(f"  new: {len(new_listings)} first seen in the last "
              f"{NEW_WINDOW_HOURS:g}h ({first_time} of them this run)")

    routed = sum(1 for a in area_info.values()
                 if a["per_school"]
                 and a["per_school"][conf["default_school"]]["bike_source"] == "route")
    if bike_routes:
        print(f"bike times: {routed} of {len(area_info)} area(s) from real cycling routes, "
              f"{len(area_info) - routed} from the straight-line estimate")

    # What the dashboard's own commute filter actually does to this city, in the
    # same units the slider uses — added after Lund shipped 16 real listings that
    # still read as "0 found" on the live site. Every earlier line here answers
    # "did the scrape work"; none of them answer "will a visitor's screen be
    # empty anyway", and those turned out to be different questions. commuteMinutes()
    # in index.html prefers transit_min and falls back to bike_min — mirrored here
    # rather than re-derived, so this print can't quietly drift from what the
    # frontend actually filters on.
    default_school = conf["default_school"]
    if default_school not in conf["schools"]:
        # Same failure this whole block exists to catch, one step further back:
        # if the default campus itself has no coordinates, every area's commute
        # to it is unmeasurable and the dashboard's default view is empty for a
        # reason this print can't see. Said explicitly rather than the block
        # below just quietly having nothing to report.
        print(f"  ! default campus {default_school} has no coordinates this run — "
              f"every area's commute to it is unmeasurable, so the default filter "
              f"shows nothing until that resolves")
    else:
        commute_min = [
            (a["per_school"][default_school]["transit_min"]
             if isinstance(a["per_school"][default_school].get("transit_min"), (int, float))
             else a["per_school"][default_school]["bike_min"])
            for a in area_info.values() if a.get("per_school") and a["per_school"].get(default_school)
        ]
        if commute_min:
            cap = conf.get("max_commute_default", 45)
            within = sum(1 for m in commute_min if m <= cap)
            print(f"  commute from {default_school} (default campus): "
                  f"{min(commute_min)}-{max(commute_min)} min across {len(commute_min)} area(s) · "
                  f"default filter ≤{cap} min shows {within} of {len(commute_min)} "
                  f"with no slider touched"
                  + ("" if within else " — EVERY area is past the cap, so the default view is empty"))

    # Restated here, next to the run's other headline numbers, because the Overpass
    # block itself prints ~600 lines earlier — near the very start of a scrape. In a
    # terminal you scroll up; in an Actions log you get the tail and nothing else,
    # which is how a run that shipped no shops at all read as a completely normal
    # green build. Whether a feature made it into the payload belongs in the summary.
    trail = f" ({'; '.join(grocery_notes)})" if grocery_notes else ""
    if grocery_stores:
        print(f"shops: {len(grocery_stores)} chain supermarket(s) in the payload{trail}")
    else:
        print(f"shops: none — the dashboard's Shops toggle will be hidden{trail}")

    result = {
        # Which city this payload is, so the dashboard can't render one city's
        # listings under another's name after a switch.
        "city": city,
        "city_name": conf["name"],
        "max_commute_default": conf.get("max_commute_default", 45),
        "coverage_note": conf.get("coverage_note"),
        "coverage_links": conf.get("coverage_links") or [],
        # Which sources failed this run, so an empty city says why instead of
        # claiming there is nothing available. Empty list = everything answered,
        # and then zero listings really does mean zero vacancies.
        "source_notes": provider_notes,
        "providers": conf["providers"],
        "area_group_noun": conf.get("area_group_noun", "area"),
        "transit_overlay": conf.get("transit_overlay"),
        "sources": conf.get("sources") or [],
        # So the badge's tooltip can say what NEW actually means rather than the
        # number living in two places.
        "new_window_hours": NEW_WINDOW_HOURS,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kth_coords": KTH_COORDS,
        # The dashboard builds its campus dropdown from this, so the two lists
        # can't drift apart.
        "schools": {sid: {"name": sc["name"], "coords": list(sc["coords"])}
                    for sid, sc in conf["schools"].items()},
        "default_school": conf["default_school"],
        "areas": area_info,
        "listings": listings,
        # Chain supermarkets, for the map's optional shop dots. Sent as a flat
        # list rather than per-area because they're their own map layer.
        "groceries": grocery_stores,
        "new_listing_ids": [l["id"] for l in new_listings],
    }
    listings_file(city).write_text(json.dumps(result, indent=2, ensure_ascii=False))
    write_cities_index()
    notify_new(new_listings)
    return result


# ── Local API + dashboard server ─────────────────────────────────────────

def _background_poll_loop(interval_minutes: float, use_login: bool = False,
                          http_only: bool = False, bike_routes: bool = True,
                          bf_bike_routes: bool = False, cities=None):
    """Runs for the lifetime of `--serve`, re-scraping on its own so you
    don't have to sit there clicking Refresh. Any failure (SSSB hiccup,
    network blip) is logged and skipped rather than killing the loop.

    Each city is caught separately: one city's provider being down must not
    stop the others from refreshing, the same way a failure here has always
    been logged and skipped rather than killing the loop.
    """
    while True:
        time.sleep(interval_minutes * 60)
        for city in (cities or [DEFAULT_CITY]):
            try:
                print(f"[{datetime.now().isoformat(timespec='seconds')}] "
                      f"auto-check ({city})...")
                run_scrape(use_login=use_login, http_only=http_only, bike_routes=bike_routes,
                           bf_bike_routes=bf_bike_routes, city=city)
            except SystemExit as e:
                print(f"  ! {city} auto-check stopped early: {e}")
            except Exception as e:
                print(f"  ! {city} auto-check failed, will retry next interval: {e}")


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
          bf_bike_routes: bool = False, open_browser: bool = True,
          cities=None):
    from flask import Flask, jsonify, send_from_directory
    from flask_cors import CORS

    app = Flask(__name__)
    CORS(app)  # local dev tool — fine to allow any origin

    static_dir = Path(__file__).parent

    @app.route("/")
    def index():
        return send_from_directory(static_dir, "index.html")

    def _requested_city():
        """?city=<id>, falling back to the default. An unknown id falls back
        rather than erroring — a stale bookmark should show *something*."""
        from flask import request
        asked = request.args.get("city") or DEFAULT_CITY
        return asked if asked in CITIES else DEFAULT_CITY

    @app.route("/api/cities")
    def api_cities():
        """What the city picker reads. Rebuilt on request rather than served
        from disk, so a city scraped after startup appears without a restart."""
        return jsonify({"default": DEFAULT_CITY, "cities": write_cities_index()})

    @app.route("/api/listings")
    def api_listings():
        city = _requested_city()
        saved = load_previous(city)
        if saved.get("generated_at"):
            saved["poll_interval_min"] = interval_minutes
            return jsonify(saved)
        return jsonify(run_scrape(use_login=use_login, http_only=http_only,
                                  bike_routes=bike_routes, bf_bike_routes=bf_bike_routes,
                                  city=city))

    @app.route("/api/status")
    def api_status():
        """Just the timestamp, so the dashboard can check whether anything
        changed without pulling the whole listing set (~100 KB) every minute.
        Data only changes once per --interval, so the vast majority of those
        polls used to transfer and re-render an identical payload.
        """
        generated_at = load_previous(_requested_city()).get("generated_at")
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
                                  bike_routes=bike_routes, bf_bike_routes=bf_bike_routes,
                                  city=_requested_city()))

    threading.Thread(target=_background_poll_loop,
                     args=(interval_minutes, use_login, http_only, bike_routes,
                           bf_bike_routes, cities or [DEFAULT_CITY]),
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
    parser.add_argument("--city", default=None,
                        help=f"which city to scrape: {', '.join(sorted(CITIES))} "
                             f"(default: all of them). The dashboard shows one at a time and "
                             f"picks with the #city part of its URL")
    args = parser.parse_args()
    # A city each, unless one was named. --once with no --city scrapes every
    # city, which is what the publish workflow wants.
    # Naming a city scrapes exactly that one, enabled or not — which is how a
    # city gets checked before it goes live. Omitting --city scrapes only the
    # cities marked enabled, so an unverified one can sit in the registry without
    # reaching the published site.
    scrape_cities = ([args.city] if args.city
                     else [c for c, conf in CITIES.items() if conf.get("enabled", True)])
    for _c in scrape_cities:
        city_conf(_c)   # fail fast on a typo, before a minute of scraping

    if args.forget_login:
        forget_credentials()
    elif args.login:
        _prompt_and_store()
        print("Done — future runs will use this automatically.")
    elif args.once:
        # Each city is scraped in its own try, so one city's provider being down
        # still publishes the others. The exit code stays non-zero if *every*
        # city failed, which is the cron contract: a totally failed run must not
        # look green.
        failed = []
        for _c in scrape_cities:
            try:
                run_scrape(debug=args.debug, use_login=args.with_login,
                           http_only=args.http_only, bike_routes=not args.no_bike_routes,
                           bf_bike_routes=args.bike_routes_bf, city=_c)
            except SystemExit:
                raise            # --http-only's "fail loudly" contract
            except Exception as e:
                failed.append(_c)
                print(f"  ! {_c} scrape failed: {e}")
        if failed and len(failed) == len(scrape_cities):
            raise SystemExit(f"every city failed to scrape ({', '.join(failed)})")
        if failed:
            print(f"note: {', '.join(failed)} failed; the other "
                  f"{len(scrape_cities) - len(failed)} still published")
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
                for _c in scrape_cities:
                    run_scrape(debug=args.debug, use_login=args.with_login,
                               http_only=args.http_only, bike_routes=not args.no_bike_routes,
                               bf_bike_routes=args.bike_routes_bf, city=_c)
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
              bf_bike_routes=args.bike_routes_bf, open_browser=not args.no_browser,
              cities=scrape_cities)
