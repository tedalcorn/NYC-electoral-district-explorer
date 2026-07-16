# NYC Electoral District Explorer

A one-page dashboard that collates publicly available information about a single New York
legislative district, so students can find it fast and then verify it at the source. Built as a
pilot for an NYU Wagner senior seminar. It is a starting point for reporting, **never a citation**.

The current pilot is built for **NY State Senate District 20 (Sen. Zellnor Myrie), Central Brooklyn**.

## What it shows
- **Map** with thematic layers — District, Criminal justice (precincts + shootings), Transit
  (subway, bus, Citi Bike), Housing (evictions).
- **Who are residents** — population, income, foreign-born share, median age vs. the citywide figure (ACS).
- **Census detail** — race/ethnicity and age vs. NYC, population and income over time (with the 2022
  redistricting break marked).
- **Background on the member** — elections with vote totals, committees, signature priorities, socials.
- **Legislation** — bills he prime-sponsors vs. co-sponsors, filterable by subject (NY Senate Open Legislation API).
- **Local news** — the last six months across local outlets, deduplicated, tagged by topic, filterable.

## How it's built
`build/build_portal.py` fetches each source, writes one JSON per pane (each with a provenance block)
into `data/`, and bakes them into `data/portal_data.js`. `index.html` is presentation only and runs
from a static file or GitHub Pages — no server.

```
python3 build/build_portal.py     # regenerate the data
open index.html                   # view
```

### Keys (set as environment variables or CI secrets — not committed)
- `CENSUS_API_KEY` — optional for low volume ([request one](https://api.census.gov/data/key_signup.html)).
- `NYSENATE_API_KEY` — required for the legislation feed ([legislation.nysenate.gov](https://legislation.nysenate.gov/)).

Ground rules: no invented data (a failed fetch shows an honest gap); every pane names its source,
retrieval date, and caveats; charts start their y-axis at zero. The prompt behind the build is in
`prompt.txt`.
