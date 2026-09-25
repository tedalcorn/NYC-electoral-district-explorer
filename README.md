# NYC Electoral District Explorer

A one-page dashboard that collates publicly available information about a single New York
legislative district, so students can find it fast and then verify it at the source. Built as a
pilot for an NYU Wagner senior seminar. It is a starting point for reporting, **never a citation**.

The current pilot is built for **NY State Senate District 20 (Sen. Zellnor Myrie), Central Brooklyn**.

## What it shows
- **Map** with thematic layers — District, Community districts, Criminal justice (precincts + shootings),
  Transit (subway lines in MTA colors, bus routes, Citi Bike), Evictions, Income & demographics by census
  tract, Schools & early education, Food stores.
- **Country of birth and language at home** for the district (ACS).
- **Who are residents** — population, income, foreign-born share, median age vs. the citywide figure (ACS).
- **Census detail** — race/ethnicity and age vs. NYC, population and income over time (with the 2022
  redistricting break marked).
- **Background on the member** — elections with vote totals, committees, signature priorities, socials.
- **Legislation** — bills he prime-sponsors vs. co-sponsors, filterable by subject (NY Senate Open Legislation API).
- **Local news** — the last six months from news organizations only, with coverage of the same event
  grouped into one story, tagged by topic, filterable.

## How it's built
One shared page, many districts. `index.html` reads `?d=<code>` (e.g. `?d=AD-43`) and loads that
district's data from `data/<code>/`; a sticky bar at the top switches between every district that
has been built. Everything specific to a district lives in data, so an edit to `index.html` applies
to all of them at once.

```
districts/<code>.json             one small config per district (chamber, number, any hand-compiled facts)
python3 build/district.py SD-20   build or rebuild one district → data/SD-20/ (needs geopandas + both keys)
python3 build/district.py AD-43 --skip-maps    everything but the slow map layers
python3 build/maplayers.py AD-43  map layers only (runs monthly in CI for every district)
python3 build/refresh.py          news + legislation for every district (runs daily in CI)
open "index.html?d=SD-20"         view
```
To add a district: create `districts/<code>.json` with `{"code":"SD-25","chamber":"senate","number":25}`
(or `"assembly"`), run `build/district.py <code>`, commit `data/<code>/` and `data/districts.js`.
Member facts the APIs don't carry (election history, priorities, committees, social accounts) and each
community board's needs go in the config under `curated`; see `districts/SD-20.json` for the shape.
Citywide inputs (TIGER lines, borough/council/community-district boundaries, NTAs, citywide ACS) are
downloaded once into `build/cache/` and reused.

### Keys (set as environment variables or CI secrets — not committed)
- `CENSUS_API_KEY` — required as of 2026 ([request one](https://api.census.gov/data/key_signup.html)).
- `NYSENATE_API_KEY` — required for the legislation feed ([legislation.nysenate.gov](https://legislation.nysenate.gov/)).

Ground rules: no invented data (a failed fetch shows an honest gap); every pane names its source,
retrieval date, and caveats; charts start their y-axis at zero. The prompt behind the build is in
`prompt.txt`.
