#!/usr/bin/env python3
"""
Map layers for the District Portal, clipped to the district boundary. Used by build_portal.py
(full build) and runnable on its own to refresh just the map:

    python3 build/maplayers.py        # reads data/boundary.json, writes data/maplayers.json, re-bakes portal_data.js

Needs shapely. Robustness rule: if a layer's fetch fails, the previously committed layer is kept.
HARD RULE: no invented data. A record without usable coordinates is counted and reported, never placed by guess.
"""
import json, sys, time, urllib.parse, urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from shapely.geometry import shape, mapping, Point
from shapely.ops import unary_union

DATA = Path(__file__).resolve().parent.parent / "data"
UA = {"User-Agent": "Mozilla/5.0 (NYU Senior Seminar class project; contact ted.alcorn@gmail.com)"}
SHOOTINGS_SINCE = "2021-01-01"
EVICTIONS_SINCE = "2023-01-01"

# MTA trunk-line colors (the MTA's published palette). Services sharing a trunk share a color.
TRUNKS = [
    ("123", "#EE352E", ["1", "2", "3"]), ("456", "#00933C", ["4", "5", "6", "5 Peak"]), ("7", "#B933AD", ["7"]),
    ("ACE", "#0039A6", ["A", "C", "E"]), ("BDFM", "#FF6319", ["B", "D", "F", "M"]), ("G", "#6CBE45", ["G"]),
    ("JZ", "#996633", ["J", "Z"]), ("L", "#A7A9AC", ["L"]), ("NQRW", "#FCCC0A", ["N", "Q", "R", "W"]),
    ("S", "#808183", ["SF", "SR", "ST", "S"]),
]
SERVICE_COLOR = {svc: color for _, color, svcs in TRUNKS for svc in svcs}
SERVICE_LABEL = {"SF": "S", "SR": "S", "ST": "S", "5 Peak": "5"}

def jfetch(url, timeout=120):
    return json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read())

def soda(domain, dataset, where=None, select=None, limit=50000):
    """Page through a Socrata dataset."""
    rows, offset = [], 0
    while True:
        q = {"$limit": limit, "$offset": offset, "$order": ":id"}
        if where:
            q["$where"] = where
        if select:
            q["$select"] = select
        page = jfetch(f"https://{domain}/resource/{dataset}.json?" + urllib.parse.urlencode(q))
        rows += page
        if len(page) < limit:
            return rows
        offset += limit

def latlon(r, lat_key="latitude", lon_key="longitude"):
    """(lat, lon) or None. NYPD's current shootings feed publishes the two columns swapped on most
    rows, so a pair is accepted in either order as long as it lands in the NYC bounding box."""
    try:
        a, b = float(r[lat_key]), float(r[lon_key])
    except Exception:
        return None
    if 40.4 < a < 41.0 and -74.3 < b < -73.6:
        return a, b
    if 40.4 < b < 41.0 and -74.3 < a < -73.6:
        return b, a
    return None

def geo(g, tol):
    """Simplified GeoJSON geometry with coordinates rounded to ~1 m, to keep the baked file small."""
    def rnd(c):
        return [round(c[0], 5), round(c[1], 5)] if isinstance(c[0], (int, float)) else [rnd(x) for x in c]
    m = mapping(g.simplify(tol))
    return {"type": m["type"], "coordinates": rnd(m["coordinates"])}

def build(g20, step=print, previous=None):
    previous = previous or {}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    out = {}
    near = g20.buffer(0.0015)                                      # ~150 m, so lines don't stop dead at the border

    def keep(name, err):
        out[name] = previous.get(name)
        step(f"   {name} FAILED ({err}) — keeping the previously committed layer")

    # --- police precincts
    try:
        pj = jfetch("https://data.cityofnewyork.us/api/geospatial/y76i-bdw7?method=export&format=GeoJSON")
        feats = []
        for f in pj["features"]:
            try:
                if shape(f["geometry"]).buffer(0).intersects(g20):
                    pk = next((v for k, v in f["properties"].items() if "precinct" in k.lower()), "?")
                    feats.append({"type": "Feature", "properties": {"precinct": pk}, "geometry": f["geometry"]})
            except Exception:
                continue
        out["precincts"] = {"type": "FeatureCollection", "features": feats}
        step(f"   precincts: {len(feats)} intersect the district")
    except Exception as e:
        keep("precincts", e)

    # --- shootings: NYPD's current incident-level feed, joined to the victim feed for fatalities
    try:
        where = f"boro='BROOKLYN' AND occur_date>='{SHOOTINGS_SINCE}T00:00:00'"
        inc = soda("data.cityofnewyork.us", "5ucz-vwe8", where)
        keys = {r["incident_key"] for r in inc}
        vic = soda("data.cityofnewyork.us", "pztn-9bne", select="incident_key,stat_murder_flg")
        n_vic, n_killed = Counter(), Counter()
        for v in vic:
            if v.get("incident_key") in keys:
                n_vic[v["incident_key"]] += 1
                n_killed[v["incident_key"]] += v.get("stat_murder_flg") == "Y"
        pts, n_nocoord = [], 0
        for r in inc:
            ll = latlon(r)
            if ll is None:
                n_nocoord += 1
                continue
            if g20.contains(Point(ll[1], ll[0])):
                k = r["incident_key"]
                pts.append({"lat": round(ll[0], 5), "lon": round(ll[1], 5), "date": (r.get("occur_date") or "")[:10],
                            "murder": n_killed[k] > 0, "victims": n_vic[k] or None, "killed": n_killed[k],
                            "precinct": r.get("precinct"), "nycha": r.get("jurisdiction_code") == "2"})
        pts.sort(key=lambda p: p["date"])
        out["shootings"] = pts
        out["shootings_stats"] = {
            "in_district": len(pts), "fatal": sum(p["murder"] for p in pts), "nycha": sum(p["nycha"] for p in pts),
            "victims": sum(p["victims"] or 0 for p in pts), "no_victim_record": sum(1 for p in pts if not p["victims"]),
            "first": SHOOTINGS_SINCE, "last_in_district": pts[-1]["date"] if pts else None,
            "data_through": max((r.get("occur_date") or "")[:10] for r in inc),
            "brooklyn_incidents": len(inc), "brooklyn_missing_coords": n_nocoord}
        step(f"   shootings: {len(pts)} incidents in the district, {SHOOTINGS_SINCE} to "
             f"{out['shootings_stats']['data_through']}; {n_nocoord}/{len(inc)} Brooklyn incidents lacked coordinates")
    except Exception as e:
        keep("shootings", e)
        out["shootings_stats"] = previous.get("shootings_stats")

    # --- subway: stations + service lines dissolved by trunk color
    try:
        subs = []
        for r in jfetch("https://data.ny.gov/resource/39hk-dx4f.json?$limit=2500"):
            ll = latlon(r, "gtfs_latitude", "gtfs_longitude")
            if ll and g20.contains(Point(ll[1], ll[0])):
                subs.append({"lat": round(ll[0], 5), "lon": round(ll[1], 5), "name": r.get("stop_name"), "routes": r.get("daytime_routes")})
        out["subway"] = subs
        step(f"   subway: {len(subs)} stations in the district")
    except Exception as e:
        keep("subway", e)
    try:
        by_trunk = {}
        for r in jfetch("https://data.ny.gov/resource/s692-irgq.json?$limit=500"):
            svc = r.get("service")
            if svc not in SERVICE_COLOR:
                continue
            line = shape(r["geometry"])
            stops_here = SERVICE_LABEL.get(svc, svc) in {x for st in (out.get("subway") or []) for x in (st.get("routes") or "").split()}
            if not stops_here and line.intersection(g20).length < 0.002:
                continue                                          # only grazes the border and has no station inside
            clip = line.intersection(near)
            if not clip.is_empty:
                t = by_trunk.setdefault(SERVICE_COLOR[svc], {"geoms": [], "services": set()})
                t["geoms"].append(clip)
                t["services"].add(SERVICE_LABEL.get(svc, svc))
        feats = []
        for trunk, color, _ in TRUNKS:
            if color in by_trunk:
                t = by_trunk[color]
                feats.append({"type": "Feature", "properties": {"trunk": trunk, "color": color, "services": sorted(t["services"])},
                              "geometry": geo(unary_union(t["geoms"]), 0.00005)})
        out["subway_lines"] = {"type": "FeatureCollection", "features": feats}
        step(f"   subway lines: {[(f['properties']['trunk'], f['properties']['services']) for f in feats]}")
    except Exception as e:
        keep("subway_lines", e)

    # --- Citi Bike docks (GBFS)
    try:
        cb = []
        for s in jfetch("https://gbfs.citibikenyc.com/gbfs/en/station_information.json")["data"]["stations"]:
            ll = latlon(s, "lat", "lon")
            if ll and g20.contains(Point(ll[1], ll[0])):
                cb.append({"lat": round(ll[0], 5), "lon": round(ll[1], 5), "name": s.get("name")})
        out["citibike"] = cb
        step(f"   citibike: {len(cb)} docks in the district")
    except Exception as e:
        keep("citibike", e)

    # --- bus routes currently in effect, one line per route, clipped to the district
    try:
        rows = soda("data.ny.gov", "bzwk-3hb4", where="in_effect='true'",
                    select="route_short_name,route_long_name,route_type,route_color,geometry", limit=1000)
        byroute = {}
        for r in rows:
            try:
                gg = shape(r["geometry"])
                if (r.get("route_short_name") or "").startswith("T") or not gg.intersects(g20):
                    continue                                      # T### = temporary subway-replacement shuttles
                clip = gg.intersection(near)
                if clip.is_empty:
                    continue
                b = byroute.setdefault(r.get("route_short_name") or "?", {"geoms": [], "r": r})
                b["geoms"].append(clip)
            except Exception:
                continue
        feats = []
        for rn in sorted(byroute):
            r = byroute[rn]["r"]
            feats.append({"type": "Feature", "geometry": geo(unary_union(byroute[rn]["geoms"]), 0.0001),
                          "properties": {"route": rn, "name": r.get("route_long_name"), "kind": r.get("route_type")}})
        out["bus"] = {"type": "FeatureCollection", "features": feats}
        step(f"   bus: {len(feats)} routes through the district ({', '.join(sorted(byroute))})")
    except Exception as e:
        keep("bus", e)

    # --- evictions: residential, executed
    try:
        where = f"borough='BROOKLYN' AND residential_commercial_ind='Residential' AND executed_date>='{EVICTIONS_SINCE}'"
        erows = soda("data.cityofnewyork.us", "6z8x-wfk4", where)
        ev, n_nocoord = [], 0
        for r in erows:
            ll = latlon(r)
            if ll is None:
                n_nocoord += 1
                continue
            if g20.contains(Point(ll[1], ll[0])):
                ev.append({"lat": round(ll[0], 5), "lon": round(ll[1], 5), "date": (r.get("executed_date") or "")[:10], "addr": r.get("eviction_address")})
        ev.sort(key=lambda p: p["date"])
        out["evictions"] = ev
        out["evictions_stats"] = {"in_district": len(ev), "first": EVICTIONS_SINCE, "data_through": max((r.get("executed_date") or "")[:10] for r in erows),
                                  "brooklyn_records": len(erows), "brooklyn_missing_coords": n_nocoord}
        step(f"   evictions: {len(ev)} in the district; {n_nocoord}/{len(erows)} Brooklyn records lacked coordinates")
    except Exception as e:
        keep("evictions", e)
        out["evictions_stats"] = previous.get("evictions_stats")

    out["_prov"] = {
        "sources": {
            "precincts": "NYC Open Data, Police Precincts (y76i-bdw7)",
            "shootings": "NYC Open Data, NYPD Shootings (2006–Present) (5ucz-vwe8), with fatalities from NYPD Shooting Victims (pztn-9bne)",
            "subway": "MTA / NY State Open Data, MTA Subway Stations (39hk-dx4f) and MTA Subway Service Lines (s692-irgq)",
            "bus": "MTA / NY State Open Data, MTA Bus Routes (bzwk-3hb4), routes currently in effect",
            "citibike": "Citi Bike GBFS station_information feed",
            "evictions": "NYC Open Data, Evictions (6z8x-wfk4) — residential, executed",
        },
        "retrieved": now,
        "method": ("Each layer is clipped to the SD-20 boundary (points kept if inside; subway and bus lines clipped just past the "
                   "border). Shootings are one dot per incident, not per victim; an incident is fatal if any victim in NYPD's "
                   "victim file is flagged a murder. NYPD updates the shootings files quarterly."),
        "caveats": ("Points are shown at the location the agency recorded. NYPD places most shootings at the midpoint of a street "
                    "segment, so dots are approximate and several incidents can share one spot. Records without usable coordinates "
                    "are counted below, not guessed. Precinct lines are not district lines. Subway lines are colored by MTA trunk "
                    "line; bus segments show routes passing through, not full routes."),
    }
    return out

def bake():
    bundle = {}
    for name in ("boundary", "trends", "people", "turf", "news", "headline", "member", "maplayers", "legislation"):
        bundle[name] = json.load(open(DATA / f"{name}.json"))
    with open(DATA / "portal_data.js", "w") as fh:
        fh.write("window.PORTAL_DATA = " + json.dumps(bundle) + ";")

if __name__ == "__main__":
    def step(msg):
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)
    g20 = shape(json.load(open(DATA / "boundary.json"))["features"][0]["geometry"]).buffer(0)
    prev = json.load(open(DATA / "maplayers.json")) if (DATA / "maplayers.json").exists() else {}
    step("map layers: precincts, shootings, subway, Citi Bike, bus, evictions")
    json.dump(build(g20, step, prev), open(DATA / "maplayers.json", "w"))
    bake()
    step("wrote data/maplayers.json and re-baked data/portal_data.js — DONE")
