"""Bills sponsored by a member, from the NY Senate Open Legislation API (covers both chambers)."""
import time, urllib.parse
from common import jfetch, LEG_KEY, SESSION, CHAMBER

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

def search(term, cap=1500):
    items, offset, limit, total = [], 1, 50, 0
    while True:
        d = jfetch("https://legislation.nysenate.gov/api/3/bills/search?term=" + urllib.parse.quote(term) +
                   f"&limit={limit}&offset={offset}&key=" + LEG_KEY, 60)
        if not d.get("success"):
            break
        total = d.get("total", 0)
        its = d["result"]["items"]
        items += its
        if len(its) < limit or len(items) >= min(total, cap):
            break
        offset += limit
        time.sleep(0.3)
    return items

def row(r, role):
    st = r.get("status") or {}
    acts = (r.get("actions") or {}).get("items") or []
    pn = r.get("basePrintNo")
    return {"printNo": pn, "title": r.get("title") or "", "role": role,
            "introduced": acts[0]["date"] if acts else "",
            "status": st.get("statusDesc") or "", "statusDate": st.get("actionDate") or "",
            "topic": leg_topic((r.get("title") or "") + " " + (r.get("summary") or "")),
            "url": f"https://www.nysenate.gov/legislation/bills/{SESSION}/{pn}"}

def build(member_id, short_name, chamber, last_name, now):
    """Returns the legislation.json payload, or None if nothing came back."""
    pfx = CHAMBER[chamber]["print"]
    prime = search(f"sponsor.member.memberId:{member_id} AND session:{SESSION} AND basePrintNo:{pfx}*")
    prime_no = {it["result"]["basePrintNo"] for it in prime}
    co = search(f"amendments.items.\\*.coSponsors.items.shortName:{short_name} AND session:{SESSION} AND basePrintNo:{pfx}*")
    bills, seen = [], set()
    for it in prime:
        r = it["result"]
        if r["basePrintNo"] not in seen:
            seen.add(r["basePrintNo"]); bills.append(row(r, "Prime sponsor"))
    for it in co:
        r = it["result"]
        if r["basePrintNo"] not in seen and r["basePrintNo"] not in prime_no:
            seen.add(r["basePrintNo"]); bills.append(row(r, "Co-sponsor"))
    if not bills:
        return None
    bills.sort(key=lambda b: b["introduced"], reverse=True)
    house = "Senate" if chamber == "senate" else "Assembly"
    return {"session": SESSION, "n_leads": len(prime_no), "n_cosponsor": sum(1 for b in bills if b["role"] == "Co-sponsor"),
            "bills": bills,
            "_prov": {"source": "New York State Senate, Open Legislation API",
                      "url": "https://legislation.nysenate.gov/api/3/bills/search", "retrieved": now,
                      "method": (f"Session {SESSION} {house} bills (ceremonial resolutions excluded). 'Prime sponsor' = {last_name} "
                                 f"is prime sponsor; 'Co-sponsor' = a listed co-sponsor. Date shown is the introduction "
                                 f"date; subject tags are keyword-matched on the bill title and summary."),
                      "caveats": ("Status is the latest recorded action ('In Committee' = introduced, not yet advanced). "
                                  "Subject tags are automated approximations. Members co-sponsor far more bills than they lead.")}}
