/* Static filtering for the GitHub Pages build.

   server.py answers /api/events by filtering parsed events in Python. Pages has
   no server, so the whole statewide snapshot ships as one file and the browser
   filters it here. That means TWO implementations of the same rules, so this
   file deliberately mirrors, and is diff-tested against:

     src/nearme/geo.py  - haversine + local-tangent-plane point-to-polyline
     src/nearme/api.py  - in_bbox() (0.04 deg pad, point OR pavement trace),
                          level filter, "within N miles of the anchor"

   Pure functions, no DOM: tests/test_static_parity.js runs this under Node and
   compares against the Python output. Do not "improve" one side alone. */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.NearmeStatic = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var EARTH_MI = 3958.7613;
  var BBOX_PAD = 0.04; // same as api.py: a trace crossing the edge still draws

  function haversineMi(a, b) {
    var p1 = (a[0] * Math.PI) / 180,
      p2 = (b[0] * Math.PI) / 180;
    var dp = p2 - p1,
      dl = ((b[1] - a[1]) * Math.PI) / 180;
    var h =
      Math.sin(dp / 2) * Math.sin(dp / 2) +
      Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) * Math.sin(dl / 2);
    return 2 * EARTH_MI * Math.asin(Math.sqrt(h));
  }

  /* Miles on a local tangent plane, then distance to the nearest segment.
     Origin is the segment start, exactly like geo.py's _to_xy(p, a). */
  function distToSegmentMi(p, a, b) {
    var lat0 = (a[0] * Math.PI) / 180;
    var px = (((p[1] - a[1]) * Math.PI) / 180) * EARTH_MI * Math.cos(lat0);
    var py = (((p[0] - a[0]) * Math.PI) / 180) * EARTH_MI;
    var bx = (((b[1] - a[1]) * Math.PI) / 180) * EARTH_MI * Math.cos(lat0);
    var by = (((b[0] - a[0]) * Math.PI) / 180) * EARTH_MI;
    var d2 = bx * bx + by * by;
    if (d2 === 0) return Math.hypot(px, py);
    var t = Math.max(0, Math.min(1, (px * bx + py * by) / d2));
    return Math.hypot(px - t * bx, py - t * by);
  }

  function distToPolylineMi(p, pts) {
    var best = Infinity;
    for (var i = 0; i + 1 < pts.length; i++)
      best = Math.min(best, distToSegmentMi(p, pts[i], pts[i + 1]));
    if (best === Infinity)
      best = pts.reduce(function (m, q) {
        return Math.min(m, haversineMi(p, q));
      }, Infinity);
    return best;
  }

  /* GeoJSON geometry is [lon, lat]; the distance math uses [lat, lon]. */
  function geometryPoints(f) {
    var g = f.geometry;
    if (!g) return [];
    if (g.type === "Point") return [[g.coordinates[1], g.coordinates[0]]];
    return g.coordinates.map(function (c) {
      return [c[1], c[0]];
    });
  }

  /* One-time flattening per snapshot: _geom (geometry only), _trace (the LCS
     pavement polyline), _pts (both - what the bbox test looks at). */
  function prepareFeature(f) {
    var geom = geometryPoints(f);
    var trace = f.properties.trace
      ? f.properties.trace.map(function (c) {
          return [c[1], c[0]];
        })
      : null;
    f._geom = geom;
    f._trace = trace && trace.length > 1 ? trace : null;
    f._pts = f._trace ? geom.concat(f._trace) : geom;
    return f;
  }

  function prepareAll(features) {
    features.forEach(prepareFeature);
    return features;
  }

  /* Distance from [lat, lon] to a feature: its geometry, or the nearer of
     geometry / trace - mirrors how api.py takes min over both. */
  function distToFeatureMi(p, f) {
    var d =
      f.geometry && f.geometry.type === "LineString"
        ? distToPolylineMi(p, f._geom)
        : haversineMi(p, [f.geometry.coordinates[1], f.geometry.coordinates[0]]);
    if (f._trace) d = Math.min(d, distToPolylineMi(p, f._trace));
    return d;
  }

  function inBBox(f, bbox) {
    var w = bbox.west - BBOX_PAD,
      s = bbox.south - BBOX_PAD,
      e = bbox.east + BBOX_PAD,
      n = bbox.north + BBOX_PAD;
    return f._pts.some(function (pt) {
      return pt[1] >= w && pt[1] <= e && pt[0] >= s && pt[0] <= n;
    });
  }

  /* opts: {bbox: {west,south,east,north} | null,
            levels: Set|array of level names | null,
            center: [lat, lon] | null,
            radius: miles | null}
     Returns new feature objects (the snapshot is never mutated), with
     properties.distance_mi set when a center is given - the server does the same,
     and the list sorts by it. */
  function filterFeatures(features, opts) {
    opts = opts || {};
    var levels = opts.levels ? new Set(opts.levels) : null;
    var out = [];
    for (var i = 0; i < features.length; i++) {
      var f = features[i];
      if (levels && !levels.has(f.properties.level)) continue;
      if (opts.bbox && !inBBox(f, opts.bbox)) continue;
      var d = null;
      if (opts.center) {
        d = distToFeatureMi(opts.center, f);
        if (opts.radius != null && d > opts.radius) continue;
      }
      var props = Object.assign({}, f.properties);
      if (d != null) props.distance_mi = Math.round(d * 100) / 100;
      out.push({
        type: "Feature",
        geometry: f.geometry,
        properties: props,
        _geom: f._geom,
        _trace: f._trace,
        _pts: f._pts,
      });
    }
    return out;
  }

  return {
    EARTH_MI: EARTH_MI,
    BBOX_PAD: BBOX_PAD,
    haversineMi: haversineMi,
    distToSegmentMi: distToSegmentMi,
    distToPolylineMi: distToPolylineMi,
    prepareFeature: prepareFeature,
    prepareAll: prepareAll,
    distToFeatureMi: distToFeatureMi,
    inBBox: inBBox,
    filterFeatures: filterFeatures,
  };
});
