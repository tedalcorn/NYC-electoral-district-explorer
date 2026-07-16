#!/usr/bin/env python3
"""
SD-20 District Portal — data pipeline (class pilot).
Every output JSON carries a _prov block: source, url, retrieved, method, caveats.
HARD RULE: no invented data. A failed fetch produces an honest gap, never a guess.
"""
import json, os, re, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
DM = ROOT.parent / "Fall 2026 Reboot" / "district-map" / "data"

CENSUS_KEY = os.environ.get("CENSUS_API_KEY", "")  # Census API key (optional for low volume); set as env var / CI secret
NOW = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")
UA = {"User-Agent": "Mozilla/5.0 (NYU Senior Seminar class project; contact ted.alcorn@gmail.com)"}

def fetch(url, timeout=45):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def jfetch(url, timeout=45):
    return json.loads(fetch(url, timeout))

def step(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

# ---------------------------------------------------------------- 1. boundary
step("1/6 boundary: extract SD-20 from existing district-map build")
dj = json.load(open(DM / "districts.geojson"))
sd20 = [f for f in dj["features"] if f["properties"].get("code") == "SD-20"][0]
boundary = {
    "type": "FeatureCollection",
    "features": [sd20],
    "_prov": {
        "source": "U.S. Census TIGER/Line 2024, NY state senate districts (2022 lines)",
        "url": "https://www2.census.gov/geo/tiger/TIGER2024/SLDU/tl_2024_36_sldu.zip",
        "retrieved": "2026-07-02 (district-map build), reused " + NOW,
        "method": "Extracted from ../Fall 2026 Reboot/district-map/data/districts.geojson",
        "caveats": "These are the lines used in the 2024 elections.",
    },
}

# previous (pre-2022) lines — the district Myrie was first elected under in 2018.
step("1b/6 previous district lines: TIGER 2020 SLDU (pre-2022 redistricting)")
try:
    import geopandas as gpd
    from shapely.geometry import mapping
    prev_zip = DATA / "_tl_2020_36_sldu.zip"
    if not prev_zip.exists():
        prev_zip.write_bytes(fetch("https://www2.census.gov/geo/tiger/TIGER2020/SLDU/tl_2020_36_sldu.zip", timeout=120))
    gprev = gpd.read_file(prev_zip)
    grow = gprev[gprev["SLDUST"] == "020"].to_crs(4326)
    boundary["prev_lines"] = {
        "type": "Feature",
        "properties": {"note": "SD-20 under the 2012 lines (in effect 2012-2021)"},
        "geometry": mapping(grow.geometry.iloc[0]),
    }
    boundary["_prov"]["prev_lines_source"] = ("U.S. Census TIGER/Line 2020, NY SLDU (pre-2022 lines): "
                                              "https://www2.census.gov/geo/tiger/TIGER2020/SLDU/tl_2020_36_sldu.zip")
    step("   prev lines OK")
except Exception as e:
    boundary["prev_lines"] = None
    step(f"   prev lines FAILED ({e}) — dashed overlay omitted")

json.dump(boundary, open(DATA / "boundary.json", "w"))

# shapely geometry for overlap math
from shapely.geometry import shape
from shapely.ops import unary_union
g20 = shape(sd20["geometry"]).buffer(0)
minx, miny, maxx, maxy = g20.bounds

# ---------------------------------------------------------------- 1c. map layers: precincts + shootings
step("1c/8 map layers: police precincts + shootings within SD-20")
from shapely.geometry import Point
maplayers = {}
try:
    pj = jfetch("https://data.cityofnewyork.us/api/geospatial/y76i-bdw7?method=export&format=GeoJSON", timeout=90)
    feats = []
    for f in pj["features"]:
        try:
            if shape(f["geometry"]).buffer(0).intersects(g20):
                pk = next((v for k, v in f["properties"].items() if "precinct" in k.lower()), "?")
                feats.append({"type": "Feature", "properties": {"precinct": pk}, "geometry": f["geometry"]})
        except Exception:
            continue
    maplayers["precincts"] = {"type": "FeatureCollection", "features": feats}
    step(f"   precincts: {len(feats)} intersect SD-20")
except Exception as e:
    maplayers["precincts"] = None; step(f"   precincts FAILED ({e})")
try:
    where = "boro='BROOKLYN' AND occur_date>'2021-01-01T00:00:00'"
    rows = jfetch("https://data.cityofnewyork.us/resource/833y-fsy8.json?$limit=50000&$where=" + urllib.parse.quote(where), timeout=90)
    pts, n_nocoord = [], 0
    for r in rows:
        try:
            lat, lon = float(r["latitude"]), float(r["longitude"])
        except Exception:
            n_nocoord += 1; continue
        if g20.contains(Point(lon, lat)):
            pts.append({"lat": round(lat, 5), "lon": round(lon, 5), "date": (r.get("occur_date") or "")[:10],
                        "murder": r.get("statistical_murder_flag") in ("true", True, "Y"),
                        "precinct": r.get("precinct")})
    maplayers["shootings"] = pts
    maplayers["shootings_stats"] = {"in_district": len(pts), "brooklyn_since_2021": len(rows), "brooklyn_missing_coords": n_nocoord}
    step(f"   shootings: {len(pts)} within SD-20 (2021+); {n_nocoord}/{len(rows)} Brooklyn records lacked coordinates")
except Exception as e:
    maplayers["shootings"] = None; step(f"   shootings FAILED ({e})")
# --- transit: subway stations
try:
    srows = jfetch("https://data.ny.gov/resource/39hk-dx4f.json?$limit=2500", timeout=90)
    subs = []
    for r in srows:
        try:
            lat, lon = float(r["gtfs_latitude"]), float(r["gtfs_longitude"])
        except Exception:
            continue
        if g20.contains(Point(lon, lat)):
            subs.append({"lat": round(lat, 5), "lon": round(lon, 5), "name": r.get("stop_name"), "routes": r.get("daytime_routes")})
    maplayers["subway"] = subs
    step(f"   subway: {len(subs)} stations in SD-20")
except Exception as e:
    maplayers["subway"] = None; step(f"   subway FAILED ({e})")
# --- transit: Citi Bike docks (GBFS)
try:
    cbs = jfetch("https://gbfs.citibikenyc.com/gbfs/en/station_information.json")["data"]["stations"]
    cb = []
    for s in cbs:
        try:
            lat, lon = float(s["lat"]), float(s["lon"])
        except Exception:
            continue
        if g20.contains(Point(lon, lat)):
            cb.append({"lat": round(lat, 5), "lon": round(lon, 5), "name": s.get("name")})
    maplayers["citibike"] = cb
    step(f"   citibike: {len(cb)} docks in SD-20")
except Exception as e:
    maplayers["citibike"] = None; step(f"   citibike FAILED ({e})")
# --- transit: MTA bus routes (clip lines to district)
try:
    from shapely.geometry import mapping as _mp
    from shapely.ops import unary_union as _uu
    bj = jfetch("https://data.ny.gov/api/geospatial/bzwk-3hb4?method=export&format=GeoJSON", timeout=120)
    byroute = {}  # dissolve the many small segments into one line per route
    for f in bj["features"]:
        try:
            gg = shape(f["geometry"])
            if not gg.intersects(g20):
                continue
            clip = gg.intersection(g20.buffer(0.0015))
            if clip.is_empty:
                continue
            rn = f["properties"].get("route_short_name") or f["properties"].get("route_id") or "?"
            byroute.setdefault(rn, []).append(clip)
        except Exception:
            continue
    bfeats = [{"type": "Feature", "properties": {"route": rn}, "geometry": _mp(_uu(geoms).simplify(0.0002))}
              for rn, geoms in sorted(byroute.items())]
    maplayers["bus"] = {"type": "FeatureCollection", "features": bfeats}
    step(f"   bus: {len(bfeats)} routes through SD-20")
except Exception as e:
    maplayers["bus"] = None; step(f"   bus FAILED ({e})")
# --- housing: residential evictions executed 2023+
try:
    where = "borough='BROOKLYN' AND residential_commercial_ind='Residential' AND executed_date>'2023-01-01'"
    erows = jfetch("https://data.cityofnewyork.us/resource/6z8x-wfk4.json?$limit=60000&$where=" + urllib.parse.quote(where), timeout=90)
    ev = []
    for r in erows:
        try:
            lat, lon = float(r["latitude"]), float(r["longitude"])
        except Exception:
            continue
        if g20.contains(Point(lon, lat)):
            ev.append({"lat": round(lat, 5), "lon": round(lon, 5), "date": (r.get("executed_date") or "")[:10], "addr": r.get("eviction_address")})
    maplayers["evictions"] = ev
    step(f"   evictions: {len(ev)} residential in SD-20 (2023+)")
except Exception as e:
    maplayers["evictions"] = None; step(f"   evictions FAILED ({e})")
maplayers["_prov"] = {
    "sources": {
        "precincts": "NYC Open Data, Police Precincts (y76i-bdw7)",
        "shootings": "NYC Open Data, NYPD Shooting Incident Data — Historic (833y-fsy8)",
        "subway": "MTA / NY State Open Data, MTA Subway Stations (39hk-dx4f)",
        "bus": "MTA / NY State Open Data, MTA Bus Routes (bzwk-3hb4)",
        "citibike": "Citi Bike GBFS station_information feed",
        "evictions": "NYC Open Data, Evictions (6z8x-wfk4) — residential, executed",
    },
    "retrieved": NOW,
    "method": ("Each layer is clipped to the SD-20 boundary (points kept if inside; bus lines clipped to the district). "
               "Shootings: Brooklyn incidents since 2021. Evictions: residential, executed, since 2023. All official reports."),
    "caveats": ("Points are shown at the location the agency recorded; records missing coordinates are dropped, not guessed. "
                "Precinct lines are not district lines. Bus segments show routes passing through, not full routes."),
}
json.dump(maplayers, open(DATA / "maplayers.json", "w"))

# ---------------------------------------------------------------- 2. ACS time series
step("2/6 ACS time series: population + median household income, 2015-2023")
years, pop_series, inc_series, api_urls = [], [], [], {}
for yr in range(2015, 2024):
    url = (f"https://api.census.gov/data/{yr}/acs/acs5"
           f"?get=NAME,B01003_001E,B19013_001E"
           f"&for=state%20legislative%20district%20(upper%20chamber):020"
           f"&in=state:36&key={CENSUS_KEY}")
    try:
        rows = jfetch(url)
        _, pop, inc = rows[1][0], rows[1][1], rows[1][2]
        years.append(yr)
        pop_series.append(int(pop) if pop not in (None, "") else None)
        inc_series.append(int(inc) if inc not in (None, "") else None)
        api_urls[yr] = url.replace(CENSUS_KEY, "YOUR_KEY")
        step(f"   {yr}: pop {pop}, income {inc}")
    except Exception as e:
        step(f"   {yr}: FAILED ({e}) — left blank")
        years.append(yr); pop_series.append(None); inc_series.append(None)
trends = {
    "years": years, "population": pop_series, "median_hh_income": inc_series,
    "boundary_break_year": 2022,
    "_prov": {
        "source": "U.S. Census Bureau, ACS 5-year estimates (tables B01003, B19013), via Census API",
        "url": "https://api.census.gov/data/2023/acs/acs5 (per-year calls; substitute year)",
        "example_call": api_urls.get(2023, ""),
        "retrieved": NOW,
        "method": "Geography: state legislative district (upper chamber) 020, NY.",
        "caveats": ("CRITICAL: senate district lines were REDRAWN effective 2022. "
                    "Estimates before/after the break describe DIFFERENT territory — the apparent "
                    "jump at the break is partly a boundary artifact, not population change. "
                    "Each ACS 5-yr point also overlaps 4 years with its neighbors: adjacent points "
                    "are not independent. Income in nominal dollars (not inflation-adjusted)."),
    },
}
json.dump(trends, open(DATA / "trends.json", "w"))

# ---------------------------------------------------------------- 3. people: age + race, SD-20 vs NYC
step("3/6 demographics: age structure + race/ethnicity, SD-20 vs NYC")
AGE_GROUPS = [  # B01001 male idx, female idx ranges (variable numbers)
    ("Under 18",  list(range(3, 7)),   list(range(27, 31))),
    ("18-34",     list(range(7, 13)),  list(range(31, 37))),
    ("35-54",     list(range(13, 17)), list(range(37, 41))),
    ("55-74",     list(range(17, 23)), list(range(41, 47))),
    ("75+",       list(range(23, 26)), list(range(47, 50))),
]
def age_vars():
    v = []
    for _, m, f in AGE_GROUPS:
        v += [f"B01001_{i:03d}E" for i in m] + [f"B01001_{i:03d}E" for i in f]
    return v

def get_acs(varlist, geo):
    url = (f"https://api.census.gov/data/2023/acs/acs5?get={','.join(varlist)}"
           f"&for={geo}&key={CENSUS_KEY}")
    rows = jfetch(url)
    hdr = rows[0]
    out = []
    for row in rows[1:]:
        out.append({hdr[i]: row[i] for i in range(len(hdr))})
    return out

def agg_ages(recs):
    tot = {g[0]: 0 for g in AGE_GROUPS}
    for rec in recs:
        for name, m, f in AGE_GROUPS:
            for i in m + f:
                tot[name] += int(rec[f"B01001_{i:03d}E"])
    return tot

RACE_VARS = {"total": "B03002_001E", "white_nh": "B03002_003E", "black_nh": "B03002_004E",
             "asian_nh": "B03002_006E", "hispanic": "B03002_012E"}

def agg_race(recs):
    t = {k: sum(int(r[v]) for r in recs) for k, v in RACE_VARS.items()}
    tot = t["total"]
    other = tot - t["white_nh"] - t["black_nh"] - t["asian_nh"] - t["hispanic"]
    return {"White (non-Hisp.)": t["white_nh"] / tot * 100,
            "Black (non-Hisp.)": t["black_nh"] / tot * 100,
            "Hispanic (any race)": t["hispanic"] / tot * 100,
            "Asian (non-Hisp.)": t["asian_nh"] / tot * 100,
            "Other / multiracial": other / tot * 100}

people = {"_prov": {
    "source": "U.S. Census Bureau, ACS 2019-2023 5-year (tables B01001 age/sex, B03002 race/ethnicity), via Census API",
    "url": "https://data.census.gov/table/ACSDT5Y2023.B03002?g=610XX00US36020",
    "retrieved": NOW,
    "method": ("SD-20 = state legislative district (upper) 020. 'NYC' = sum of the five county "
               "geographies (Bronx 005, Kings 047, New York 061, Queens 081, Richmond 085). "
               "Shares computed from raw counts."),
    "caveats": ("ACS = survey ESTIMATES with margins of error (not shown here — that omission is itself "
                "a discussion point). Race categories follow B03002: White/Black/Asian are alone, "
                "non-Hispanic; Hispanic is all races."),
}}
try:
    sd_age = agg_ages(get_acs(age_vars(), "state%20legislative%20district%20(upper%20chamber):020&in=state:36"))
    nyc_age = agg_ages(get_acs(age_vars(), "county:005,047,061,081,085&in=state:36"))
    sd_tot, nyc_tot = sum(sd_age.values()), sum(nyc_age.values())
    people["age"] = {
        "groups": [g[0] for g in AGE_GROUPS],
        "sd20_pct": [sd_age[g[0]] / sd_tot * 100 for g in AGE_GROUPS],
        "nyc_pct": [nyc_age[g[0]] / nyc_tot * 100 for g in AGE_GROUPS],
    }
    step("   age OK")
except Exception as e:
    people["age"] = None; step(f"   age FAILED ({e}) — pane will show the gap")
try:
    sd_race = agg_race(get_acs(list(RACE_VARS.values()), "state%20legislative%20district%20(upper%20chamber):020&in=state:36"))
    nyc_race = agg_race(get_acs(list(RACE_VARS.values()), "county:005,047,061,081,085&in=state:36"))
    people["race"] = {"labels": list(sd_race.keys()),
                      "sd20_pct": [round(v, 1) for v in sd_race.values()],
                      "nyc_pct": [round(v, 1) for v in nyc_race.values()]}
    step("   race OK")
except Exception as e:
    people["race"] = None; step(f"   race FAILED ({e}) — pane will show the gap")
json.dump(people, open(DATA / "people.json", "w"))

# ---------------------------------------------------------------- 4. overlapping districts
step("4/6 shared turf: council (local file) + assembly & congressional (TIGERweb)")
def overlap_table(features, id_fn, label_fn):
    rows = []
    for f in features:
        try:
            g = shape(f["geometry"]).buffer(0)
            inter = g20.intersection(g)
            share = inter.area / g20.area * 100
            if share >= 1.0:
                rows.append({"district": id_fn(f), "label": label_fn(f), "pct_of_sd20": round(share, 1)})
        except Exception:
            continue
    return sorted(rows, key=lambda r: -r["pct_of_sd20"])

turf = {"_prov": {
    "sources": {
        "council": "NYC OpenData 'City Council Districts' (872g-cjhh), 2023 lines — reused from district-map build (2026-07-02)",
        "assembly": "Census TIGERweb, State Legislative Districts – Lower (2024 lines), queried " + NOW,
        "congress": "Census TIGERweb, 119th Congressional Districts, queried " + NOW,
    },
    "method": ("Geometric intersection (shapely) of each district with SD-20; showing districts covering "
               "≥1% of SD-20's area. Percentages are shares of SD-20's AREA, not its population — "
               "a worthwhile distinction to discuss."),
    "caveats": "Member names must be verified against official directories (links in pane) — offices change hands.",
}}
# council (local)
try:
    cj = json.load(open(DM / "council_raw.geojson"))
    turf["council"] = overlap_table(
        cj["features"],
        lambda f: "CD-" + str(f["properties"]["coundist"]),
        lambda f: "NYC Council District " + str(f["properties"]["coundist"]))
    step(f"   council: {len(turf['council'])} overlapping")
except Exception as e:
    turf["council"] = None; step(f"   council FAILED ({e})")

def tigerweb_layer(name_contains):
    svc = jfetch("https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Legislative/MapServer?f=json")
    for lyr in svc["layers"]:
        if name_contains.lower() in lyr["name"].lower() and lyr.get("type") == "Feature Layer":
            return lyr["id"]
    raise RuntimeError(f"layer {name_contains} not found")

def tigerweb_query(layer_id):
    env = urllib.parse.quote(json.dumps({"xmin": minx, "ymin": miny, "xmax": maxx, "ymax": maxy,
                                         "spatialReference": {"wkid": 4326}}))
    url = (f"https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Legislative/MapServer/{layer_id}/query"
           f"?geometry={env}&geometryType=esriGeometryEnvelope&inSR=4326&spatialRel=esriSpatialRelIntersects"
           f"&outFields=BASENAME,NAME,GEOID&returnGeometry=true&outSR=4326&f=geojson")
    return jfetch(url, timeout=90)

for key, layername, prefix in [("assembly", "State Legislative Districts - Lower", "AD-"),
                               ("congress", "Congressional Districts", "NY-")]:
    try:
        lid = tigerweb_layer(layername)
        fc = tigerweb_query(lid)
        turf[key] = overlap_table(
            fc["features"],
            lambda f, p=prefix: p + str(f["properties"].get("BASENAME", "?")),
            lambda f: f["properties"].get("NAME", "?"))
        step(f"   {key}: {len(turf[key])} overlapping (layer {lid})")
    except Exception as e:
        turf[key] = None; step(f"   {key} FAILED ({e})")
# member names: verified 2026-07-14 against council.nyc.gov / nyassembly.gov / GovTrack.
# Kept as a static lookup — RE-VERIFY after any election or special election.
NAMES = {
    "council": {35: "Crystal Hudson", 36: "Chi Ossé", 39: "Shahana Hanif", 40: "Rita Joseph", 41: "Darlene Mealy"},
    "assembly": {42: "Rodneyse Bichotte Hermelyn", 43: "Brian Cunningham", 44: "Robert C. Carroll",
                 51: "Marcela Mitaynes", 52: "Jo Anne Simon", 55: "Latrice M. Walker", 56: "Stefani Zinerman",
                 57: "Phara Souffrant Forrest", 58: "Monique Chandler-Waterman"},
    "congress": {8: "Hakeem Jeffries", 9: "Yvette Clarke", 10: "Daniel Goldman"},
}
LINKS = {"council": "https://council.nyc.gov/district-{n}/",
         "assembly": "https://nyassembly.gov/mem/?ad={n:03d}",
         "congress": "https://www.govtrack.us/congress/members/NY/{n}"}
for level in ("council", "assembly", "congress"):
    for row in (turf.get(level) or []):
        n = int(row["district"].split("-")[1])
        row["member"] = NAMES[level].get(n)   # None -> pane shows "look up ->" link
        row["link"] = LINKS[level].format(n=n)
        row["name_source"] = "verified 2026-07-14 (see _prov)"
json.dump(turf, open(DATA / "turf.json", "w"))

# ---------------------------------------------------------------- 5. local news (last ~6 months, topic-tagged)
step("5/6 local news: Google News RSS, neighborhood + member queries, last 180 days")
from email.utils import parsedate_to_datetime
from collections import Counter
try:
    from zoneinfo import ZoneInfo
    ET_TZ = ZoneInfo("America/New_York")
except Exception:
    ET_TZ = timezone.utc
NEWS_QUERIES = [
    '"Zellnor Myrie"',
    '"Crown Heights" Brooklyn',
    '"Prospect Heights" Brooklyn',
    '"Prospect Lefferts Gardens"',
    '"East Flatbush"',
    '"Wingate" Brooklyn',
    '"Ditmas Park"',
    '"Prospect Park" Brooklyn',
    '"Flatbush" Brooklyn',
    '"Lefferts" Brooklyn',
]
TOPICS = [  # (label, keywords) — first match wins; keyword-based and approximate
    ("Crime & safety", ["shooting", "shot", "police", "nypd", "crime", "arrest", "violence", "gun",
                        "stabbing", "robbery", "assault", "killed", "homicide", "shoot"]),
    ("Housing & development", ["housing", "rent", "tenant", "eviction", "affordable", "develop", "rezon",
                              "construction", "apartment", "real estate", "landlord", "condo", "nycha",
                              "zoning", "deed", "building", "brownstone"]),
    ("Schools & education", ["school", "student", "teacher", "education", "college", "university", "pre-k", "cuny"]),
    ("Transit & streets", ["subway", "mta", "train", "transit", "bike", "traffic", "congestion pricing", "bus stop", "citi bike"]),
    ("Health", ["health", "hospital", "covid", "clinic", "medical", "mental health"]),
    ("Business & economy", ["business", "restaurant", "store", "shop", "jobs", "retail", "opening", "cafe", "bakery"]),
    ("Politics & elections", ["myrie", "senator", "mayor", "council", "assembly", "election", "primary",
                             "campaign", "bill", "legislation", "mamdani", "governor", "adams"]),
]
def tag(title):
    t = title.lower()
    for label, kws in TOPICS:
        if any(k in t for k in kws):
            return label
    return "Community & other"

CUTOFF_DAYS = 180
NEWS_EXCLUDE_OUTLETS = {"MaxPreps"}  # high-school sports box scores — not district news
articles, seen = [], set()
for q in NEWS_QUERIES:
    try:
        url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(q) + "&hl=en-US&gl=US&ceid=US:en"
        root = ET.fromstring(fetch(url, timeout=30))
        kept = 0
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            key = title.lower()
            if not title or key in seen:
                continue
            try:
                dt = parsedate_to_datetime(it.findtext("pubDate") or "")
            except Exception:
                continue
            age = (datetime.now(timezone.utc) - dt).days
            if age > CUTOFF_DAYS or age < 0:
                continue
            seen.add(key)
            dt_et = dt.astimezone(ET_TZ)
            src = it.find("source")
            src_text = (src.text if src is not None else "").strip() or "Unknown outlet"
            if src_text in NEWS_EXCLUDE_OUTLETS:
                continue
            if src_text != "Unknown outlet" and title.endswith(" - " + src_text):
                title = title[:-(len(src_text) + 3)].strip()  # drop Google's trailing " - Outlet"
            articles.append({
                "title": title,
                "link": (it.findtext("link") or "").strip(),
                "outlet": src_text,
                "date_iso": dt_et.strftime("%Y-%m-%dT%H:%M"),
                "date_display": dt_et.strftime("%d/%m/%y, %-H:%M"),
                "topic": tag(title),
                "query": q,
            })
            kept += 1
        step(f"   {q}: +{kept} in last {CUTOFF_DAYS}d")
        time.sleep(1.5)
    except Exception as e:
        step(f"   {q} FAILED ({e})")
articles.sort(key=lambda a: a["date_iso"], reverse=True)
topic_counts = Counter(a["topic"] for a in articles)
outlet_counts = Counter(a["outlet"] for a in articles)
news = {
    "retrieved": NOW,
    "window_days": CUTOFF_DAYS,
    "total": len(articles),
    "topic_counts": dict(topic_counts.most_common()),
    "top_outlets": outlet_counts.most_common(12),
    "queries": NEWS_QUERIES,
    "articles": articles[:200],
    "_prov": {
        "source": "Google News RSS search — one query per SD-20 neighborhood plus one for Sen. Myrie",
        "url": "https://news.google.com/rss/search?q=<query>",
        "retrieved": NOW,
        "method": (f"{len(NEWS_QUERIES)} queries (listed in 'queries'); items deduplicated by headline, filtered to the "
                   f"last {CUTOFF_DAYS} days by publication date, tagged to a topic by keyword match, sorted newest first. "
                   f"{len(articles)} unique articles collected; the page shows the {min(200, len(articles))} most recent."),
        "caveats": ("Google News is relevance-ranked and caps each query near 100 results, so this is a broad sample, "
                    "not a census of local coverage; a neighborhood name-match does not guarantee the story is inside "
                    "SD-20; and topic tags are automated keyword guesses. A fully comprehensive feed would pull each "
                    "outlet's own RSS and verify location."),
    },
}
json.dump(news, open(DATA / "news.json", "w"))
step(f"   collected {len(articles)} articles; topics: {dict(topic_counts)}")

# ---------------------------------------------------------------- 6. copy race/income headline + turnout from district-map
step("6/6 headline stats + turnout: reuse verified district-map data")
sdm = json.load(open(DM / "state_demographics.json"))["SD-20"]
tout = json.load(open(DM / "turnout.json"))
headline = {
    "stats": sdm,
    "turnout": tout.get("SD-20"),
    "_prov": {
        "source": "Census Reporter ACS 2020-2024 5-yr (demographics); NYC BOE certified recaps (turnout) — compiled 2026-07-02 for the district-comparison map",
        "url": "https://censusreporter.org/profiles/61000US36020-state-senate-district-20-ny/",
        "retrieved": "2026-07-02, reused " + NOW,
        "method": "See ../Fall 2026 Reboot/district-map/SOURCES_AND_CAVEATS.txt for full notes.",
        "caveats": "Headline demographics use ACS 2020-24; the charts on this page use ACS 2019-23 — two adjacent vintages, so small differences between them are expected.",
    },
}

# citywide comparison values (same 2020-24 vintage as the district headline)
step("6b/6 NYC comparison: median income, age, foreign-born (ACS 2020-24, place 51000)")
try:
    cu = ("https://api.census.gov/data/2024/acs/acs5?get=B19013_001E,B01002_001E,B05002_001E,B05002_013E"
          f"&for=place:51000&in=state:36&key={CENSUS_KEY}")
    cr = jfetch(cu)[1]
    headline["city"] = {
        "name": "New York city",
        "median_hh_income": int(cr[0]),
        "median_age": round(float(cr[1]), 1),
        "pct_foreign_born": round(int(cr[3]) / int(cr[2]) * 100, 1),
        "source": "U.S. Census Bureau, ACS 2020-2024 5-year, place 'New York city, NY' (tables B19013, B01002, B05002), via Census API",
        "url": cu.replace(CENSUS_KEY, "YOUR_KEY"),
    }
    step(f"   city OK: ${headline['city']['median_hh_income']:,}, age {headline['city']['median_age']}, {headline['city']['pct_foreign_born']}% fb")
except Exception as e:
    headline["city"] = None
    step(f"   city FAILED ({e})")
json.dump(headline, open(DATA / "headline.json", "w"))

# ---------------------------------------------------------------- 7. member background (static, verified)
step("7/7 member background: Myrie elections + priorities (verified 2026-07-15, sourced)")
member = {
    "name": "Zellnor Myrie",
    "office": "New York State Senator, District 20 (since January 2019)",
    "wikipedia": "https://en.wikipedia.org/wiki/Zellnor_Myrie",
    "senate_page": "https://www.nysenate.gov/senators/zellnor-myrie",
    "legislation_url": "https://www.nysenate.gov/senators/zellnor-myrie/legislation",
    "committees": ["Codes (Chair)", "Children and Families", "Consumer Protection",
                   "Elections", "Health", "Judiciary", "Rules"],
    "social": {
        "official": [
            {"platform": "X", "url": "https://x.com/SenatorMyrie"},
            {"platform": "Instagram", "url": "https://www.instagram.com/senatormyrie/"},
            {"platform": "Facebook", "url": "https://www.facebook.com/SenatorMyrie/"},
        ],
        "other": [
            {"platform": "X (personal)", "url": "https://x.com/zellnor4ny"},
            {"platform": "Bluesky", "url": "https://bsky.app/profile/zellnor.bsky.social"},
        ],
    },
    "elections": [
        {"year": 2018, "race": "Democratic primary", "result": "Won", "self": 23784, "self_pct": 53.9,
         "opp": "Jesse Hamilton (incumbent)", "opp_votes": 20266, "opp_pct": 45.9},
        {"year": 2018, "race": "General", "result": "Won", "self": 73174, "self_pct": 92.6,
         "opp": "Jesse Hamilton", "opp_votes": 5728, "opp_pct": 7.3},
        {"year": 2020, "race": "General", "result": "Won", "self": 99491, "self_pct": 97.3,
         "opp": "Tucker Coburn (Libertarian)", "opp_votes": 2570, "opp_pct": 2.5},
        {"year": 2022, "race": "General", "result": "Won", "self": 80036, "self_pct": 99.5,
         "opp": "Effectively unopposed", "opp_votes": None, "opp_pct": None},
        {"year": 2024, "race": "General", "result": "Won", "self": 107498, "self_pct": 99.3,
         "opp": "Effectively unopposed", "opp_votes": None, "opp_pct": None},
        {"year": 2025, "race": "NYC Democratic mayoral primary", "result": "Lost (6th of 11)", "self": 10593, "self_pct": 1.0,
         "opp": "Zohran Mamdani (won)", "opp_votes": None, "opp_pct": None,
         "note": "First-round votes; eliminated in round 2 of the ranked-choice tabulation."},
    ],
    "priorities": [
        ["Voting rights", "Lead sponsor of the John R. Lewis Voting Rights Act of New York (signed 2022).",
         "https://www.nysenate.gov/legislation/bills/2021/S1046"],
        ["Criminal justice", "Sponsor of the Clean Slate Act, sealing certain convictions after a waiting period (signed 2023).",
         "https://www.cityandstateny.com/politics/2024/05/zellnor-myrie-may-run-mayor-strong-legislative-record/396413/"],
        ["Housing", "Backed the 2019 Housing Stability and Tenant Protection Act and COVID-era eviction protections; work on deed theft and homeownership.",
         "https://www.nysenate.gov/senators/zellnor-myrie/about"],
        ["Gun violence", "Authored the Community Violence Intervention Act (2021) and a law treating certain illegal gun sales as a public nuisance.",
         "https://en.wikipedia.org/wiki/Zellnor_Myrie"],
    ],
    "_prov": {
        "source": "Elections: Wikipedia tables citing NYC/NYS BOE certified results, cross-checked against the NYC BOE ranked-choice tabulation (2025). Priorities: NY Senate bill pages, Governor's office, City & State.",
        "url": "https://en.wikipedia.org/wiki/New_York%27s_20th_State_Senate_district",
        "retrieved": "2026-07-15",
        "method": ("Compiled by web research, 2026-07-15. Vote totals combine a candidate's party and minor-party "
                   "ballot lines where they appeared on more than one (NY fusion voting). Committees and social "
                   "accounts verified 2026-07-15 against nysenate.gov; the expired campaign site zellnor.nyc was removed."),
        "caveats": ("2020/2022/2024 primaries were uncontested (no primary tally exists). In 2022 and 2024 Myrie ran "
                    "effectively unopposed in the general. Committee assignments change by session — verify the current "
                    "role on his Senate page before citing."),
    },
}
json.dump(member, open(DATA / "member.json", "w"))

# ---------------------------------------------------------------- 8. legislation (NY Senate Open Legislation API)
step("8/8 legislation: recent Senate bills sponsored by Myrie (memberId 1228)")
LEG_KEY = os.environ.get("NYSENATE_API_KEY", "")  # NY Senate Open Legislation key (required for the legislation feed); set as env var / CI secret
SESSION = 2025
LEG_TOPICS = [  # (label, keywords) — matched on title + summary; first match wins
    ("Housing", ["housing", "rent", "tenant", "eviction", "landlord", "homeowner", "deed", "mortgage",
                 "foreclos", "dwelling", "co-op", "condominium", "real property", "shelter"]),
    ("Criminal justice", ["crime", "criminal", "sentenc", "parole", "probation", "incarcerat", "police",
                          "arrest", "prison", "jail", "conviction", "seal", "reentry", "re-entry", "correction",
                          "penal", "firearm", " gun", "weapon", "bail"]),
    ("Voting & elections", ["election", "voter", "voting", "ballot", "campaign finance", "redistrict", "poll"]),
    ("Education", ["school", "student", "education", "teacher", "cuny", "suny", "pupil", "college", "tuition"]),
    ("Health", ["health", "medicaid", "hospital", "medical", "mental health", "insurance", "patient",
                "disease", "prescription", "nursing"]),
    ("Economy & labor", ["employ", "worker", "wage", "labor", "business", "tax", "unemploy", "pension",
                         "consumer", "economic", "minimum wage"]),
    ("Environment & transit", ["environment", "climate", "energy", "emission", "transit", "mta", "transportation",
                              "vehicle", "pollut", "water", " park", "recycl"]),
    ("Civil rights", ["discriminat", "civil right", "human right", "equal", "gender", "reproduct", "immigrant", "disability"]),
]
def leg_topic(text):
    t = (text or "").lower()
    for label, kws in LEG_TOPICS:
        if any(k in t for k in kws):
            return label
    return "Government & other"

def leg_search(term, cap=1200):
    items, offset, limit, total = [], 1, 50, 0
    while True:
        u = ("https://legislation.nysenate.gov/api/3/bills/search?term=" + urllib.parse.quote(term) +
             f"&limit={limit}&offset={offset}&key=" + LEG_KEY)
        d = jfetch(u, 60)
        if not d.get("success"):
            break
        total = d.get("total", 0)
        its = d["result"]["items"]
        items += its
        if len(its) < limit or len(items) >= min(total, cap):
            break
        offset += limit
        time.sleep(0.3)
    return items, total

def leg_row(r, role):
    st = r.get("status") or {}
    acts = (r.get("actions") or {}).get("items") or []
    pn = r.get("basePrintNo")
    return {"printNo": pn, "title": r.get("title") or "",
            "role": role,
            "introduced": acts[0]["date"] if acts else "",
            "status": st.get("statusDesc") or "", "statusDate": st.get("actionDate") or "",
            "topic": leg_topic((r.get("title") or "") + " " + (r.get("summary") or "")),
            "url": f"https://www.nysenate.gov/legislation/bills/{SESSION}/{pn}"}

legislation = {"session": SESSION}
try:
    prime_items, n_prime = leg_search(f"sponsor.member.memberId:1228 AND session:{SESSION} AND basePrintNo:S*")
    prime_no = {it["result"]["basePrintNo"] for it in prime_items}
    co_items, n_co = leg_search(f"amendments.items.\\*.coSponsors.items.shortName:MYRIE AND session:{SESSION} AND basePrintNo:S*")
    bills, seen = [], set()
    for it in prime_items:
        r = it["result"]
        if r["basePrintNo"] in seen:
            continue
        seen.add(r["basePrintNo"]); bills.append(leg_row(r, "Prime sponsor"))
    for it in co_items:
        r = it["result"]
        if r["basePrintNo"] in seen or r["basePrintNo"] in prime_no:
            continue
        seen.add(r["basePrintNo"]); bills.append(leg_row(r, "Co-sponsor"))
    bills.sort(key=lambda b: b["introduced"], reverse=True)
    legislation["n_leads"], legislation["n_cosponsor"] = len(prime_no), sum(1 for b in bills if b["role"] == "Co-sponsor")
    legislation["bills"] = bills
    step(f"   {legislation['n_leads']} leads + {legislation['n_cosponsor']} co-sponsored = {len(bills)} bills")
except Exception as e:
    legislation["bills"] = None; legislation["n_leads"] = legislation["n_cosponsor"] = None
    step(f"   legislation FAILED ({e})")
legislation["_prov"] = {
    "source": "New York State Senate, Open Legislation API",
    "url": "https://legislation.nysenate.gov/api/3/bills/search",
    "retrieved": NOW,
    "method": (f"Session {SESSION} Senate bills (ceremonial resolutions excluded). 'Leads' = Myrie is prime sponsor; "
               f"'Co-sponsor' = he is a listed co-sponsor. Date shown is the introduction date (first action); subject "
               f"tags are keyword-matched on the bill title and summary."),
    "caveats": ("Status is the latest recorded action ('In Senate Committee' = introduced, not yet advanced). Subject "
                "tags are automated approximations. A prime sponsor drives a bill; a co-sponsor lends support — he "
                "co-sponsors far more bills than he leads."),
}
json.dump(legislation, open(DATA / "legislation.json", "w"))

# ---------------------------------------------------------------- bake for file:// use
out = {}
for name in ("boundary", "trends", "people", "turf", "news", "headline", "member", "maplayers", "legislation"):
    out[name] = json.load(open(DATA / f"{name}.json"))
with open(DATA / "portal_data.js", "w") as fh:
    fh.write("window.PORTAL_DATA = " + json.dumps(out) + ";")
step("baked data/portal_data.js (index.html reads this — works from file://)")
step("DONE. Data in " + str(DATA))

