"""
Shared pieces for the per-district build. One page (index.html) serves every district; everything
that differs between districts is data, written to data/<code>/ by build/district.py.

  districts/<code>.json   one config per district: chamber, number, and any hand-curated facts
  build/cache/            citywide downloads fetched once and reused by every district (git-ignored)
  data/<code>/            what the page loads for that district
"""
import json, os, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CACHE = ROOT / "build" / "cache"
DISTRICTS = ROOT / "districts"
CACHE.mkdir(exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0 (NYU Senior Seminar class project; contact ted.alcorn@gmail.com)"}
CENSUS_KEY = os.environ.get("CENSUS_API_KEY", "")
LEG_KEY = os.environ.get("NYSENATE_API_KEY", "")
NOW = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")
TODAY = NOW[:10]
STATE = "36"
SESSION = 2025

# NYC boroughs: how each dataset names them
BOROS = {"005": {"name": "Bronx", "boro": "BRONX", "county": "BRONX", "code": 2},
         "047": {"name": "Brooklyn", "boro": "BROOKLYN", "county": "KINGS", "code": 3},
         "061": {"name": "Manhattan", "boro": "MANHATTAN", "county": "NEW YORK", "code": 1},
         "081": {"name": "Queens", "boro": "QUEENS", "county": "QUEENS", "code": 4},
         "085": {"name": "Staten Island", "boro": "STATEN ISLAND", "county": "RICHMOND", "code": 5}}
CHAMBER = {"senate": {"label": "NY Senate", "long": "New York State Senate", "title": "Sen.", "role": "State Senator",
                      "tiger": "sldu", "acs_geo": "state legislative district (upper chamber)", "print": "S",
                      "page": "https://www.nysenate.gov/senators/{slug}", "api": "senate"},
           "assembly": {"label": "NY Assembly", "long": "New York State Assembly", "title": "Assembly Member", "role": "Assembly Member",
                        "tiger": "sldl", "acs_geo": "state legislative district (lower chamber)", "print": "A",
                        "page": "https://nyassembly.gov/mem/?ad={num:03d}", "api": "assembly"}}

def step(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def fetch(url, timeout=120, tries=4):
    """GET with retries: public data portals and flaky connections both drop requests now and then."""
    for i in range(tries):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read()
        except Exception as e:
            if i == tries - 1 or (isinstance(e, urllib.error.HTTPError) and e.code in (400, 403, 404)):
                raise
            time.sleep(5 * (i + 1))

def jfetch(url, timeout=120):
    return json.loads(fetch(url, timeout))

def cached(name, url, timeout=900):
    """Download once into build/cache/, return the path."""
    p = CACHE / name
    if not p.exists():
        p.write_bytes(fetch(url, timeout))
    return p

def cached_json(name, url, timeout=900):
    return json.loads(cached(name, url, timeout).read_text())

def soda(domain, dataset, where=None, select=None, limit=50000, order=":id"):
    rows, offset = [], 0
    while True:
        q = {"$limit": limit, "$offset": offset, "$order": order}
        if where:
            q["$where"] = where
        if select:
            q["$select"] = select
        page = jfetch(f"https://{domain}/resource/{dataset}.json?" + urllib.parse.urlencode(q))
        rows += page
        if len(page) < limit:
            return rows
        offset += limit

def census(year, params):
    url = f"https://api.census.gov/data/{year}/acs/acs5?" + params + (f"&key={CENSUS_KEY}" if CENSUS_KEY else "")
    rows = jfetch(url)
    return [dict(zip(rows[0], r)) for r in rows[1:]]

def load_config(code):
    return json.load(open(DISTRICTS / f"{code}.json"))

def all_configs():
    return [json.load(open(p)) for p in sorted(DISTRICTS.glob("*.json"))]

def district_dir(code):
    d = DATA / code
    d.mkdir(parents=True, exist_ok=True)
    return d

def members(chamber):
    """Current members of a chamber from the NY Senate Open Legislation API, keyed by district number."""
    d = jfetch(f"https://legislation.nysenate.gov/api/3/members/{SESSION}/{CHAMBER[chamber]['api']}?limit=300&full=true&key={LEG_KEY}")
    out = {}
    for m in d["result"]["items"]:
        if m.get("incumbent") or m["districtCode"] not in out:
            out[m["districtCode"]] = m
    return out

def bake(code):
    """data/<code>/portal_data.js from that district's JSON files."""
    d = district_dir(code)
    out = {}
    for name in ("district", "boundary", "trends", "people", "turf", "news", "headline", "member", "maplayers", "legislation", "community"):
        p = d / f"{name}.json"
        if p.exists():
            out[name] = json.load(open(p))
    with open(d / "portal_data.js", "w") as fh:
        fh.write("window.PORTAL_DATA = " + json.dumps(out, separators=(",", ":")) + ";")

def write_registry():
    """data/districts.js: the list the page's district picker shows."""
    rows = []
    for cfg in all_configs():
        p = DATA / cfg["code"] / "district.json"
        if p.exists():
            rows.append(json.load(open(p)))
    rows.sort(key=lambda r: (r["chamber"] != "senate", r["number"]))
    with open(DATA / "districts.js", "w") as fh:
        fh.write("window.DISTRICTS = " + json.dumps(rows, separators=(",", ":")) + ";")
    return rows
