/* Node harness for the browser-side filter used by the GitHub Pages build.

   The Pages site has no server, so web/static.js re-implements what
   src/nearme/api.py does. This harness lets tests/test_nearme.py diff the two:
   it reads {snapshot, queries} on stdin and writes, per query, the surviving
   uids and their distances.

     node tests/static_parity.js < payload.json
*/
"use strict";

const NS = require("../web/static.js");

let raw = "";
process.stdin.on("data", (d) => (raw += d));
process.stdin.on("end", () => {
  const { snapshot, queries } = JSON.parse(raw);
  NS.prepareAll(snapshot.features);
  const out = queries.map((q) => ({
    key: q.key,
    features: NS.filterFeatures(snapshot.features, {
      // python hands over bbox as [lon_min, lat_min, lon_max, lat_max]
      bbox: q.bbox
        ? { west: q.bbox[0], south: q.bbox[1], east: q.bbox[2], north: q.bbox[3] }
        : null,
      levels: q.levels || null,
      center: q.center || null,
      radius: q.radius == null ? null : q.radius,
    }).map((f) => ({
      uid: f.properties.uid,
      distance_mi: f.properties.distance_mi == null ? null : f.properties.distance_mi,
    })),
  }));
  process.stdout.write(JSON.stringify(out));
});
