"""
Census layers for one district: census tracts (clipped) with income, rent, race, poverty, SNAP…, plus
district-wide country of birth and language at home. Called by district.py; runnable alone:

    python3 build/acs_static.py SD-20

Needs geopandas and CENSUS_API_KEY (the Census API no longer answers without one). Tract-level ACS for
each county and the statewide tract shapes are cached in build/cache/ and shared by every district.
HARD RULE: no invented data. A suppressed estimate stays null and is drawn as "no estimate".
"""
import json, re, sys, urllib.request
import geopandas as gpd
from shapely.geometry import shape, mapping
from shapely.ops import unary_union
from common import CACHE, CENSUS_KEY, CHAMBER, STATE, TODAY, census, cached, district_dir, load_config, step

TRACT_ZIP = "https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_36_tract_500k.zip"
TV = {"B01003_001E": "pop", "B19013_001E": "income", "B19013_001M": "income_moe", "B25064_001E": "rent",
      "B03002_001E": "r_tot", "B03002_003E": "r_white", "B03002_004E": "r_black", "B03002_006E": "r_asian", "B03002_012E": "r_hisp",
      "B05002_001E": "fb_tot", "B05002_013E": "fb", "B25003_001E": "ten_tot", "B25003_003E": "renters",
      "B17001_001E": "pov_tot", "B17001_002E": "poor", "B22003_001E": "hh_tot", "B22003_002E": "snap"}

def pick_year():
    for y in (2024, 2023):
        try:
            census(y, f"get=B01003_001E&for=state:{STATE}")
            return y
        except Exception:
            continue
    raise SystemExit("ACS API unreachable")

def num(v):
    try:
        f = float(v)
        return None if f < 0 else f
    except Exception:
        return None

def rnd(c):
    return [round(c[0], 5), round(c[1], 5)] if isinstance(c[0], (int, float)) else [rnd(x) for x in c]

def county_tracts(year, county):
    p = CACHE / f"acs{year}_tracts_{county}.json"
    if not p.exists():
        p.write_text(json.dumps(census(year, "get=" + ",".join(TV) + f"&for=tract:*&in=state:{STATE}%20county:{county}")))
    return {r["tract"]: r for r in json.load(open(p))}

def group_rows(year, table, geo, num_):
    rows = census(year, f"get=group({table})&for={urllib.parse.quote(geo)}:{num_:03d}&in=state:{STATE}")[0]
    labels = json.loads(urllib.request.urlopen(f"https://api.census.gov/data/{year}/acs/acs5/groups/{table}.json", timeout=90).read())["variables"]
    return rows, labels

def build(cfg, g20, counties):
    ddir = district_dir(cfg["code"])
    year = pick_year()
    vintage = f"{year-4}–{year}"
    ch = CHAMBER[cfg["chamber"]]
    step(f"   ACS 5-year {vintage}")
    recs = {}
    for c in counties:
        recs[c] = county_tracts(year, c)
    t = gpd.read_file(cached("cb_2023_36_tract_500k.zip", TRACT_ZIP))
    t = t[t.COUNTYFP.isin(counties)]
    d_proj = gpd.GeoSeries([g20], crs=4326).to_crs(2263).iloc[0]
    tp = t.to_crs(2263)
    t = t.assign(share=(tp.geometry.intersection(d_proj).area / tp.geometry.area).values)
    t = t[t.share > 0.02].to_crs(4326)
    feats, full = [], []
    for _, row in t.iterrows():
        r = recs[row.COUNTYFP].get(row.TRACTCE)
        if not r:
            continue
        v = {name: num(r[var]) for var, name in TV.items()}
        pct = lambda a, b: round(100 * v[a] / v[b], 1) if v[a] is not None and v[b] else None
        geom = row.geometry.intersection(g20)
        if geom.geom_type == "GeometryCollection":
            geom = unary_union([p for p in geom.geoms if p.geom_type in ("Polygon", "MultiPolygon")])
        geom = geom.simplify(0.00005)
        if geom.is_empty:
            continue
        tid = f"{row.NAME}" + ("" if len(counties) == 1 else f" ({row.COUNTYFP})")
        m = mapping(geom)
        feats.append({"type": "Feature", "geometry": {"type": m["type"], "coordinates": rnd(m["coordinates"])},
                      "properties": {"tract": tid, "geoid": row.GEOID, "share_in_district": round(100 * row.share), "pop": v["pop"],
                                     "income": v["income"], "income_moe": v["income_moe"], "rent": v["rent"],
                                     "pct_black": pct("r_black", "r_tot"), "pct_white": pct("r_white", "r_tot"),
                                     "pct_hisp": pct("r_hisp", "r_tot"), "pct_asian": pct("r_asian", "r_tot"),
                                     "pct_foreign_born": pct("fb", "fb_tot"), "pct_renters": pct("renters", "ten_tot"),
                                     "pct_poverty": pct("poor", "pov_tot"), "pct_snap": pct("snap", "hh_tot"),
                                     "renter_hh": v["renters"]}})
        fm = mapping(row.geometry.simplify(0.00003))
        full.append({"type": "Feature", "properties": {"tract": tid}, "geometry": {"type": fm["type"], "coordinates": rnd(fm["coordinates"])}})
    json.dump({"type": "FeatureCollection", "features": full}, open(ddir / "tracts_full.json", "w"), separators=(",", ":"))
    tracts = {"type": "FeatureCollection", "features": feats,
              "_prov": {"source": f"U.S. Census Bureau, American Community Survey 5-year estimates, {vintage}, by census tract",
                        "url": f"https://api.census.gov/data/{year}/acs/acs5", "retrieved": TODAY, "vintage": vintage, "year": year,
                        "method": ("Tables B19013 (median household income), B25064 (median gross rent), B03002 (race/ethnicity; White, "
                                   "Black and Asian are non-Hispanic), B05002 (foreign-born), B25003 (renters), B17001 (people below the "
                                   "federal poverty line), B22003 (households that received SNAP in the past 12 months). Tract shapes "
                                   "(Census cartographic boundaries, 2023) are clipped to the district; tracts less than 2% inside are dropped."),
                        "caveats": ("Tract estimates rest on small samples and carry wide margins of error: a typical tract's median income "
                                    "is ±$15,000–$30,000, so neighboring shades can differ by chance. Values describe the WHOLE tract even where "
                                    "only part of it is inside the district. Gray = the Census published no estimate (parks, too few households).")}}
    step(f"   {len(feats)} tracts")

    # district-wide: country of birth + language at home
    rows, labels = group_rows(year, "B05006", ch["acs_geo"], cfg["number"])
    fb_total = num(rows["B05006_001E"])
    countries = []
    for var, meta in labels.items():
        if not re.fullmatch(r"B05006_\d{3}E", var) or var == "B05006_001E":
            continue
        leaf = meta["label"].replace("Estimate!!Total:!!", "").split("!!")[-1]
        if leaf.endswith(":") or leaf.lower().startswith("other") or "n.e.c" in leaf:
            continue
        e, moe = num(rows.get(var)), num(rows.get(var[:-1] + "M"))
        if e:
            countries.append({"country": leaf, "n": int(e), "moe": int(moe) if moe is not None else None})
    countries.sort(key=lambda c: -c["n"])
    rows, labels = group_rows(year, "C16001", ch["acs_geo"], cfg["number"])
    lang_total = num(rows["C16001_001E"])
    langs = []
    for var, meta in labels.items():
        if not re.fullmatch(r"C16001_\d{3}E", var):
            continue
        parts = meta["label"].replace("Estimate!!Total:!!", "").split("!!")
        if len(parts) != 1 or var == "C16001_001E":
            continue
        name = parts[0].rstrip(":")
        e = num(rows.get(var))
        lep = num(rows.get(f"C16001_{int(var[7:10]) + 2:03d}E")) if name != "Speak only English" else None
        if e:
            langs.append({"language": name, "n": int(e), "pct": round(100 * e / lang_total, 1),
                          "n_limited_english": int(lep) if lep is not None else None})
    langs.sort(key=lambda l: -l["n"])
    origins = {"foreign_born_total": int(fb_total or 0), "countries": countries[:12], "pop_5plus": int(lang_total or 0), "languages": langs,
               "_prov": {"source": f"U.S. Census Bureau, ACS 5-year estimates, {vintage}, {ch['label']} District {cfg['number']}",
                         "url": f"https://api.census.gov/data/{year}/acs/acs5", "retrieved": TODAY,
                         "method": ("Table B05006 (place of birth for the foreign-born population; individual countries only, regional "
                                    "subtotals and 'other' lines left out) and table C16001 (language spoken at home, ages 5+; "
                                    "'limited English' = speaks English less than 'very well')."),
                         "caveats": ("Survey estimates with margins of error (shown for countries). Country of birth is not ancestry or "
                                     "ethnicity: U.S.-born children of immigrants are not counted here. The Census groups many languages "
                                     "together (Haitian is reported with French and Cajun).")}}
    with open(ddir / "acs_static.js", "w") as fh:
        fh.write("window.TRACTS = " + json.dumps(tracts, separators=(",", ":")) + ";\nwindow.ORIGINS = " + json.dumps(origins, separators=(",", ":")) + ";")
    step(f"   origins: {[(c['country'], c['n']) for c in countries[:4]]}")
    return year

if __name__ == "__main__":
    import urllib.parse
    from district import district_geometry
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else "SD-20")
    g20, counties, _ = district_geometry(cfg)
    build(cfg, g20, counties)
