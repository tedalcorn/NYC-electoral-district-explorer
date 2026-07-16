#!/usr/bin/env python3
"""
Daily refresh for the District Portal.

Re-fetches only the volatile feeds — local news + legislation — reuses the committed
static data (boundary, demographics, map layers, member), and re-bakes data/portal_data.js.
Runs in CI daily. For a full rebuild (Census, map layers, etc.) use build_portal.py.

Robustness rule: if a feed fetch comes back empty (API hiccup), keep the previously
committed file rather than blanking the page. Uses only the Python standard library.
"""
import json, os, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
try:
    from zoneinfo import ZoneInfo
    ET_TZ = ZoneInfo("America/New_York")
except Exception:
    ET_TZ = timezone.utc

DATA = Path(__file__).resolve().parent.parent / "data"
NOW = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")
UA = {"User-Agent": "Mozilla/5.0 (NYU Senior Seminar class project; contact ted.alcorn@gmail.com)"}
LEG_KEY = os.environ.get("NYSENATE_API_KEY", "")

def fetch(url, timeout=45):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read()

def jfetch(url, timeout=45):
    return json.loads(fetch(url, timeout))

def step(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

# ---------------------------------------------------------------- news (mirrors build_portal.py section 5)
step("news: Google News RSS, neighborhood + member queries, last 180 days")
NEWS_QUERIES = ['"Zellnor Myrie"', '"Crown Heights" Brooklyn', '"Prospect Heights" Brooklyn',
                '"Prospect Lefferts Gardens"', '"East Flatbush"', '"Wingate" Brooklyn', '"Ditmas Park"',
                '"Prospect Park" Brooklyn', '"Flatbush" Brooklyn', '"Lefferts" Brooklyn']
TOPICS = [
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
NEWS_EXCLUDE_OUTLETS = {"MaxPreps"}
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
                title = title[:-(len(src_text) + 3)].strip()
            articles.append({"title": title, "link": (it.findtext("link") or "").strip(), "outlet": src_text,
                             "date_iso": dt_et.strftime("%Y-%m-%dT%H:%M"), "date_display": dt_et.strftime("%d/%m/%y, %-H:%M"),
                             "topic": tag(title), "query": q})
            kept += 1
        step(f"   {q}: +{kept}")
        time.sleep(1.5)
    except Exception as e:
        step(f"   {q} FAILED ({e})")
if articles:
    articles.sort(key=lambda a: a["date_iso"], reverse=True)
    news = {"retrieved": NOW, "window_days": CUTOFF_DAYS, "total": len(articles),
            "topic_counts": dict(Counter(a["topic"] for a in articles).most_common()),
            "top_outlets": Counter(a["outlet"] for a in articles).most_common(12),
            "queries": NEWS_QUERIES, "articles": articles[:200],
            "_prov": {"source": "Google News RSS search — one query per SD-20 neighborhood plus one for Sen. Myrie",
                      "url": "https://news.google.com/rss/search?q=<query>", "retrieved": NOW,
                      "method": (f"{len(NEWS_QUERIES)} queries (listed in 'queries'); items deduplicated by headline, filtered "
                                 f"to the last {CUTOFF_DAYS} days by publication date, tagged to a topic by keyword match, sorted "
                                 f"newest first. {len(articles)} unique articles collected; the page shows the {min(200, len(articles))} most recent."),
                      "caveats": ("Google News is relevance-ranked and caps each query near 100 results, so this is a broad sample, "
                                  "not a census of local coverage; a neighborhood name-match does not guarantee the story is inside "
                                  "SD-20; and topic tags are automated keyword guesses.")}}
    json.dump(news, open(DATA / "news.json", "w"))
    step(f"   wrote {len(articles)} articles")
else:
    step("   NO articles fetched — keeping the previously committed news.json")

# ---------------------------------------------------------------- legislation (mirrors build_portal.py section 8)
step("legislation: recent Senate bills sponsored by Myrie (memberId 1228)")
SESSION = 2025
LEG_TOPICS = [
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
    return {"printNo": pn, "title": r.get("title") or "", "role": role,
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
    if bills:
        legislation = {"session": SESSION, "n_leads": len(prime_no),
                       "n_cosponsor": sum(1 for b in bills if b["role"] == "Co-sponsor"), "bills": bills,
                       "_prov": {"source": "New York State Senate, Open Legislation API",
                                 "url": "https://legislation.nysenate.gov/api/3/bills/search", "retrieved": NOW,
                                 "method": (f"Session {SESSION} Senate bills (ceremonial resolutions excluded). 'Prime sponsor' = Myrie "
                                            f"is prime sponsor; 'Co-sponsor' = he is a listed co-sponsor. Date shown is the introduction "
                                            f"date; subject tags are keyword-matched on the bill title and summary."),
                                 "caveats": ("Status is the latest recorded action ('In Senate Committee' = introduced, not yet advanced). "
                                             "Subject tags are automated approximations. He co-sponsors far more bills than he leads.")}}
        json.dump(legislation, open(DATA / "legislation.json", "w"))
        step(f"   wrote {len(prime_no)} prime + {legislation['n_cosponsor']} co-sponsored")
    else:
        step("   NO bills fetched — keeping the previously committed legislation.json")
except Exception as e:
    step(f"   legislation FAILED ({e}) — keeping the previously committed legislation.json")

# ---------------------------------------------------------------- re-bake portal_data.js from all committed JSON
out = {}
for name in ("boundary", "trends", "people", "turf", "news", "headline", "member", "maplayers", "legislation"):
    out[name] = json.load(open(DATA / f"{name}.json"))
with open(DATA / "portal_data.js", "w") as fh:
    fh.write("window.PORTAL_DATA = " + json.dumps(out) + ";")
step("re-baked data/portal_data.js — DONE")
