#!/usr/bin/env python3
"""
Static Census layers for the District Portal (run by hand about once a year, when a new ACS
5-year release lands). Needs geopandas. Writes data/acs_static.js, loaded by index.html:

  window.TRACTS  — census tracts clipped to the district, with income, rent, race/ethnicity,
                   foreign-born and renter shares (and the income margin of error)
  window.ORIGINS — for the whole district: top countries of birth of foreign-born residents,
                   and languages spoken at home

HARD RULE: no invented data. A suppressed estimate stays null and is drawn as "no estimate".
"""
import json, os, re, urllib.request
from datetime import datetime, timezone
from pathlib import Path
import geopandas as gpd
from shapely.geometry import shape, mapping
from shapely.ops import unary_union

DATA = Path(__file__).resolve().parent.parent / "data"
KEY = os.environ.get("CENSUS_API_KEY", "")
NOW = datetime.now(timezone.utc).strftime("%Y-%m-%d")
STATE, COUNTY, SLDU = "36", "047", "020"
TRACT_ZIP = "https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_36_tract_500k.zip"

def api(year, params):
    url = f"https://api.census.gov/data/{year}/acs/acs5?" + params + (f"&key={KEY}" if KEY else "")
    rows = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=90).read())
    return [dict(zip(rows[0], r)) for r in rows[1:]]

def pick_year():
    for y in (2024, 2023):
        try:
            api(y, f"get=B01003_001E&for=state:{STATE}")
            return y
        except Exception:
            continue
    raise SystemExit("ACS API unreachable")

def num(v):
    try:
        f = float(v)
        return None if f < 0 else f                                # -666666666 etc. = suppressed
    except Exception:
        return None

YEAR = pick_year()
VINTAGE = f"{YEAR-4}–{YEAR}"
print("ACS 5-year", VINTAGE)

# ---------------------------------------------------------------- tracts
TV = {"B01003_001E": "pop", "B19013_001E": "income", "B19013_001M": "income_moe", "B25064_001E": "rent",
      "B03002_001E": "r_tot", "B03002_003E": "r_white", "B03002_004E": "r_black", "B03002_006E": "r_asian", "B03002_012E": "r_hisp",
      "B05002_001E": "fb_tot", "B05002_013E": "fb", "B25003_001E": "ten_tot", "B25003_003E": "renters",
      "B17001_001E": "pov_tot", "B17001_002E": "poor", "B22003_001E": "hh_tot", "B22003_002E": "snap"}
recs = {r["tract"]: r for r in api(YEAR, "get=" + ",".join(TV) + f"&for=tract:*&in=state:{STATE}%20county:{COUNTY}")}
g20 = shape(json.load(open(DATA / "boundary.json"))["features"][0]["geometry"]).buffer(0)
t = gpd.read_file(TRACT_ZIP)
t = t[t.COUNTYFP == COUNTY]
d_proj = gpd.GeoSeries([g20], crs=4326).to_crs(2263).iloc[0]
tp = t.to_crs(2263)
t = t.assign(share=(tp.geometry.intersection(d_proj).area / tp.geometry.area).values)
t = t[t.share > 0.02].to_crs(4326)

def rnd(c):
    return [round(c[0], 5), round(c[1], 5)] if isinstance(c[0], (int, float)) else [rnd(x) for x in c]

feats = []
for _, row in t.iterrows():
    r = recs.get(row.TRACTCE)
    if not r:
        continue
    v = {name: num(r[var]) for var, name in TV.items()}
    pct = lambda a, b: round(100 * v[a] / v[b], 1) if v[a] is not None and v[b] else None
    geom = row.geometry.intersection(g20)
    if geom.geom_type == "GeometryCollection":                     # keep the polygon parts, drop stray lines/points
        geom = unary_union([p for p in geom.geoms if p.geom_type in ("Polygon", "MultiPolygon")])
    geom = geom.simplify(0.00005)
    if geom.is_empty:
        continue
    m = mapping(geom)
    feats.append({"type": "Feature", "geometry": {"type": m["type"], "coordinates": rnd(m["coordinates"])},
                  "properties": {"tract": row.NAME, "share_in_district": round(100 * row.share), "pop": v["pop"],
                                 "income": v["income"], "income_moe": v["income_moe"], "rent": v["rent"],
                                 "pct_black": pct("r_black", "r_tot"), "pct_white": pct("r_white", "r_tot"),
                                 "pct_hisp": pct("r_hisp", "r_tot"), "pct_asian": pct("r_asian", "r_tot"),
                                 "pct_foreign_born": pct("fb", "fb_tot"), "pct_renters": pct("renters", "ten_tot"),
                                 "pct_poverty": pct("poor", "pov_tot"), "pct_snap": pct("snap", "hh_tot")}})
tracts = {"type": "FeatureCollection", "features": feats,
          "_prov": {"source": f"U.S. Census Bureau, American Community Survey 5-year estimates, {VINTAGE}, by census tract",
                    "url": f"https://api.census.gov/data/{YEAR}/acs/acs5", "retrieved": NOW,
                    "method": ("Tables B19013 (median household income), B25064 (median gross rent), B03002 (race/ethnicity; White, "
                               "Black and Asian are non-Hispanic), B05002 (foreign-born), B25003 (renters), B17001 (people below the federal poverty line), B22003 (households that received SNAP in the past 12 months). Tract shapes (Census "
                               "cartographic boundaries, 2023) are clipped to the district; tracts less than 2% inside are dropped."),
                    "caveats": ("Tract estimates rest on small samples and carry wide margins of error: a typical tract's median income "
                                "is ±$15,000–$30,000, so neighboring shades can differ by chance. Values describe the WHOLE tract even where "
                                "only part of it is inside the district. Gray = the Census published no estimate (parks, too few households).")}}
print(len(feats), "tracts")

# ---------------------------------------------------------------- district-wide: country of birth + language at home
def group(table):
    rows = api(YEAR, f"get=group({table})&for=state%20legislative%20district%20(upper%20chamber):{SLDU}&in=state:{STATE}")[0]
    labels = json.loads(urllib.request.urlopen(f"https://api.census.gov/data/{YEAR}/acs/acs5/groups/{table}.json", timeout=90).read())["variables"]
    return rows, labels

rows, labels = group("B05006")
fb_total = num(rows["B05006_001E"])
countries = []
for var, meta in labels.items():
    if not re.fullmatch(r"B05006_\d{3}E", var) or var == "B05006_001E":
        continue
    parts = meta["label"].replace("Estimate!!Total:!!", "").split("!!")
    leaf = parts[-1]
    if leaf.endswith(":") or leaf.lower().startswith("other") or "n.e.c" in leaf:   # keep individual countries only
        continue
    e, moe = num(rows.get(var)), num(rows.get(var[:-1] + "M"))
    if e:
        countries.append({"country": leaf, "n": int(e), "moe": int(moe) if moe is not None else None})
countries.sort(key=lambda c: -c["n"])

rows, labels = group("C16001")
lang_total = num(rows["C16001_001E"])
langs = []
for var, meta in labels.items():
    if not re.fullmatch(r"C16001_\d{3}E", var):
        continue
    parts = meta["label"].replace("Estimate!!Total:!!", "").split("!!")
    if len(parts) != 1 or var == "C16001_001E":                       # top-level language groups only
        continue
    name = parts[0].rstrip(":")
    e = num(rows.get(var))
    lep = num(rows.get(f"C16001_{int(var[7:10]) + 2:03d}E")) if name != "Speak only English" else None
    if e:
        langs.append({"language": name, "n": int(e), "pct": round(100 * e / lang_total, 1),
                      "n_limited_english": int(lep) if lep is not None else None})
langs.sort(key=lambda l: -l["n"])
origins = {"foreign_born_total": int(fb_total), "countries": countries[:12], "pop_5plus": int(lang_total), "languages": langs,
           "_prov": {"source": f"U.S. Census Bureau, ACS 5-year estimates, {VINTAGE}, State Senate District 20",
                     "url": f"https://api.census.gov/data/{YEAR}/acs/acs5", "retrieved": NOW,
                     "method": ("Table B05006 (place of birth for the foreign-born population; individual countries only, regional "
                                "subtotals and 'other' lines left out) and table C16001 (language spoken at home, ages 5+; "
                                "'limited English' = speaks English less than 'very well')."),
                     "caveats": ("Survey estimates with margins of error (shown for countries). Country of birth is not ancestry or "
                                 "ethnicity: U.S.-born children of immigrants are not counted here. The Census groups many languages "
                                 "together (Haitian is reported with French and Cajun).")}}
with open(DATA / "acs_static.js", "w") as fh:
    fh.write("window.TRACTS = " + json.dumps(tracts) + ";\nwindow.ORIGINS = " + json.dumps(origins) + ";")
print("wrote data/acs_static.js", os.path.getsize(DATA / "acs_static.js"), "bytes")
print([(c["country"], c["n"]) for c in countries[:8]]); print([(l["language"], l["pct"]) for l in langs[:6]])
