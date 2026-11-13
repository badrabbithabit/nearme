# nearme — "what's closed on my way?"

CLI that answers one question fast: **are there lane closures near me right now**,
using CHP incident reports (including what motorists phoned in) plus the Caltrans
Lane Closure System. There is also a map — `python server.py` for the full live
version, or GitHub Pages for a snapshot build (see [The map](#the-map)).

```
python nearme.py near --at 95616 --radius 20 --lanes-only
python nearme.py near --at home --route 50            # my commute route
python nearme.py near --at "Davis, CA" --json         # for scripting
python nearme.py watch --at home --interval 180       # refresh loop
python nearme.py layers                               # what else is available
```

## Where the data comes from

QuickMap (`quickmap.dot.ca.gov`) is the public face of the same feed the CHP
Traffic Incident page uses, but — unlike `cad.chp.ca.gov/traffic.aspx`, which is a
stateful ASP.NET WebForms grid with no coordinates — QuickMap's layers are **static
KML files with real latitude/longitude**, which is what makes "near me" possible.

| Endpoint | Contents |
| --- | --- |
| `/config/layers.json` | layer id → KML file(s); fetched once a day, drives `layers` |
| `/data/v2_chp-only.kml` | ~200 live CHP incidents, geocoded, with CAD log notes |
| `/data/v2_lcs2way.kml` | ~400 Caltrans lane closures (active) + closure polylines |
| `/data/v2_lcspending.kml` | closures not yet active |
| `/data/v2_gi.kml`, `v2_cc.kml`, `v2_chin.kml`, … | general incidents, chain control, highway info |

Each `Placemark`'s `<description>` holds a pre-rendered Google Maps infowindow
(HTML), so `kml.py` parses that HTML into fields. There is **no JSON endpoint**
(`.json`/`.geojson` 404), and the CHP page itself has no API.

Refresh: the origin stamps `Last-Modified`/`ETag` and regenerates the files roughly
every 3 minutes (`updateIntervalMinutes: 3` in `config/app.config.json`). This tool
does conditional GETs, caches in `.cache/`, refuses to re-fetch faster than
`--min-refresh` (default 180 s), and answers from cache if the network is down.

## Reading the output

```
  1.0 mi  CHP      11/12 06:55  1182-Trfc Collision-No Inj
         where : I5 S / Jno 50
         closure : LANE CLOSURE / southbound
         quote   : 'BLKING SLO / SB 5 JNO Q' // 'IN SLOW LN'
         source  : reporting party
         id      : 261112SAFSP0004, 261112SA0108
         log     : 11/12 06:55 [1] WHI HOND CRV VS RED CHEV SLV
```

`closure` level, in severity order: `FULL CLOSURE` > `ONE-WAY TRAFFIC` >
`LANE CLOSURE` > `SHOULDER` > `BLOCKAGE` > `no closure reported`.
`--lanes-only` keeps `LANE CLOSURE` and worse.

### The part that matters: authoritative vs inferred

* **Caltrans LCS records are explicit** — "Southbound 160 Lane Closure",
  "1 of 4 general purpose lanes closed", a closure ID, an end time. Trust these.
* **CHP incidents are free text.** A CHP "event" is a dispatcher's transcription of
  a call from a motorist (the `RP` in CAD notes = *reporting party*), e.g.
  `PER DUPE BLK LEXUS VS SD BLKG # 1 LANE`. `lane_closure.py` infers a level from
  that shorthand and always prints the `quote` it matched. **The quote is the
  evidence; the label is a guess.** CHP CAD abbreviations matter: `LN/LNS` = lane,
  `SLO` = slow, `BLKG/BLK` = blocking, `CLSD` = closed, `SLD` = shoulder,
  `RP` = reporting party.
* `source : reporting party` means a civilian called it in — fresher, but also the
  least verified. `source : chp unit` means a trooper is on scene.
* One crash often appears as two records (a local CHP record plus an FSP
  `[Shared]` record from the neighbouring com centre); those are merged, and both
  IDs are shown.

## The map

```
python server.py                    # http://127.0.0.1:8388
python server.py --port 9000 --layers chp_incidents,lane_closures,chain_controls
```

Stdlib only. `server.py` fetches QuickMap at most once per `--refresh` (90 s) no
matter how many tabs poll, serves `web/` (vanilla JS + vendored Leaflet), and
exposes `/api/events` (GeoJSON, filtered server-side), `/api/meta`,
`/api/geocode`, `/api/refresh`. The browser never touches the state agency.

### Hosting it on GitHub Pages (no server)

Pages serves files, not processes, so the published map is a **snapshot**:

```
python build_site.py --out _site    # copies web/ + writes nearme.json (one statewide FeatureCollection)
```

`.github/workflows/publish.yml` does that on a 30-minute schedule (and on push)
and deploys `_site` with the official Pages actions. The app probes `/api/meta` at
boot; when nothing answers it switches to **static mode** and `web/static.js`
filters `nearme.json` in the browser — bbox, severity, radius.

That is a second implementation of the same filtering rules, so
`TestStaticParity` runs `web/static.js` under Node and diffs its output against
`src/nearme/api.py` for seven query shapes (including the case only the bbox pad
can catch). Change one side without the other and the CI job fails.

What differs in static mode: the data is up to ~30–45 min stale and the UI says so;
polling is every 5 min instead of 90 s; place search calls Nominatim directly
instead of `/api/geocode`; and a build whose feeds parse to zero events exits
non-zero, so a flaky upstream leaves the last good deployment in place.

`python tests/smoke_browser.py` (needs Playwright) boots both modes in headless
Chromium and fails if either renders an empty map or throws in the console.

## Flags

`--at` coordinates (`"38.58,-121.49"`), ZIP, place name, or a name from
`config.json` `places`. `--radius` miles · `--layers` any id from `layers` ·
`--lanes-only` · `--route 50` (matches I-50 and SR-50) · `--grep REGEX` ·
`--max-age MINUTES` · `--max N` · `--notes N` · `--json` · `--force` (bypass TTL).

## Config

Copy `config.example.json` → `config.json` (gitignored):

```json
{ "at": "home", "radius_mi": 15,
  "layers": ["chp_incidents", "lane_closures"],
  "places": { "home": "38.5816,-121.4944", "work": "38.555,-121.5" } }
```

## Tests

```
python tests/test_nearme.py
```

Offline, no third-party deps. `tests/fixtures/*.kml` are real placemarks trimmed
from live responses, so they pin the actual upstream format (MultiGeometry closure
traces, `[Appended, 07:04:13]` note prefixes, doubled unit tags, `LNS OF TRFC`
shorthand). The parity test shells out to Node when it is on PATH and skips
otherwise.

## Honest limits

* Unofficial mirror of a public display — no contract, no documented schema, can
  change or start blocking scripts. Attribution belongs to CHP / Caltrans.
* Statewide KML ≈ 250–700 KB per fetch; fine by hand, don't poll sub-second.
* Nominatim (OSM) geocoding has a 1 req/s courtesy limit and results are cached.
* Incident coordinates are approximate (often an cross-street centroid), and CHP
  "Area" labels are city-level. A 1-mile radius will miss things; 5–15 mi is sane.
* Distances to closures use the polyline when available, so a 3-mile-long
  closure registers as 0 mi if you are inside it.
* Nothing here knows your direction of travel. `--route` + `closure.direction`
  are the current proxies.

## Plausible next steps

1. Bearing/direction filter — compute which side of the road the event is on from
   the polyline heading, so "SB 5" closures don't show up for a NB commute.
2. `--commute work` — geocode a polyline between two places and report events
   within N miles of the *route*, not of a point.
3. Alerts: diff successive polls and notify on new `full`/`lanes` events inside the
   radius (Firebase push is what QuickMap itself uses; a local notifier is easier).
4. Cross-check `cad.chp.ca.gov/traffic.aspx` per communications centre for the raw
   incident list when QuickMap's mirror lags.
5. Persist a history (SQLite) so "how long do SR50 closures usually last" becomes
   answerable.
