#!/usr/bin/env python3
"""
Local-news feed for the District Portal — shared by build_portal.py (full build) and
refresh.py (daily job). Standard library only.

Pipeline: Google News RSS queries -> keep only items from news organizations (domain
allowlist below) -> collapse duplicate listings -> group stories about the same event ->
tag a topic. Everything dropped is counted and reported in the output, never hidden.
"""
import json, math, re, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
try:
    from zoneinfo import ZoneInfo
    ET_TZ = ZoneInfo("America/New_York")
except Exception:
    ET_TZ = timezone.utc

UA = {"User-Agent": "Mozilla/5.0 (NYU Senior Seminar class project; contact ted.alcorn@gmail.com)"}
CUTOFF_DAYS = 180

# One query per neighborhood + the member, then subject-focused queries. Left alone, neighborhood
# searches return mostly crime briefs from local TV; the subject queries pull in the rest.
HOODS = '("Crown Heights" OR "Prospect Heights" OR "Flatbush" OR "Prospect Lefferts Gardens")'
NEWS_QUERIES = [
    '"Zellnor Myrie"', '"Crown Heights" Brooklyn', '"Prospect Heights" Brooklyn',
    '"Prospect Lefferts Gardens"', '"East Flatbush"', '"Wingate" Brooklyn', '"Ditmas Park"',
    '"Prospect Park" Brooklyn', '"Flatbush" Brooklyn', '"Lefferts" Brooklyn',
    HOODS + ' (housing OR tenants OR rent OR rezoning OR landlord)',
    HOODS + ' (school OR students OR teachers OR CUNY)',
    HOODS + ' (subway OR bus OR MTA OR "bike lane" OR traffic)',
    HOODS + ' (hospital OR health OR clinic)',
    HOODS + ' ("small business" OR restaurant OR jobs OR storefront)',
    HOODS + ' ("community board" OR "City Council" OR Assembly OR "State Senate")',
]

# Domain -> display name. News organizations only: no apps (Citizen), government or corporate
# press offices, listings/marketing sites, or aggregators. To add an outlet, add its domain here.
OUTLETS = {
    # citywide dailies, broadcast, wire
    "nytimes.com": "The New York Times", "nydailynews.com": "New York Daily News", "nypost.com": "New York Post",
    "amny.com": "amNewYork", "gothamist.com": "Gothamist", "thecity.nyc": "THE CITY", "hellgatenyc.com": "Hell Gate",
    "ny1.com": "Spectrum News NY1", "spectrumlocalnews.com": "Spectrum News NY1", "abc7ny.com": "ABC7 New York",
    "nbcnewyork.com": "NBC New York", "cbsnews.com": "CBS News", "pix11.com": "PIX11", "fox5ny.com": "FOX 5 New York",
    "news12.com": "News 12", "1010wins.com": "1010 WINS", "audacy.com": "Audacy", "wnyc.org": "WNYC",
    "silive.com": "SILive.com", "apnews.com": "Associated Press", "reuters.com": "Reuters", "bloomberg.com": "Bloomberg",
    "usatoday.com": "USA Today", "npr.org": "NPR", "pbs.org": "PBS", "theatlantic.com": "The Atlantic",
    "nymag.com": "New York Magazine", "curbed.com": "Curbed", "grubstreet.com": "Grub Street", "observer.com": "Observer",
    # Brooklyn and neighborhood press
    "brooklynpaper.com": "Brooklyn Paper", "brooklyneagle.com": "Brooklyn Eagle", "bkreader.com": "BKReader",
    "bkmag.com": "BKMAG", "bklyner.com": "Bklyner", "brownstoner.com": "Brownstoner", "patch.com": "Patch",
    "ourtimepress.com": "Our Time Press", "canarsiecourier.com": "Canarsie Courier",
    "brooklynreporter.com": "The Brooklyn Home Reporter", "thecityreporter.nyc": "The City Reporter",
    "journalism.blog.brooklyn.edu": "Brooklyn News Service", "nycitynewsservice.com": "New York City News Service",
    "indypendent.org": "The Indypendent", "epicenter-nyc.com": "Epicenter NYC",
    # community and ethnic press
    "collive.com": "COLlive", "crownheights.info": "CrownHeights.info", "anash.org": "Anash.org", "haitiantimes.com": "The Haitian Times",
    "caribbeanlife.com": "Caribbean Life", "amsterdamnews.com": "New York Amsterdam News", "jta.org": "Jewish Telegraphic Agency",
    "forward.com": "The Forward", "documentedny.com": "Documented",
    # politics and policy
    "cityandstateny.com": "City & State New York", "politico.com": "Politico", "politicsny.com": "PoliticsNY",
    "capitolpressroom.org": "The Capitol Pressroom", "timesunion.com": "Times Union", "nysfocus.com": "New York Focus",
    "chalkbeat.org": "Chalkbeat", "the74million.org": "The 74", "thetrace.org": "The Trace", "city-journal.org": "City Journal",
    "nyc.streetsblog.org": "Streetsblog NYC", "nextcity.org": "Next City",
    # real estate and business trade press
    "newyorkyimby.com": "New York YIMBY", "therealdeal.com": "The Real Deal", "crainsnewyork.com": "Crain's New York",
    "commercialobserver.com": "Commercial Observer", "6sqft.com": "6sqft", "brickunderground.com": "Brick Underground",
    "bisnow.com": "Bisnow", "bizjournals.com": "The Business Journals", "archpaper.com": "The Architect's Newspaper",
    # food and culture
    "ny.eater.com": "Eater New York", "timeout.com": "Time Out", "hyperallergic.com": "Hyperallergic",
    "brooklynvegan.com": "BrooklynVegan", "untappedcities.com": "Untapped New York",
}

def outlet_for(src_url):
    """Canonical outlet name for a source URL, or None if the domain is not on the allowlist.
    Subdomains match their parent (brooklyn.news12.com -> news12.com), so the seven regional
    News 12 sites that each re-list the same story collapse into one outlet."""
    host = re.sub(r"^https?://", "", (src_url or "").lower()).split("/")[0]
    host = re.sub(r"^www\.", "", host)
    while host:
        if host in OUTLETS:
            return OUTLETS[host]
        if "." not in host:
            return None
        host = host.split(".", 1)[1]
    return None

# ---------------------------------------------------------------- topic tags (whole-word match, first hit wins)
TOPICS = [
    ("Politics & elections", ["myrie", "senator", "senate", "mayor", "mayoral", "council", "councilmember", "assembly",
                              "assemblymember", "election", "primary", "campaign", "legislation", "mamdani", "governor",
                              "hochul", "albany", "lawmaker", "lawmakers", "ballot", "community board"]),
    ("Crime & safety", ["shooting", "shootings", "shot", "shots", "gunshots", "gunfire", "gunman", "police", "nypd", "cops",
                        "crime", "arrest", "arrested", "arrests", "violence", "gun", "guns", "stabbing", "stabbed", "stab",
                        "slashed", "robbery", "robbed", "assault", "assaulted", "killed", "homicide", "murder", "suspect",
                        "kidnap", "kidnapping", "rape", "predator", "wanted", "charged", "indicted", "sentenced", "missing",
                        "hit-and-run", "attack", "attacked", "attacks", "dead", "deadly", "fatally", "victim", "sketch"]),
    ("Housing & development", ["housing", "rent", "rents", "rental", "tenant", "tenants", "eviction", "evictions", "affordable",
                              "development", "developer", "developers", "rezoning", "rezone", "zoning", "construction",
                              "real estate", "landlord", "landlords", "condo", "condos", "nycha", "deed", "brownstone",
                              "permits", "lottery", "landmarks", "lpc", "listings", "homes", "townhouse", "apartments",
                              "renderings", "excavation", "supportive"]),
    ("Schools & education", ["school", "schools", "student", "students", "teacher", "teachers", "education", "college",
                             "university", "pre-k", "cuny", "principal", "classroom"]),
    ("Transit & streets", ["subway", "mta", "train", "trains", "transit", "bike", "bikes", "traffic", "congestion pricing",
                           "bus", "buses", "citi bike", "crash", "crashes", "collision", "pedestrian", "street redesign",
                           "bike lane", "dot", "open streets"]),
    ("Health", ["health", "hospital", "hospitals", "covid", "clinic", "medical", "mental health", "suny downstate", "downstate"]),
    ("Arts, parks & events", ["festival", "concert", "concerts", "parade", "carnival", "j'ouvert", "jouvert", "museum",
                              "library", "theater", "theatre", "tribute", "art", "artist", "artists", "exhibit", "exhibition",
                              "garden", "botanic", "celebration", "celebrates", "reenactment", "bandshell", "pow wow",
                              "film", "music", "playground", "lake", "greenest"]),
    ("Business & economy", ["business", "businesses", "restaurant", "restaurants", "store", "shop", "jobs", "retail", "opening",
                            "opens", "cafe", "bakery", "bar", "storefront"]),
]
_TOPIC_RX = [(label, re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(k) for k in kws) + r")(?![a-z0-9])"))
             for label, kws in TOPICS]

def tag(title):
    t = title.lower()
    for label, rx in _TOPIC_RX:
        if rx.search(t):
            return label
    return "Community & other"

# ---------------------------------------------------------------- same-event grouping
# Two headlines are treated as the same story when they were published within WINDOW_DAYS of each
# other and share most of their distinctive words, weighting rare words above common ones.
WINDOW_DAYS = 3
MAX_SPAN_DAYS = 7      # a follow-up a week later (an arrest, a sentencing) is a new story
_STOP = set("""a an the and or but of in on at to for from by with into near inside outside after before amid over under
 as is are was were be been being has have had it its his her their this that these those who whom which what when where
 why how not no new says say said sources source report reports reported video watch photos photo live update updates
 then still nyc ny york city police nypd cops officials authorities man woman people person""".split())

def _stem(w):
    if w.endswith("ies") or w.endswith("ied"):
        return w[:-3] + "y"
    for suf in ("ing", "ed", "ly", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            w = w[:-len(suf)]
            if suf in ("ing", "ed") and len(w) > 4 and w[-1] == w[-2]:   # kidnapp(ing) -> kidnap
                w = w[:-1]
            break
    return w

# Headlines about one incident drift as it develops ("stabbed to death" -> "charged in fatal stabbing"),
# so a few police-brief words are folded together before comparing.
_SYN = {}
for _canon, _words in {
    "fatal": "fatal fatally deadly dead death deaths killed kill kills killing dies died murder murdered homicide slain",
    "shoot": "shot shots shooting shootings shoot gunfire gunshots gunshot gunman gunned",
    "stab": "stab stabbed stabbing stabbings slashed slashing knifed",
    "arrest": "arrest arrested arrests custody charged charges accused indicted nabbed busted",
    "crash": "crash crashes crashed crashing slams slammed smashes smashed collision struck hit",
    "fire": "fire blaze flames",
}.items():
    for _w in _words.split():
        _SYN[_w] = _canon
_PLACES = set("brooklyn crown heights prospect park flatbush east lefferts gardens ditmas wingate".split())

def _tokens(title):
    t = re.sub(r"[’']s\b", "", title.lower())                    # possessives
    t = re.sub(r"(\d+)[- ]year[- ]old", r"\1yo", t)               # ages are distinctive: keep as one token
    words = [w for w in re.sub(r"[^a-z0-9]+", " ", t).split() if w not in _STOP and len(w) > 1]
    return {_SYN.get(w) or _stem(w) for w in words}

def _same_story(a, b, wt):
    shared = a["_tok"] & b["_tok"]
    if len(shared - _PLACES) < 3:                                 # a shared neighborhood is not a shared story
        return False
    ws = sum(wt[t] for t in shared)
    wa, wb = sum(wt[t] for t in a["_tok"]), sum(wt[t] for t in b["_tok"])
    return ws / min(wa, wb) >= 0.58 and ws / (wa + wb - ws) >= 0.33

def group_stories(articles):
    """articles: newest first. Returns lead stories, each with an 'also' list of the other listings."""
    for a in articles:
        a["_tok"] = _tokens(a["title"])
        a["_dt"] = datetime.strptime(a["date_iso"], "%Y-%m-%dT%H:%M")
    df = Counter(t for a in articles for t in a["_tok"])          # rare words (an address, a name) count for more
    wt = {t: math.log(1 + len(articles) / n) for t, n in df.items()}   # than common ones (a neighborhood, "shooting")
    clusters = []
    for a in sorted(articles, key=lambda x: x["_dt"]):            # oldest first, so the first report leads
        home = None
        for c in reversed(clusters):                              # clusters are ordered by their first report
            if (a["_dt"] - c[0]["_dt"]).days > MAX_SPAN_DAYS:
                break
            if any((a["_dt"] - m["_dt"]).days <= WINDOW_DAYS and _same_story(a, m, wt) for m in c):
                home = c
                break
        if home is not None:
            home.append(a)
        else:
            clusters.append([a])
    out = []
    for c in clusters:
        lead, rest = c[0], c[1:]
        also, seen_outlets = [], {lead["outlet"]}
        for m in rest:                                           # one link per additional outlet
            if m["outlet"] in seen_outlets:
                continue
            seen_outlets.add(m["outlet"])
            also.append({"outlet": m["outlet"], "link": m["link"], "title": m["title"]})
        story = {k: v for k, v in lead.items() if not k.startswith("_")}
        story["also"] = also
        story["n_listings"] = len(c)
        out.append(story)
    out.sort(key=lambda s: s["date_iso"], reverse=True)
    return out

# ---------------------------------------------------------------- fetch + assemble
def fetch_items(step=print, queries=NEWS_QUERIES):
    items = []
    for q in queries:
        try:
            url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(q) + "&hl=en-US&gl=US&ceid=US:en"
            root = ET.fromstring(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30).read())
            n = 0
            for it in root.iter("item"):
                src = it.find("source")
                items.append({"title": (it.findtext("title") or "").strip(), "link": (it.findtext("link") or "").strip(),
                              "outlet": (src.text if src is not None else "").strip(),
                              "src_url": (src.get("url") if src is not None else "") or "",
                              "pub": it.findtext("pubDate") or "", "query": q})
                n += 1
            step(f"   {q[:70]}: {n} items")
            time.sleep(1.5)
        except Exception as e:
            step(f"   {q[:70]} FAILED ({e})")
    return items

def build_news(items, now_str, queries=NEWS_QUERIES, code="SD-20", member="Sen. Myrie", hoods=None):
    """items: raw dicts from fetch_items. Returns the news.json payload, or None if nothing usable."""
    now = datetime.now(timezone.utc)
    articles, seen, excluded = [], set(), Counter()
    n_recent = n_dupe = 0
    for it in items:
        try:
            dt = parsedate_to_datetime(it["pub"])
        except Exception:
            continue
        age = (now - dt).days
        if not it["title"] or age > CUTOFF_DAYS or age < 0:
            continue
        n_recent += 1
        raw_name = it["outlet"] or "Unknown outlet"
        title = it["title"]
        if title.endswith(" - " + raw_name):                      # drop Google's trailing " - Outlet"
            title = title[:-(len(raw_name) + 3)].strip()
        outlet = outlet_for(it["src_url"])
        if outlet is None:
            excluded[raw_name] += 1
            continue
        key = (outlet, re.sub(r"[^a-z0-9]+", " ", title.lower()).strip())
        if key in seen:                                           # same headline, same outlet (incl. News 12 editions)
            n_dupe += 1
            continue
        seen.add(key)
        dt_et = dt.astimezone(ET_TZ)
        articles.append({"title": title, "link": it["link"], "outlet": outlet,
                         "date_iso": dt_et.strftime("%Y-%m-%dT%H:%M"), "topic": tag(title), "query": it["query"]})
    if not articles:
        return None
    articles.sort(key=lambda a: a["date_iso"], reverse=True)
    stories = group_stories(articles)
    n_ex = sum(excluded.values())
    return {
        "retrieved": now_str, "window_days": CUTOFF_DAYS, "total": len(stories), "n_listings": len(articles),
        "topic_counts": dict(Counter(s["topic"] for s in stories).most_common()),
        "queries": queries, "articles": stories,
        "excluded": {"n_items": n_ex, "n_sources": len(excluded), "top": excluded.most_common(12)},
        "_prov": {
            "source": "Google News RSS search",
            "url": "https://news.google.com/rss/search?q=<query>", "retrieved": now_str,
            "method": (f"{len(queries)} searches: one per {code} neighborhood, one for {member}, and six that pair the "
                       f"neighborhoods with a subject (housing, schools, transit, health, business, local government). "
                       f"Of {n_recent} results from the last {CUTOFF_DAYS} days, {n_ex} from {len(excluded)} sources that are "
                       f"not news organizations (apps such as Citizen, government and corporate press offices, listings "
                       f"sites) or not on the outlet list were set aside, and {n_dupe} repeat listings of the same headline "
                       f"were removed (News 12 posts one story to each of its regional sites). The remaining "
                       f"{len(articles)} articles were grouped into {len(stories)} stories: headlines published within "
                       f"{WINDOW_DAYS} days of each other that share most of their distinctive words count as one story, "
                       f"shown under the earliest report with the other outlets linked beside it."),
            "caveats": ("Google News is relevance-ranked and caps each search near 100 results, so this is a broad sample, "
                        "not a census of local coverage. A neighborhood name-match does not guarantee the story is inside "
                        f"{code}. Story grouping and topic tags are automated word-matching and will sometimes be wrong. "
                        "The outlet list is a judgment call; a legitimate outlet missing from it is dropped until added."),
        },
    }

if __name__ == "__main__":                                        # dry run: python3 build/news.py [raw.json]
    import sys
    raw = json.load(open(sys.argv[1])) if len(sys.argv) > 1 else fetch_items()
    news = build_news(raw, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ"))
    print(json.dumps({k: news[k] for k in ("total", "n_listings", "topic_counts", "excluded")}, indent=1))
