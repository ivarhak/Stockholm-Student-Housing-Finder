<img width="1440" height="811" alt="Screenshot 2026-08-08 at 9 30 32 PM" src="https://github.com/user-attachments/assets/c0b398f4-ac31-46c2-b48a-c7dde8380526" />

# Stockholm Student Housing Finder

A local tool that checks what SSSB student housing is currently available,
works out the commute to your campus for each area, and shows it all on a map —
sorted by ascending queue days, with a refresh button and a desktop
notification when something new gets published. It also pulls in Stockholm's
**Bostadsförmedlingen** listings alongside SSSB's — that's where Svenska
Bostäder advertises its student apartments, and it needs no login at all
(it's a public JSON feed).

**No SSSB login required either.** The vacancy list is public — queue days
included — so out of the box this asks for no credentials at all. There's a
`--with-login` escape hatch if SSSB ever changes that; see section 3.

**Live version:** [ivarhak.github.io/Stockholm-Student-Housing-Finder](https://ivarhak.github.io/Stockholm-Student-Housing-Finder/)
— published from this repo and re-scraped every two hours. Read-only; run it
locally for desktop notifications and an on-demand Refresh. See section 5.

Two pieces:
- `sssb_kth_monitor.py` — runs on your machine: Selenium scraping for SSSB, a plain HTTP fetch for Bostadsförmedlingen, commute math, and a small local API.
- `sssb_kth_dashboard.html` — the UI. Served by the script itself locally, and published as-is to GitHub Pages, where it reads a pre-scraped `listings.json` instead of the API.

## 1. Why this runs on your laptop

It's a personal tool that watches your queues and pops desktop notifications,
so it lives on your machine — no server to pay for, nothing to deploy. It also
keeps Chrome available as a fallback for reading SSSB's listing page, which is
rendered by their JavaScript app rather than served as plain HTML.

This is deliberately a laptop tool. Running it on a phone was tried and
abandoned: iOS suspends background processes, so the dashboard's local server
dies the moment you switch to Safari, and working around that added more
complexity than the convenience was worth.

## 2. Setup

**Double-click `start.command`** (`start.bat` on Windows). That's it.

It creates the virtual environment, installs the requirements, starts the
server and opens the dashboard in your browser. Everything except the last two
steps is skipped once it's been done, so every run after the first is just
"double-click, dashboard opens". Any arguments you pass go straight through, so
`./start.command --interval 30` works too.

The only prerequisites are **Python 3.10+** and **Chrome** (or Chromium).
`webdriver-manager` fetches the matching driver by itself. Chrome is only the
fallback path for reading SSSB's listing page (see "How it reads SSSB" in
section 4), but keep it installed so that fallback exists.

There is no login step, and nothing ever prompts for input — so cron and Task
Scheduler runs need nothing extra either.

<details>
<summary>Prefer to do it by hand, or run it from an IDE?</summary>

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python sssb_kth_monitor.py      # no arguments = serve the dashboard
```

Running with no arguments serves the dashboard, which is what you want almost
every time. In PyCharm or similar, point a run configuration at
`sssb_kth_monitor.py` with no arguments and it'll do the same thing.
</details>

<details>
<summary>Putting it in your Dock or on your Desktop (macOS)</summary>

Drag `start.command` onto the Dock, or make an alias on the Desktop:

```bash
ln -s "$PWD/start.command" ~/Desktop/Student\ Housing.command
```

The alias keeps working when you `git pull`, since it points at the file rather
than copying it. To give it a nicer icon, select the file in Finder, press
Cmd+I, and paste an image onto the small icon in the top-left of the Info window.
</details>

<details>
<summary>If SSSB ever starts requiring a login again</summary>

Pass `--with-login`, and store credentials first with:

```bash
python sssb_kth_monitor.py --login
```

This prompts for your username and password (password input is hidden) and
asks if you want to save them to your computer's own secure keychain —
macOS Keychain, Windows Credential Locker, or Linux Secret Service,
depending on your OS, via the `keyring` package. They are **never written to
a file in this project folder**. `--forget-login` removes them again.

For unattended runs where there's no terminal to prompt on and no keyring
daemon (a headless Linux box, say), set `SSSB_USERNAME`/`SSSB_PASSWORD` as
environment variables in the crontab entry itself — the script reads those as
a fallback. That's fine security-wise since your crontab isn't part of this
project folder; just don't put those `export` lines in a script that lives in
here.

Note that `login()`'s field selectors have never been verified against SSSB's
real markup, because nobody has needed this path. If it fails, run
`--debug --with-login` and fix the selectors from `debug_page.html`.
</details>

Optional, for **real transit times** (otherwise you'll just get the
straight-line/bike estimate): get a free key for the **Resrobot** API at
[trafiklab.se](https://www.trafiklab.se/) (sign up → create a project →
add the "Resrobot v2.1" API), then:

```bash
export RESROBOT_API_KEY="your key"
```

## 3. If the scrape ever comes back empty

`scrape_listings()` finds real listings by looking for links containing
`refid=` in the URL (confirmed against a real SSSB booking link), then reads
the area/rent/size/queue-days/floor values out of the surrounding card text.
If it ever comes back empty while you can see real listings on the site
yourself:

```bash
python sssb_kth_monitor.py --debug
```

That runs a **visible** browser window (so you can watch what happens) and
saves the fully-rendered page to `debug_page.html`. Check whether that file
actually contains `refid=` anywhere (`grep -c "refid=" debug_page.html`) — if
SSSB changed their link format, that's the thing to update.

If instead the run reports listings but with empty queue days, SSSB may have
moved the "Ködagar" column behind a login again; try `--with-login` (see the
collapsed section above). The scrape prints a warning telling you so.

Bostadsförmedlingen needs no selector fixing at all, since
`fetch_bostadsformedlingen()` reads a plain JSON feed rather than scraped
markup. If its field names ever drift, the terminal prints the first ad's
actual keys on every run, so you can fix the candidate names in `_bf_field()`
from that alone.

## 4. Running it

**Dashboard** — double-click `start.command`, or from a terminal:
```bash
python sssb_kth_monitor.py          # no arguments needed
```
It scrapes if the saved listings are stale, serves the UI + API, and opens
**http://localhost:5055** in your browser by itself (`--no-browser` if you'd
rather it didn't). It then auto-checks SSSB in the background every 15 minutes
(change with `--interval 30`, don't go below 5 — see the rate-limiting note
below), and the dashboard polls for fresh results every 60 seconds, so you don't
need to click anything for it to notice new listings — you'll just see them
appear, plus the desktop notification. "Refresh" still triggers an immediate
check on demand instead of waiting for the next scheduled one.

While a check is running, **Refresh goes grey and tells you so**, with a running
timer under the button, and stays unclickable until it finishes. Clicking it
again wouldn't have made anything happen faster — scrapes run one at a time, so a
second click just queued up another full one behind the first. The page updates
itself the moment the data lands, so there's nothing to click. The same applies
while the background auto-check is running, and it survives reloading the page
mid-check.

If that startup scrape fails — SSSB briefly down, no network, Chrome missing —
the dashboard still comes up and serves the last listings it saved, and says in
the terminal what went wrong. The background auto-check retries on its own.

**One-off check** (scrapes once, saves, notifies if there's something new, exits):
```bash
python sssb_kth_monitor.py --once
```
This one *does* exit non-zero if the scrape fails, since it's what cron runs and
a silent success would be worse than a loud failure.

> Opening `sssb_kth_dashboard.html` directly as a file (or previewing it in
> Claude) shows example data with a banner saying so — the live version only
> works served from `http://localhost:5055` since that's what makes the
> `/api/...` calls same-origin.

### How it reads SSSB

A normal run first tries reading the vacancy list with a plain HTTP request,
and only launches Chrome if that comes back empty — which makes the usual case
noticeably faster, since no browser has to start at all. You don't need to
think about this; it's automatic.

`--http-only` forces the fast path and fails loudly instead of falling back,
which is handy for checking whether it still works. If the page ever does need
a real browser, that run tells you so and prints any API-looking URLs it found
in the page, since one of those is probably the endpoint the page fetches its
listings from.

## 5. The published version (GitHub Pages)

There's a **live read-only copy** of the dashboard published from this repo, so
you can look at current listings without running anything:

> **https://ivarhak.github.io/Stockholm-Student-Housing-Finder/**

A GitHub Actions workflow (`.github/workflows/publish.yml`) scrapes both sources
**every two hours**, writes the result next to the dashboard as `listings.json`,
and deploys the pair to Pages. The page picks up a new scrape on its own — a tab
left open re-checks the data file every 15 minutes.

It's the **same `sssb_kth_dashboard.html`** as the local version, not a second
copy to keep in sync. When no local server answers, it falls back to reading
`./listings.json` and adjusts: the Refresh button disappears (there's no server
to ask for a re-scrape) and a line in its place says how often the data updates
and where it comes from. Everything else — map, filters, campus picker, search,
both themes — works identically.

What the published version can't do, by nature:

- **No Refresh.** Re-scraping needs a token, and a token cannot live in a public
  static page. The schedule is the refresh.
- **No desktop notifications.** Those are the reason to run it locally.
- **Freshness is approximate.** GitHub's scheduled workflows are best-effort and
  commonly run 5–20 minutes late. Also note GitHub **disables cron workflows
  after 60 days without repo activity** — it emails you first.

<details>
<summary>Setting it up on your own fork</summary>

1. **Settings → Pages → Build and deployment → Source: GitHub Actions.** The
   deploy step fails without this; it's the one manual step.
2. Push. The workflow also runs on demand from the **Actions** tab
   (`workflow_dispatch`), which is the quickest way to test it.
3. The URL follows the repo name: `https://<user>.github.io/<repo>/`. Nothing in
   the code hardcodes it — the dashboard fetches its data with a relative path
   precisely so a project site served from a subpath works unchanged.

The workflow caches two files between runs, both worth understanding:
`bike_route_cache.json`, so 26 areas × 7 campuses of real cycling routes aren't
re-fetched from a free community service every two hours; and
`current_listings.json`, which is what new-listing diffing compares against —
without it, every run would start from nothing and mark all ~170 listings as NEW.

If a scrape fails the job fails deliberately, and Pages keeps serving the last
good deploy rather than publishing an empty map.
</details>

## 6. Getting notified automatically

If you leave `python sssb_kth_monitor.py --serve` running, you're already
covered — its background auto-check (every 15 min by default) fires the
same desktop notification on new listings as `--once` does. The cron/Task
Scheduler route below is only needed if you'd rather *not* keep the
dashboard process running all the time and just want periodic checks:

**macOS/Linux (cron)** — checks every 30 min:
```bash
crontab -e
# add:
*/30 * * * * cd /path/to/this-folder && /path/to/venv/bin/python sssb_kth_monitor.py --once >> cron.log 2>&1
```

**Windows (Task Scheduler)**: create a basic task that runs
`venv\Scripts\python.exe sssb_kth_monitor.py --once` every 30 minutes,
with "Start in" set to this folder.

## 7. Notes / known limitations

- **Light and dark themes.** The toggle sits in the header next to the title;
  dark is the default. Everything the page draws — including the map's area
  roundels, provider pins and campus markers — reads its colours from CSS
  custom properties, so switching repaints the map without re-rendering it. The
  chosen theme is remembered in `localStorage`, wrapped in a `try` so the page
  still loads where storage is blocked (it just won't persist there). That's the
  only browser storage this file touches; everything else about what's new since
  the last check is computed server-side.
- **Chain supermarkets on the map.** The **Shops** chip (next to the provider
  chips) draws a dot for every ICA, Coop, Willys, Hemköp, Lidl and City Gross in
  greater Stockholm, from OpenStreetMap. Off by default, and only drawn from zoom
  13 in — at city zoom a few hundred dots bury the area roundels, which are the
  point of the map. The chip goes dashed when it's on but you're zoomed too far
  out, so nothing looks broken.
  Every area also gets a **nearest shop** figure computed at scrape time, shown
  in the area's gauges and on rows in the cross-area list. It's straight-line
  distance, like the distance gauge next to it — a 300 m crow-flies shop can
  still be a 700 m walk.
  Shops are cached in `data/grocery_cache.json` for 30 days, since supermarkets
  don't move and re-fetching them every scrape would be pointless load on a free
  community service. `OVERPASS_URL` overrides the endpoint. If the lookup fails
  the run says so and carries on without dots.
- **Area colours match the tunnelbana.** A roundel's colour is the line its
  area sits on — North blue, South red, City green — matching the three lines
  drawn on the map in official SL colours. Gray × means an area exists but has
  nothing available (or nothing matching your filters) right now.
- **First run scrapes before serving.** `--serve` scrapes on startup unless the
  saved listings are newer than your `--interval`, and prints which it's doing.
  `data/current_listings.json` is regenerated output and deliberately *not* in
  the repo — it used to be, which meant a fresh clone was served month-old test
  listings that looked current. If the "updated" time in the top-right turns
  amber, nothing has scraped in over an hour.
- **Coordinates**: area coordinates are looked up automatically via
  OpenStreetMap (free, no key) and cached in `data/geocode_cache.json`. If a
  pin looks wrong on the map, open that file and hand-correct the
  `[lat, lon]` for that area.
- **Campus dropdown**: the "Campus" picker in the top bar re-centres the map
  and recomputes every commute from that school instead of KTH, so the max-
  commute slider, the sorting and the map counts all follow. KTH is the
  default. You can also click any of the other campus pins on the map to
  switch to it. Seven Stockholm schools are built in (KTH, SU, KI, SSE,
  Konstfack, KMH, KKH) — add more in `SCHOOLS` in `sssb_kth_monitor.py` and
  the dropdown picks them up automatically.
- **Bike times are real routes, not straight lines.** They come from
  [FOSSGIS's public Valhalla](https://valhalla1.openstreetmap.de/) routing
  service over OSM's cycling network — free, no key. Results are cached in
  `data/bike_route_cache.json`, so the first run is slow (it routes each area
  to each campus) and later runs are instant. A listing's row shows
  `12 min bike` for a routed time and `~12 min bike` for a fallback estimate,
  so you can always tell which you're looking at. If the service is
  unreachable the run says so and falls back to the old straight-line guess
  rather than failing; `--no-bike-routes` skips routing entirely.
- **Filtering far-away areas**: the "Max commute" slider in the dashboard
  hides areas beyond that many minutes (transit time if you set up
  Resrobot, otherwise the bike estimate). Flemingsberg in particular is
  quite far from central KTH — it'll likely get filtered out by default,
  which is probably what you want.
- **Queue-days and rent sliders**: next to "Max commute". Both start at
  "Any" (hiding nothing) and filter individual listings rather than whole
  areas, so the counts on the map roundels drop as you pull them down and an
  area shows the gray × once nothing in it still qualifies. Bostadsförmedlingen
  ads are never hidden by the queue-days slider — they publish an application
  deadline instead of a queue-days figure, so there's nothing to compare
  against. The "All listings" search tab ignores every slider, so it's
  the way to look at everything regardless of what's filtered.
- **The cog** (next to those sliders) opens a second row with the
  finer-grained filters: minimum size, a lowest/highest floor range
  (floor 0 = *bottenvåning*), and minimum contract length. They behave the
  same way as the main sliders — "Any" until you pull one in — and while any
  cog filter is engaged the cog shows an amber count, so you can collapse the
  panel without forgetting the map is still narrowed. "Reset these" clears
  just that row.
  - *Min contract* uses SSSB's "Max N år" badge, which caps how long you may
    hold the contract. Only a stated cap **below** your minimum is excluded;
    listings with no stated cap always pass, since no cap is the better case.
    In practice SSSB only ever seems to print "Max 4 år", so this slider
    mostly acts as a switch between "include those" and "exclude those".
- **Electricity** is shown, not filtered on. Cards that say "Elström ingår"
  display *el ingår* in the listing row (and it's searchable in the "All
  listings" tab), but there's no toggle for it: a card saying nothing about
  electricity isn't the same as one that excludes it, and Bostadsförmedlingen
  ads have no such field at all, so a filter would have quietly dropped that
  entire provider.
- **No elevator filter**: SSSB's vacancy list doesn't publish whether a
  building has a lift — the card only carries type, address, area, size,
  rent, move-in date, queue days, floor, and the two badges above. Adding one
  would mean opening each listing's own page during every scrape (76 extra
  page loads), and it's not confirmed that page states it either. The floor
  sliders are the practical stand-in for "no walk-up".
  Bostadsförmedlingen's feed *does* publish a lift flag (and a balcony one), so
  those ads show *elevator* / *balcony* on their row where stated — but for the
  same reason as electricity above, they're display-only, since a filter would
  have hidden every SSSB listing rather than narrowed anything.
- **If SSSB changes their site**, the selectors in step 3 are the only
  place you should need to touch.
- **Rate limiting**: don't drop the cron interval much below ~15 minutes —
  there's no need to hammer their login endpoint, and it's not clear how
  they'd react to it.
- **Bostadsförmedlingen: student housing only.** The feed
  (`bostad.stockholm.se/AllaAnnonser/`) carries the whole Stockholm rental
  queue; only ads flagged as student housing are kept. The flag is read
  tolerantly, so a renamed field shows up as a loud terminal warning listing
  the ad's real keys rather than silently returning zero listings. If the feed
  moves again, `BF_ADS_URL=<url>` overrides it without editing the script.
- **Clicking a Bostadsförmedlingen listing** opens that ad on their site. The
  feed publishes the link itself in a `Url` field, so that's what's used. Where
  it's missing, the script falls back to building
  `bostad.stockholm.se/bostad/<n>/`, where `<n>` is a year-prefixed 9-digit ad
  number — *not* the feed's `AnnonsId`, which is shorter and 404s — found by
  value shape since it's unclear which key holds it. The terminal reports the
  split between the two, and anything unresolvable links to a search page zoomed
  on the address rather than a dead page.
  `BF_LISTING_URL="https://…/{id}"` overrides the fallback template.
- **Bostadsförmedlingen ads use straight-line bike estimates**, not routed
  ones, so a scrape stays quick: there are ~100 of them and they rotate, so
  routing them all costs ~700 paced requests (about 9 minutes) per run. The 26
  SSSB areas are a fixed set and always routed. `--bike-routes-bf` opts in.
- **Desktop notifications** need nothing extra on macOS — the script uses the
  built-in `osascript`. (`plyer`, listed in requirements, needs a compiled
  `pyobjus` extension that often won't install; it's only a fallback for
  Windows now.) If a notification doesn't appear, the terminal prints what each
  path actually reported, plus the new listings themselves, so nothing is lost
  when the popup is. On macOS the usual cause is notification permission for
  whichever terminal app you ran it from: **System Settings → Notifications**.
- **Bostadsförmedlingen**: `fetch_bostadsformedlingen()` pulls Stockholm's
  city housing agency's public ad feed (`bostad.stockholm.se/AllaAnnonser/`)
  and keeps only the student ads — no login, no API key. Svenska Bostäder
  doesn't run a separate student queue of its own; their listings show up
  through this same feed. There's no landlord field in it, so the row is
  labelled with the *queue* the ad belongs to (`KoNamn`), which for the
  external queues is the landlord running it. Each ad carries its own
  coordinates, so these get individual purple pins on the map instead of
  SSSB's per-area dots, and sort by application deadline instead of queue
  days, since BF doesn't publish a queue-days figure up front.
- **Provider filter**: the dashboard's "Provider" chips (top bar) toggle
  SSSB and Bostadsförmedlingen independently of the SSSB-only "SSSB lines"
  filter. The "Queues to join" sidebar tab explains how to actually
  register for each queue.


Made by IvarHak on GitHub with the help of Claude Code
Feel free to use or modify however
