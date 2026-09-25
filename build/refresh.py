#!/usr/bin/env python3
"""
Daily refresh, every district: re-fetch news + legislation, reuse the committed static data, re-bake.
Runs in CI. Standard library only. For anything else use build/district.py <code>.

Robustness rule: if a feed comes back empty (API hiccup), the previously committed file is kept.
"""
import json, sys
from common import NOW, all_configs, bake, district_dir, step, write_registry
import news as newsfeed
import legislation

def news_queries(cfg, dist):
    """Same recipe as district.py: the member, each neighborhood, then six subject searches."""
    hoods = dist.get("neighborhoods") or []
    top = hoods[:4]
    hq = "(" + " OR ".join(f'"{h}"' for h in top) + ")" if top else '"' + (dist.get("area") or cfg["code"]) + '"'
    boro = (dist.get("boroughs") or ["Brooklyn"])[0]
    return [f'"{dist["member"]}"'] + [f'"{h}" {boro}' for h in hoods[:8]] + cfg.get("news_queries_extra", []) + [
        hq + ' (housing OR tenants OR rent OR rezoning OR landlord)', hq + ' (school OR students OR teachers OR CUNY)',
        hq + ' (subway OR bus OR MTA OR "bike lane" OR traffic)', hq + ' (hospital OR health OR clinic)',
        hq + ' ("small business" OR restaurant OR jobs OR storefront)', hq + ' ("community board" OR "City Council" OR Assembly OR "State Senate")'], top

only = [a for a in sys.argv[1:] if not a.startswith("--")]
for cfg in all_configs():
    code = cfg["code"]
    if only and code not in only:
        continue
    ddir = district_dir(code)
    if not (ddir / "district.json").exists():
        step(f"{code}: not built yet — run build/district.py {code} first; skipping")
        continue
    dist = json.load(open(ddir / "district.json"))
    mem = json.load(open(ddir / "member.json"))
    step(f"== {code} · {dist['member']}")
    queries, top = news_queries(cfg, dist)
    nd = newsfeed.build_news(newsfeed.fetch_items(step, queries), NOW, queries, code=code, member=dist["member"], hoods=top)
    if nd:
        json.dump(nd, open(ddir / "news.json", "w"))
        step(f"   news: {nd['total']} stories from {nd['n_listings']} articles; {nd['excluded']['n_items']} non-news items set aside")
    else:
        step("   news: NOTHING fetched — keeping the previous news.json")
    if mem.get("memberId"):
        try:
            ld = legislation.build(mem["memberId"], mem["shortName"], cfg["chamber"], mem.get("last_name") or dist["member_last"], NOW)
            if ld:
                json.dump(ld, open(ddir / "legislation.json", "w"))
                step(f"   legislation: {ld['n_leads']} prime + {ld['n_cosponsor']} co-sponsored")
            else:
                step("   legislation: NOTHING fetched — keeping the previous file")
        except Exception as e:
            step(f"   legislation FAILED ({e}) — keeping the previous file")
    bake(code)
write_registry()
step("re-baked every district — DONE")
