// Geometry for the 3D neuron view: deterministic seeds, a small 3D force layout and the perspective
// projection. No DOM access, so the same file can be checked in node without a browser.
(function (root) {
  "use strict";

  function hash(text) {
    let h = 2166136261;
    for (let i = 0; i < text.length; i++) {
      h ^= text.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return (h >>> 0) || 1;
  }

  // xorshift32: the same seed always gives the same neuron shape, so a topic looks the same each visit.
  function rng(seed) {
    let s = seed >>> 0 || 1;
    return function next() {
      s ^= s << 13;
      s ^= s >>> 17;
      s ^= s << 5;
      return (s >>> 0) / 4294967296;
    };
  }

  function randomUnit(next) {
    const z = next() * 2 - 1;
    const a = next() * Math.PI * 2;
    const r = Math.sqrt(Math.max(0, 1 - z * z));
    return { x: r * Math.cos(a), y: z, z: r * Math.sin(a) };
  }

  function cross(a, b) {
    return { x: a.y * b.z - a.z * b.y, y: a.z * b.x - a.x * b.z, z: a.x * b.y - a.y * b.x };
  }

  function normalise(v) {
    const l = Math.hypot(v.x, v.y, v.z) || 1;
    return { x: v.x / l, y: v.y / l, z: v.z / l };
  }

  // Spring layout in 3D. The pinned node (the searched topic) stays at the origin, so the other
  // nodes arrange themselves around it. Nodes start on a golden-spiral shell; forces then settle them.
  // The landing network (no pin) gets wider spacing: its 31 topics have long titles that need room for
  // their labels. The topic view keeps the tighter values it was checked with.
  function layout(ids, edges, pinId) {
    const n = ids.length;
    const landing = !pinId;
    const R = 46 + 13 * Math.sqrt(n);
    const pos = new Map();
    const vel = new Map();
    const golden = Math.PI * (3 - Math.sqrt(5));
    ids.forEach((id, i) => {
      const y = 1 - (2 * (i + 0.5)) / n;
      const r = Math.sqrt(Math.max(0, 1 - y * y));
      const jitter = rng(hash(id))() * 0.3;
      pos.set(id, { x: R * r * Math.cos(golden * i + jitter), y: R * y, z: R * r * Math.sin(golden * i + jitter) });
      vel.set(id, { x: 0, y: 0, z: 0 });
    });
    if (pinId && pos.has(pinId)) {
      pos.set(pinId, { x: 0, y: 0, z: 0 });
    }
    const repulsion = R * R * (landing ? 0.6 : 0.15);
    const springLength = R * (landing ? 0.8 : 0.42);
    const spring = 0.05;
    const gravity = landing ? 0.008 : 0.012;
    const damping = 0.82;
    const step = 0.35;
    const iterations = 300;
    for (let it = 0; it < iterations; it++) {
      const force = new Map(ids.map((id) => [id, { x: 0, y: 0, z: 0 }]));
      for (let i = 0; i < n; i++) {
        const pi = pos.get(ids[i]);
        for (let j = i + 1; j < n; j++) {
          const pj = pos.get(ids[j]);
          const dx = pi.x - pj.x;
          const dy = pi.y - pj.y;
          const dz = pi.z - pj.z;
          const d2 = dx * dx + dy * dy + dz * dz + 1;
          const mag = repulsion / d2 / Math.sqrt(d2);
          const fi = force.get(ids[i]);
          const fj = force.get(ids[j]);
          fi.x += dx * mag; fi.y += dy * mag; fi.z += dz * mag;
          fj.x -= dx * mag; fj.y -= dy * mag; fj.z -= dz * mag;
        }
      }
      for (const e of edges) {
        const a = pos.get(e.source);
        const b = pos.get(e.target);
        if (!a || !b) continue;
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const dz = b.z - a.z;
        const d = Math.hypot(dx, dy, dz) || 1;
        const mag = (d - springLength) * spring / d;
        const fa = force.get(e.source);
        const fb = force.get(e.target);
        fa.x += dx * mag; fa.y += dy * mag; fa.z += dz * mag;
        fb.x -= dx * mag; fb.y -= dy * mag; fb.z -= dz * mag;
      }
      for (const id of ids) {
        if (id === pinId) continue;
        const p = pos.get(id);
        const f = force.get(id);
        const v = vel.get(id);
        f.x -= p.x * gravity; f.y -= p.y * gravity; f.z -= p.z * gravity;
        v.x = (v.x + f.x * step) * damping;
        v.y = (v.y + f.y * step) * damping;
        v.z = (v.z + f.z * step) * damping;
        p.x += v.x; p.y += v.y; p.z += v.z;
      }
    }
    return pos;
  }

  // A bowed control point for the axon-like curve between two somata; the bow side comes from the seed.
  function controlPoint(a, b, seedText) {
    const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2, z: (a.z + b.z) / 2 };
    const d = { x: b.x - a.x, y: b.y - a.y, z: b.z - a.z };
    const len = Math.hypot(d.x, d.y, d.z);
    const next = rng(hash(seedText));
    const side = normalise(cross(d, randomUnit(next)));
    const bow = len * (0.16 + 0.1 * next());
    return { x: mid.x + side.x * bow, y: mid.y + side.y * bow, z: mid.z + side.z * bow };
  }

  // Five short dendrite-like filaments per neuron: a direction, a length and a curl.
  function dendrites(seedText, radius) {
    const next = rng(hash(seedText + "#dendrite"));
    const out = [];
    for (let i = 0; i < 5; i++) {
      const dir = randomUnit(next);
      const len = radius * (2.0 + 1.4 * next());
      const curl = normalise(cross(dir, randomUnit(next)));
      out.push({ dir, len, curl, bend: 0.35 + 0.3 * next() });
    }
    return out;
  }

  function bezier(a, c, b, t) {
    const u = 1 - t;
    return {
      x: u * u * a.x + 2 * u * t * c.x + t * t * b.x,
      y: u * u * a.y + 2 * u * t * c.y + t * t * b.y,
      z: u * u * a.z + 2 * u * t * c.z + t * t * b.z,
    };
  }

  // cam: { tx, ty, tz, yaw, pitch, dist, shiftX }. The eye sits on +z at distance dist from the target.
  // Returns screen x, y, the scale (focal / depth) and the depth itself.
  function project(p, cam, width, height, focal) {
    const x = p.x - cam.tx;
    const y = p.y - cam.ty;
    const z = p.z - cam.tz;
    const cy = Math.cos(cam.yaw);
    const sy = Math.sin(cam.yaw);
    const x1 = cy * x + sy * z;
    const z1 = cy * z - sy * x;
    const cp = Math.cos(cam.pitch);
    const sp = Math.sin(cam.pitch);
    const y2 = cp * y - sp * z1;
    const z2 = sp * y + cp * z1;
    const vz = cam.dist - z2;
    const s = focal / Math.max(vz, 1);
    return { x: width * cam.shiftX + x1 * s, y: height * 0.5 - y2 * s, s, vz };
  }

  root.NetGeometry = { hash, rng, layout, controlPoint, dendrites, bezier, project };
})(typeof window !== "undefined" ? window : globalThis);
