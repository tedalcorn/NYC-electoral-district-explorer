#!/usr/bin/env python3
"""
Build (or rebuild) everything for ONE district:

    python3 build/district.py SD-20              # full build
    python3 build/district.py AD-43 --skip-maps  # everything but the slow map layers
    flags: --skip-maps --skip-acs --skip-news --skip-leg

Reads districts/<code>.json, writes data/<code>/, then refreshes data/districts.js (the picker list).
Citywide inputs are downloaded once into build/cache/ and shared by every district.
HARD RULE: no invented data. A failed fetch is an honest gap, never a guess.
"""
import json, re, sys, urllib.parse
from collections import Counter
import geopandas as gpd
from shapely.geometry import shape, mapping, Point
from shapely.ops import unary_union
from common import (BOROS, CHAMBER, CENSUS_KEY, LEG_KEY, NOW, TODAY, STATE, SESSION, all_configs, bake, cached, cached_json,
                    census, district_dir, fetch, jfetch, load_config, members, step, write_registry)

TIGER = "https://www2.census.gov/geo/tiger/TIGER{yr}/{T}/tl_{yr}_36_{t}.zip"

def boro_shapes():
    gj = cached_json("boroughs.geojson", "https://data.cityofnewyork.us/api/geospatial/gthc-hcne?method=export&format=GeoJSON")
    out = {}
    for f in gj["features"]:
        name = next(v for k, v in f["properties"].items() if "name" in k.lower())
        fips = next(k for k, b in BOROS.items() if b["name"].lower() == name.lower())
        out[fips] = shape(f["geometry"]).buffer(0)
    return out

def district_geometry(cfg):
    """(shapely polygon in WGS84, county FIPS list, borough names) for a district; the polygon is also the
    2024 TIGER feature written to boundary.json by build()."""
    ch = CHAMBER[cfg["chamber"]]
    g = gpd.read_file(cached(f"tl_2024_36_{ch['tiger']}.zip", TIGER.format(yr=2024, T=ch["tiger"].upper(), t=ch["tiger"])))
    col = "SLDUST" if ch["tiger"] == "sldu" else "SLDLST"
    row = g[g[col] == f"{cfg['number']:03d}"].to_crs(4326)
    if row.empty:
        raise SystemExit(f"{cfg['code']}: no TIGER feature with {col}={cfg['number']:03d}")
    poly = row.geometry.iloc[0].buffer(0)
    proj = gpd.GeoSeries([poly], crs=4326).to_crs(2263).iloc[0]
    counties = []
    for fips, b in boro_shapes().items():
        bp = gpd.GeoSeries([b], crs=4326).to_crs(2263).iloc[0]
        if proj.intersection(bp).area / proj.area >= 0.01:
            counties.append(fips)
    if not counties:
        raise SystemExit(f"{cfg['code']} does not touch New York City")
    return poly, counties, [BOROS[c]["boro"] for c in counties]

def acs_geo(cfg):
    return urllib.parse.quote(CHAMBER[cfg["chamber"]]["acs_geo"]) + f":{cfg['number']:03d}&in=state:{STATE}"

AGE_GROUPS = [("Under 18", list(range(3, 7)), list(range(27, 31))), ("18-34", list(range(7, 13)), list(range(31, 37))),
              ("35-54", list(range(13, 17)), list(range(37, 41))), ("55-74", list(range(17, 23)), list(range(41, 47))),
              ("75+", list(range(23, 26)), list(range(47, 50)))]
AGE_VARS = [f"B01001_{i:03d}E" for _, m, f in AGE_GROUPS for i in m + f]
RACE_VARS = {"total": "B03002_001E", "white_nh": "B03002_003E", "black_nh": "B03002_004E", "asian_nh": "B03002_006E", "hispanic": "B03002_012E"}
NYC_COUNTIES = "county:005,047,061,081,085&in=state:36"

def agg_ages(recs):
    return {name: sum(int(r[f"B01001_{i:03d}E"]) for r in recs for i in m + f) for name, m, f in AGE_GROUPS}

def agg_race(recs):
    t = {k: sum(int(r[v]) for r in recs) for k, v in RACE_VARS.items()}
    tot = t["total"]
    other = tot - t["white_nh"] - t["black_nh"] - t["asian_nh"] - t["hispanic"]
    return {"White (non-Hisp.)": t["white_nh"] / tot * 100, "Black (non-Hisp.)": t["black_nh"] / tot * 100,
            "Hispanic (any race)": t["hispanic"] / tot * 100, "Asian (non-Hisp.)": t["asian_nh"] / tot * 100,
            "Other / multiracial": other / tot * 100}

def overlap_table(poly, features, id_fn, label_fn, min_share=1.0):
    rows = []
    for f in features:
        try:
            g = shape(f["geometry"]).buffer(0)
            share = poly.intersection(g).area / poly.area * 100
            if share >= min_share:
                rows.append({"district": id_fn(f), "label": label_fn(f), "pct_of_district": round(share, 1)})
        except Exception:
            continue
    return sorted(rows, key=lambda r: -r["pct_of_district"])

def prop(f, *needles):
    for k, v in f["properties"].items():
        if any(n in k.lower() for n in needles):
            return v
    return None

def build(code, skip=()):
    cfg = load_config(code)
    ch = CHAMBER[cfg["chamber"]]
    cur = cfg.get("curated", {})
    ddir = district_dir(code)
    poly, counties, boros = district_geometry(cfg)
    boro_names = [BOROS[c]["name"] for c in counties]
    step(f"{code}: {ch['label']} District {cfg['number']} — {', '.join(boro_names)}")

    # ---------------------------------------------------------------- member (Open Legislation API + anything hand-curated)
    step("member")
    mem = members(cfg["chamber"]).get(cfg["number"]) or {}
    name = mem.get("fullName") or cfg.get("member_name") or "Vacant"
    last = name.split()[-1] if name != "Vacant" else "the member"
    if last.lower() in ("jr.", "jr", "sr.", "iii", "ii") and len(name.split()) > 1:
        last = name.split()[-2].rstrip(",")
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    page = ch["page"].format(slug=slug, num=cfg["number"])
    photo = None
    img = mem.get("imgName")
    if img and img != "no_image.jpg":
        try:
            (ddir / "member.jpg").write_bytes(fetch(f"https://legislation.nysenate.gov/static/img/business_assets/members/mini/{img}"))
            photo = f"data/{code}/member.jpg"
        except Exception as e:
            step(f"   photo FAILED ({e})")
    cm = cur.get("member", {})
    member = {"name": name, "last_name": last, "memberId": mem.get("memberId"), "shortName": mem.get("shortName"),
              "office": f"{ch['role']}, District {cfg['number']}" + (f" ({cur['office_note']})" if cur.get("office_note") else ""),
              "wikipedia": cm.get("wikipedia"), "senate_page": page, "legislation_url": page + ("/legislation" if cfg["chamber"] == "senate" else ""),
              "photo": photo, "photo_source": f"https://legislation.nysenate.gov/static/img/business_assets/members/mini/{img}" if photo else None,
              "committees": cm.get("committees"), "social": cm.get("social"), "elections": cm.get("elections"), "priorities": cm.get("priorities"),
              "_prov": cm.get("_prov") or {"source": "New York State Senate Open Legislation API (name, district, portrait)",
                                            "url": f"https://legislation.nysenate.gov/api/3/members/{SESSION}/{ch['api']}", "retrieved": TODAY,
                                            "method": "Automated lookup only.",
                                            "caveats": "Election history, committees, social accounts and priorities have not been compiled for this member yet; use the official page."}}
    json.dump(member, open(ddir / "member.json", "w"))
    step(f"   {name}" + (" (photo)" if photo else " (no photo in the API)"))

    # ---------------------------------------------------------------- boundary (2024 lines) + previous (2020 file = 2012 lines)
    step("boundary")
    g24 = gpd.read_file(cached(f"tl_2024_36_{ch['tiger']}.zip", TIGER.format(yr=2024, T=ch["tiger"].upper(), t=ch["tiger"])))
    col = "SLDUST" if ch["tiger"] == "sldu" else "SLDLST"
    feat = json.loads(g24[g24[col] == f"{cfg['number']:03d}"].to_crs(4326).to_json())["features"][0]
    feat["properties"] = {"code": code}
    boundary = {"type": "FeatureCollection", "features": [feat],
                "_prov": {"source": f"U.S. Census TIGER/Line 2024, NY state {'senate' if cfg['chamber']=='senate' else 'assembly'} districts (2022 lines)",
                          "url": TIGER.format(yr=2024, T=ch["tiger"].upper(), t=ch["tiger"]), "retrieved": TODAY,
                          "method": f"Feature {col}={cfg['number']:03d}.", "caveats": "These are the lines used in the 2022 and 2024 elections."}}
    try:
        g20 = gpd.read_file(cached(f"tl_2020_36_{ch['tiger']}.zip", TIGER.format(yr=2020, T=ch["tiger"].upper(), t=ch["tiger"])))
        prev = g20[g20[col] == f"{cfg['number']:03d}"].to_crs(4326)
        boundary["prev_lines"] = {"type": "Feature", "properties": {"note": f"{code} under the 2012 lines (in effect 2012-2021)"},
                                  "geometry": mapping(prev.geometry.iloc[0])}
    except Exception as e:
        boundary["prev_lines"] = None; step(f"   previous lines FAILED ({e})")
    json.dump(boundary, open(ddir / "boundary.json", "w"))

    # ---------------------------------------------------------------- neighborhoods (NTAs) — for the label and the news searches
    step("neighborhoods")
    ntas = cached_json("nta2020.json", "https://data.cityofnewyork.us/resource/9nt8-h7nd.json?$limit=500")
    proj = gpd.GeoSeries([poly], crs=4326).to_crs(2263).iloc[0]
    hoods = []
    for r in ntas:
        if r.get("ntatype") not in (None, "0", 0):                 # 1-9 = parks, airports, cemeteries, etc.
            continue
        try:
            g = gpd.GeoSeries([shape(r["the_geom"])], crs=4326).to_crs(2263).iloc[0].buffer(0)
        except Exception:
            continue
        inter = proj.intersection(g).area
        if inter / proj.area >= 0.03 or inter / g.area >= 0.5:
            hoods.append((inter / proj.area, r["ntaname"], r["boroname"]))
    hoods.sort(reverse=True)
    # Census NTA names ("Crown Heights (South)", "Prospect Lefferts Gardens-Wingate") are split into plain
    # place names people actually write, in order of how much of the district each covers
    COMPOUND = ["Bedford-Stuyvesant", "Co-op City", "Bay Ridge-Dyker", "Mott Haven-Port Morris"]   # hyphens that are part of ONE name
    def clean(nm):
        for i, c in enumerate(COMPOUND):
            nm = nm.replace(c, f"@{i}@")
        parts = [re.sub(r"\s*\(.*?\)", "", part).strip() for part in re.split(r"[-–/]", nm)]
        out = []
        for x in parts:
            for i, c in enumerate(COMPOUND):
                x = x.replace(f"@{i}@", c)
            if len(x) > 3:
                out.append(x)
        return out
    seen, terms = set(), []
    for share, nm, boro in hoods:
        for t in clean(nm):
            if t not in seen:
                seen.add(t); terms.append((share, t, boro))
    hoods = terms
    hood_names = [t for _, t, _ in terms]
    step(f"   {hood_names[:8]}")

    # ---------------------------------------------------------------- district record (drives every district-specific string on the page)
    area = cfg.get("area") or (", ".join(hood_names[:3]) or boro_names[0])
    district = {"code": code, "chamber": cfg["chamber"], "number": cfg["number"], "chamber_label": ch["label"], "chamber_long": ch["long"],
                "title": f"{ch['label']} District {cfg['number']}", "member_title": ch["title"], "member": name, "member_last": last,
                "area": area, "boroughs": boro_names, "neighborhoods": hood_names[:12], "built": TODAY,
                "census_reporter": f"https://censusreporter.org/profiles/{'610' if cfg['chamber']=='senate' else '620'}00US36{cfg['number']:03d}/"}
    json.dump(district, open(ddir / "district.json", "w"))

    # ---------------------------------------------------------------- headline + trends + people (ACS by district geography)
    step("census: headline, trends, age + race vs NYC")
    headline = {"_prov": {"source": "U.S. Census Bureau, ACS 2020–2024 5-year, via the Census API",
                          "url": district["census_reporter"], "retrieved": TODAY,
                          "method": f"Geography: {ch['acs_geo']} {cfg['number']:03d}, NY (tables B01003, B19013, B01002, B05002).",
                          "caveats": "Survey estimates with margins of error not shown here. The charts below use the adjacent ACS vintage (2019–23), so small differences are expected."}}
    try:
        r = census(2024, "get=B01003_001E,B19013_001E,B01002_001E,B05002_001E,B05002_013E&for=" + acs_geo(cfg))[0]
        headline["stats"] = {"population": int(r["B01003_001E"]), "median_hh_income": int(r["B19013_001E"]),
                             "median_age": round(float(r["B01002_001E"]), 1),
                             "pct_foreign_born": round(int(r["B05002_013E"]) / int(r["B05002_001E"]) * 100, 1)}
    except Exception as e:
        headline["stats"] = None; step(f"   headline FAILED ({e})")
    try:
        c = cached_json("acs2024_nyc_place.json", "https://api.census.gov/data/2024/acs/acs5?get=B19013_001E,B01002_001E,B05002_001E,B05002_013E&for=place:51000&in=state:36&key=" + CENSUS_KEY)[1]
        headline["city"] = {"name": "New York city", "median_hh_income": int(c[0]), "median_age": round(float(c[1]), 1),
                            "pct_foreign_born": round(int(c[3]) / int(c[2]) * 100, 1),
                            "source": "U.S. Census Bureau, ACS 2020–2024 5-year, place 'New York city, NY'"}
    except Exception as e:
        headline["city"] = None; step(f"   city FAILED ({e})")
    json.dump(headline, open(ddir / "headline.json", "w"))

    years, pops, incs = [], [], []
    for yr in range(2015, 2024):
        try:
            r = census(yr, "get=B01003_001E,B19013_001E&for=" + acs_geo(cfg))[0]
            pops.append(int(r["B01003_001E"])); incs.append(int(r["B19013_001E"]))
        except Exception:
            pops.append(None); incs.append(None)
        years.append(yr)
    json.dump({"years": years, "population": pops, "median_hh_income": incs, "boundary_break_year": 2022,
               "_prov": {"source": "U.S. Census Bureau, ACS 5-year estimates (tables B01003, B19013), via Census API",
                         "url": "https://api.census.gov/data/2023/acs/acs5", "retrieved": TODAY,
                         "method": f"Geography: {ch['acs_geo']} {cfg['number']:03d}, NY; one call per year.",
                         "caveats": ("District lines were REDRAWN effective 2022: points before and after describe different territory, "
                                     "so the jump at the break is partly a boundary artifact. Each 5-year point overlaps four years with its "
                                     "neighbors. Income in nominal dollars.")}}, open(ddir / "trends.json", "w"))
    people = {"_prov": {"source": "U.S. Census Bureau, ACS 2019–2023 5-year (tables B01001 age/sex, B03002 race/ethnicity), via Census API",
                        "url": f"https://data.census.gov/table/ACSDT5Y2023.B03002", "retrieved": TODAY,
                        "method": f"District = {ch['acs_geo']} {cfg['number']:03d}. 'NYC' = the five counties summed. Shares from raw counts.",
                        "caveats": "Survey estimates with margins of error (not shown). White/Black/Asian are alone, non-Hispanic; Hispanic is all races."}}
    try:
        d_age = agg_ages(census(2023, "get=" + ",".join(AGE_VARS) + "&for=" + acs_geo(cfg)))
        raw = json.loads(cached("acs2023_nyc_age.json", "https://api.census.gov/data/2023/acs/acs5?get=" + ",".join(AGE_VARS) + "&for=" + NYC_COUNTIES + "&key=" + CENSUS_KEY).read_text())
        n_age = agg_ages([dict(zip(raw[0], r)) for r in raw[1:]])
        dt, nt = sum(d_age.values()), sum(n_age.values())
        people["age"] = {"groups": [g[0] for g in AGE_GROUPS], "district_pct": [d_age[g[0]] / dt * 100 for g in AGE_GROUPS],
                         "nyc_pct": [n_age[g[0]] / nt * 100 for g in AGE_GROUPS]}
    except Exception as e:
        people["age"] = None; step(f"   age FAILED ({e})")
    try:
        d_race = agg_race(census(2023, "get=" + ",".join(RACE_VARS.values()) + "&for=" + acs_geo(cfg)))
        raw = json.loads(cached("acs2023_nyc_race.json", "https://api.census.gov/data/2023/acs/acs5?get=" + ",".join(RACE_VARS.values()) + "&for=" + NYC_COUNTIES + "&key=" + CENSUS_KEY).read_text())
        n_race = agg_race([dict(zip(raw[0], r)) for r in raw[1:]])
        people["race"] = {"labels": list(d_race), "district_pct": [round(v, 1) for v in d_race.values()], "nyc_pct": [round(v, 1) for v in n_race.values()]}
    except Exception as e:
        people["race"] = None; step(f"   race FAILED ({e})")
    json.dump(people, open(ddir / "people.json", "w"))

    # ---------------------------------------------------------------- shared turf: the other chamber, council, congress
    step("turf")
    other = "assembly" if cfg["chamber"] == "senate" else "senate"
    oc = CHAMBER[other]
    turf = {"_prov": {"sources": {other: f"Census TIGER/Line 2024 {oc['tiger'].upper()}",
                                  "council": "NYC Open Data, City Council Districts (872g-cjhh)",
                                  "congress": "Census TIGERweb, 119th Congressional Districts"},
                      "method": f"Geometric intersection of each district with {code}; showing districts covering ≥1% of its AREA (not population).",
                      "caveats": "Member names come from the Open Legislation API (state) or were compiled by hand (council, Congress); offices change hands — confirm before relying on a name."}}
    try:
        go = gpd.read_file(cached(f"tl_2024_36_{oc['tiger']}.zip", TIGER.format(yr=2024, T=oc["tiger"].upper(), t=oc["tiger"]))).to_crs(4326)
        ocol = "SLDUST" if oc["tiger"] == "sldu" else "SLDLST"
        feats = json.loads(go.to_json())["features"]
        pfx = "SD-" if other == "senate" else "AD-"
        turf[other] = overlap_table(poly, feats, lambda f: pfx + str(int(f["properties"][ocol])), lambda f: f"{oc['label']} District {int(f['properties'][ocol])}")
        onames = members(other)
        for r in turf[other]:
            n = int(r["district"].split("-")[1]); m = onames.get(n)
            r["member"] = m.get("fullName") if m else None
            r["link"] = oc["page"].format(slug=re.sub(r"[^a-z0-9]+", "-", (m or {}).get("fullName", "").lower()).strip("-"), num=n)
    except Exception as e:
        turf[other] = None; step(f"   {other} FAILED ({e})")
    try:
        cj = cached_json("council.geojson", "https://data.cityofnewyork.us/api/geospatial/872g-cjhh?method=export&format=GeoJSON")
        turf["council"] = overlap_table(poly, cj["features"], lambda f: "CD-" + str(int(prop(f, "dist"))), lambda f: "NYC Council District " + str(int(prop(f, "dist"))))
    except Exception as e:
        turf["council"] = None; step(f"   council FAILED ({e})")
    try:
        minx, miny, maxx, maxy = poly.bounds
        svc = jfetch("https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Legislative/MapServer?f=json")
        lid = next(l["id"] for l in svc["layers"] if "congressional districts" in l["name"].lower() and l.get("type") == "Feature Layer")
        env = urllib.parse.quote(json.dumps({"xmin": minx, "ymin": miny, "xmax": maxx, "ymax": maxy, "spatialReference": {"wkid": 4326}}))
        fc = jfetch(f"https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Legislative/MapServer/{lid}/query?geometry={env}"
                    "&geometryType=esriGeometryEnvelope&inSR=4326&spatialRel=esriSpatialRelIntersects&outFields=BASENAME,NAME,GEOID&returnGeometry=true&outSR=4326&f=geojson")
        turf["congress"] = overlap_table(poly, fc["features"], lambda f: "NY-" + str(f["properties"].get("BASENAME", "?")), lambda f: f["properties"].get("NAME", "?"))
    except Exception as e:
        turf["congress"] = None; step(f"   congress FAILED ({e})")
    tn = cur.get("turf_names", {})
    for level, link in (("council", "https://council.nyc.gov/district-{n}/"), ("congress", "https://www.govtrack.us/congress/members/NY/{n}")):
        for r in (turf.get(level) or []):
            n = int(r["district"].split("-")[1])
            r["member"] = tn.get(level, {}).get(str(n)); r["link"] = link.format(n=n)
    json.dump(turf, open(ddir / "turf.json", "w"))

    # ---------------------------------------------------------------- community boards: which overlap, how much, plus any curated needs
    step("community boards")
    cds = cached_json("community_districts.geojson", "https://data.cityofnewyork.us/api/geospatial/5crt-au7u?method=export&format=GeoJSON")
    cards, geo = [], []
    curated_cb = cur.get("community_boards", {})
    for f in cds["features"]:
        bcd = int(prop(f, "boro_cd"))
        if bcd % 100 > 18:                                        # 55, 56, 64, 80s, 95: joint-interest areas (parks, cemeteries, airports), not boards
            continue
        g = gpd.GeoSeries([shape(f["geometry"]).buffer(0)], crs=4326).to_crs(2263).iloc[0]
        share = proj.intersection(g).area / g.area * 100
        if share < 2:
            continue
        boro = {1: "manhattan", 2: "bronx", 3: "brooklyn", 4: "queens", 5: "staten-island"}[bcd // 100]
        n = bcd % 100
        c = curated_cb.get(str(bcd), {})
        cards.append({"cd": bcd, "n": n, "boro": boro.replace("-", " ").title(), "share": round(share, 1),
                      "hoods": c.get("hoods"), "precincts": c.get("precincts"), "needs": c.get("needs"), "note": c.get("note"), "flag": c.get("flag"),
                      "statement_url": c.get("statement_url"), "statement_label": c.get("statement_label"),
                      "profile": f"https://communityprofiles.planning.nyc.gov/{boro}/{n}"})
        gs = shape(f["geometry"]).simplify(0.0003)
        m = mapping(gs)
        geo.append({"type": "Feature", "properties": {"cd": bcd, "n": n}, "geometry": m})
    cards.sort(key=lambda c: -c["share"])
    community = {"cards": cards, "_prov": {"source": cur.get("community_boards_prov") or "NYC Department of City Planning, Community Districts (5crt-au7u) and Community Profiles.",
                                             "url": "https://communityprofiles.planning.nyc.gov/", "retrieved": TODAY,
                                             "method": "Share = projected (EPSG:2263) area of each board inside the district ÷ the board's area.",
                                             "caveats": ("Each board's needs come from its annual Statement of Community District Needs and were compiled by hand; "
                                                         "boards without a compiled statement show only their share and links.")}}
    json.dump(community, open(ddir / "community.json", "w"))
    with open(ddir / "community_districts.js", "w") as fh:
        fh.write("window.CD_GEO = " + json.dumps({"type": "FeatureCollection", "features": geo}, separators=(",", ":")) + ";")
    step(f"   {[(c['cd'], c['share']) for c in cards]}")

    # ---------------------------------------------------------------- news
    if "news" not in skip:
        step("news")
        import news as newsfeed
        top = [h for h in hood_names[:4]]
        hq = "(" + " OR ".join(f'"{h}"' for h in top) + ")"
        queries = [f'"{name}"'] + [f'"{h}" {b}' for _, h, b in hoods[:8]] + cfg.get("news_queries_extra", []) + [
            hq + ' (housing OR tenants OR rent OR rezoning OR landlord)', hq + ' (school OR students OR teachers OR CUNY)',
            hq + ' (subway OR bus OR MTA OR "bike lane" OR traffic)', hq + ' (hospital OR health OR clinic)',
            hq + ' ("small business" OR restaurant OR jobs OR storefront)', hq + ' ("community board" OR "City Council" OR Assembly OR "State Senate")']
        nd = newsfeed.build_news(newsfeed.fetch_items(step, queries), NOW, queries, code=code, member=name, hoods=top)
        if nd:
            json.dump(nd, open(ddir / "news.json", "w")); step(f"   {nd['total']} stories")
        else:
            step("   NO articles — keeping any previous news.json")

    # ---------------------------------------------------------------- legislation
    if "leg" not in skip and mem.get("memberId"):
        step("legislation")
        import legislation
        try:
            ld = legislation.build(mem["memberId"], mem["shortName"], cfg["chamber"], last, NOW)
            if ld:
                json.dump(ld, open(ddir / "legislation.json", "w")); step(f"   {ld['n_leads']} prime + {ld['n_cosponsor']} co-sponsored")
        except Exception as e:
            step(f"   legislation FAILED ({e})")

    # ---------------------------------------------------------------- census tracts + origins, then map layers (tract counts need the tract file)
    if "acs" not in skip:
        step("census tracts + origins")
        import acs_static
        acs_static.build(cfg, poly, counties)
    if "maps" not in skip:
        step("map layers")
        import maplayers
        prev = json.load(open(ddir / "maplayers.json")) if (ddir / "maplayers.json").exists() else {}
        json.dump(maplayers.build(poly, step, prev, boros, [BOROS[c]["county"] for c in counties], ddir, code), open(ddir / "maplayers.json", "w"))

    bake(code)
    write_registry()
    step(f"DONE {code} → data/{code}/")

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    skip = {a[7:] for a in sys.argv[1:] if a.startswith("--skip-")}
    build(args[0] if args else "SD-20", skip)
