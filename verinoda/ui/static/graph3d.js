/* Verinoda: the graph in three dimensions. A force-directed layout in 3D (Barnes-Hut octree), drawn
   with perspective on a 2D canvas: no WebGL, no libraries, nothing from the network. The camera
   orbits a target and flies between views with eased transitions; it can follow one note or frame a
   region. The page (app.js) gives it the nodes, links, colours and what to do on a click. */
"use strict";
(() => {
  // -- octree for the many-body force ----------------------------------------------------------
  function octChild(q, n) {
    const h = q.s / 2, i = (n.x >= q.x0 + h ? 1 : 0) + (n.y >= q.y0 + h ? 2 : 0) + (n.z >= q.z0 + h ? 4 : 0);
    return q.kids[i] || (q.kids[i] = { x0: q.x0 + (i & 1) * h, y0: q.y0 + ((i >> 1) & 1) * h, z0: q.z0 + (i >> 2) * h,
      s: h, m: 0, cx: 0, cy: 0, cz: 0, n: null, kids: null, more: null });
  }
  function octInsert(q, n, depth) {
    q.cx = (q.cx * q.m + n.x) / (q.m + 1); q.cy = (q.cy * q.m + n.y) / (q.m + 1); q.cz = (q.cz * q.m + n.z) / (q.m + 1);
    q.m++;
    if (q.kids === null) {
      if (q.m === 1) { q.n = n; return; }
      if (depth > 24) { (q.more || (q.more = [])).push(n); return; } // (nearly) the same point
      const old = q.n; q.n = null; q.kids = [null, null, null, null, null, null, null, null];
      if (old) octInsert(octChild(q, old), old, depth + 1);
    }
    octInsert(octChild(q, n), n, depth + 1);
  }
  function buildOct(ns) {
    let x0 = Infinity, y0 = Infinity, z0 = Infinity, x1 = -Infinity, y1 = -Infinity, z1 = -Infinity;
    for (const n of ns) {
      if (n.x < x0) x0 = n.x; if (n.y < y0) y0 = n.y; if (n.z < z0) z0 = n.z;
      if (n.x > x1) x1 = n.x; if (n.y > y1) y1 = n.y; if (n.z > z1) z1 = n.z;
    }
    const root = { x0, y0, z0, s: Math.max(x1 - x0, y1 - y0, z1 - z0) + 1, m: 0, cx: 0, cy: 0, cz: 0, n: null, kids: null, more: null };
    for (const n of ns) octInsert(root, n, 0);
    return root;
  }
  function charge(n, q, strength, theta2) {
    if (q.m === 0) return;
    let dx = q.cx - n.x, dy = q.cy - n.y, dz = q.cz - n.z, d2 = dx * dx + dy * dy + dz * dz;
    if (q.kids === null || (q.s * q.s) / theta2 < d2) {
      let m = q.m;
      if (q.kids === null && (q.n === n || (q.more && q.more.includes(n)))) m -= 1; // not itself
      if (m <= 0) return;
      if (d2 < 1e-4) { // the same point: a small push that is never zero
        dx = ((n.index * 7919) % 13 - 6.5) * 0.01; dy = ((n.index * 104729) % 11 - 5.5) * 0.01;
        dz = ((n.index * 1299709) % 7 - 3.5) * 0.01; d2 = dx * dx + dy * dy + dz * dz;
      }
      if (d2 < 30) d2 = Math.sqrt(30 * d2); // soften very close pairs
      const k = strength * m / d2;
      n.vx += dx * k; n.vy += dy * k; n.vz += dz * k;
      return;
    }
    for (const c of q.kids) if (c) charge(n, c, strength, theta2);
  }

  const TAU = Math.PI * 2, NEAR = 12, R3 = 1.6; // R3: dots a little larger than in 2D, depth makes them smaller
  const ease = (u) => (u < 0.5 ? 4 * u * u * u : 1 - Math.pow(-2 * u + 2, 3) / 2);
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const CAM_KEYS = ["yaw", "pitch", "dist", "tx", "ty", "tz"];

  class Graph3D {
    constructor(canvas, opts) {
      this.c = canvas; this.ctx = canvas.getContext("2d");
      this.o = Object.assign({ charge: -150, distance: 44, gravity: 0.035, labels: true,
        color: () => "#8a8f98", linkColor: () => "#777", theme: () => ({}),
        onSelect: null, onOpen: null, onHover: null }, opts);
      this.nodes = []; this.links = []; this.byId = new Map(); this.adj = new Map();
      this.cam = { yaw: 0.55, pitch: 0.32, dist: 800, tx: 0, ty: 0, tz: 0 };
      this.anim = null; this.spin = true; this.follow = null; this.region = null; this.path = null;
      this.autoFrame = true; // until the user moves the camera it keeps the whole graph in view while it settles
      this.hover = null; this.selected = null; this.hidden = new Set(); this.marks = null; this.filter = "";
      this.alpha = 0; this.active = false; this.running = false; this.dirty = true; this.down = null;
      this.w = 10; this.h = 10; this.dpr = 1; this.frames = 0; this.last = 0;
      this.bind();
      new ResizeObserver(() => this.resize()).observe(canvas.parentElement);
    }

    // -- data and layout --------------------------------------------------------------------------
    setData(nodes, links, opts) {
      opts = opts || {};
      const old = this.byId, init = opts.init || null;
      this.nodes = nodes.map((n, i) => {
        const o = old.get(n.id), from = !o && init ? init.get(n.id) : null;
        let x, y, z;
        if (o && isFinite(o.x)) ({ x, y, z } = o);
        else if (from && isFinite(from.x)) { // the flat graph: it inflates into depth
          x = from.x; y = -from.y; z = ((i * 7919) % 101 - 50) * 0.6;
        } else { // a sphere, filled from the middle out
          const r = 14 * Math.cbrt(i + 1), t = Math.acos(1 - 2 * ((i + 0.5) / Math.max(1, nodes.length))), p = i * 2.39996;
          x = r * Math.sin(t) * Math.cos(p); y = r * Math.cos(t); z = r * Math.sin(t) * Math.sin(p);
        }
        return Object.assign({}, n, { index: i, x, y, z, vx: 0, vy: 0, vz: 0 });
      });
      this.byId = new Map(this.nodes.map((n) => [n.id, n]));
      this.links = links.map((l) => Object.assign({}, l, { s: this.byId.get(l.source), t: this.byId.get(l.target) }))
        .filter((l) => l.s && l.t && l.s !== l.t);
      this.adj = new Map(this.nodes.map((n) => [n.id, new Set()]));
      const deg = new Map();
      for (const l of this.links) {
        this.adj.get(l.s.id).add(l.t.id); this.adj.get(l.t.id).add(l.s.id);
        deg.set(l.s, (deg.get(l.s) || 0) + 1); deg.set(l.t, (deg.get(l.t) || 0) + 1);
      }
      for (const n of this.nodes) {
        n.deg = deg.get(n) || 0;
        const size = n.size !== undefined ? n.size : n.deg;
        n.r = 3.2 + Math.min(14, Math.sqrt(Math.max(size, n.deg)) * 1.5);
      }
      const unlinked = this.nodes.filter((n) => !n.deg).length;
      this.orphans = unlinked > 0 && unlinked < this.nodes.length;
      for (const k of ["hover", "selected", "follow"]) if (this[k] && !this.byId.has(this[k].id)) this[k] = null;
      if (this.selected) this.selected = this.byId.get(this.selected.id);
      if (this.follow) this.follow = this.byId.get(this.follow.id);
      this.alpha = old.size ? 0.4 : 1;
      this.dirty = true; this.kick();
    }
    visible(n) { return !this.hidden.has(String(n.group)); }
    tick() {
      const ns = this.nodes, a = this.alpha, o = this.o;
      if (!ns.length) { this.alpha = 0; return; }
      const q = buildOct(ns);
      for (const n of ns) charge(n, q, o.charge * a, 0.81);
      for (const l of this.links) {
        const s = l.s, t = l.t;
        let dx = t.x + t.vx - s.x - s.vx, dy = t.y + t.vy - s.y - s.vy, dz = t.z + t.vz - s.z - s.vz;
        const d = Math.sqrt(dx * dx + dy * dy + dz * dz) || 1e-6;
        const k = ((d - o.distance) / d) * a / Math.max(1, Math.min(s.deg, t.deg));
        dx *= k; dy *= k; dz *= k;
        const bias = s.deg / (s.deg + t.deg || 1);
        t.vx -= dx * bias; t.vy -= dy * bias; t.vz -= dz * bias;
        s.vx += dx * (1 - bias); s.vy += dy * (1 - bias); s.vz += dz * (1 - bias);
      }
      // unlinked notes make a shell round the linked ones instead of piling up among them
      let shell = 0;
      if (this.orphans) {
        const d2 = [];
        for (const n of ns) if (n.deg) d2.push(n.x * n.x + n.y * n.y + n.z * n.z);
        d2.sort((x, y) => x - y);
        shell = Math.sqrt(d2[Math.floor(d2.length * 0.8)] || 0) * 1.1 + 40;
      }
      for (const n of ns) {
        if (!n.deg && shell) {
          const d = Math.sqrt(n.x * n.x + n.y * n.y + n.z * n.z) || 1e-6, k = ((d - shell) / d) * 0.5 * a;
          n.vx -= n.x * k; n.vy -= n.y * k; n.vz -= n.z * k;
        } else {
          const gr = (n.deg ? o.gravity : o.gravity * 6) * a;
          n.vx -= n.x * gr; n.vy -= n.y * gr; n.vz -= n.z * gr;
        }
        n.vx *= 0.6; n.vy *= 0.6; n.vz *= 0.6;
        n.x += n.vx; n.y += n.vy; n.z += n.vz;
        if (!isFinite(n.x) || !isFinite(n.y) || !isFinite(n.z)) {
          const r = 14 * Math.cbrt(n.index + 1), p = n.index * 2.39996;
          n.x = r * Math.cos(p); n.y = 0; n.z = r * Math.sin(p); n.vx = n.vy = n.vz = 0;
        }
      }
      this.alpha -= this.alpha * 0.025;
    }

    // -- the camera ------------------------------------------------------------------------------
    focal() { return Math.min(this.w, this.h) * 1.15; }
    bounds(ns) { // centre and a radius that frames most of them (a few far ones may fall outside)
      let cx = 0, cy = 0, cz = 0;
      for (const n of ns) { cx += n.x; cy += n.y; cz += n.z; }
      cx /= ns.length; cy /= ns.length; cz /= ns.length;
      const d = ns.map((n) => Math.sqrt((n.x - cx) ** 2 + (n.y - cy) ** 2 + (n.z - cz) ** 2) + n.r).sort((a, b) => a - b);
      return { x: cx, y: cy, z: cz, r: Math.max(110, d[Math.min(d.length - 1, Math.floor(d.length * 0.92))] * 1.05) }; // never so close that the rest looms
    }
    distFor(r) { return r + (this.focal() * r) / (Math.min(this.w, this.h) * 0.42); }
    flyTo(to, ms = 850) {
      const from = {}; for (const k of CAM_KEYS) from[k] = this.cam[k];
      const keys = CAM_KEYS.filter((k) => to[k] !== undefined), goal = Object.assign({}, from, to);
      let dy = (goal.yaw - from.yaw) % TAU; if (dy > Math.PI) dy -= TAU; if (dy < -Math.PI) dy += TAU;
      goal.yaw = from.yaw + dy;
      goal.pitch = clamp(goal.pitch, -1.45, 1.45);
      if (ms <= 0) { Object.assign(this.cam, goal); this.anim = null; }
      else this.anim = { from, to: goal, keys, t0: performance.now(), ms };
      this.dirty = true; this.kick();
    }
    core(ns) { const linked = ns.filter((n) => n.deg); return linked.length >= 3 ? linked : ns; }
    fit(ms = 800, ids = null) {
      const ns = (ids ? ids.map((id) => this.byId.get(id)).filter(Boolean) : this.nodes).filter((n) => this.visible(n));
      if (!ns.length) return;
      const b = this.bounds(this.core(ns)); // unlinked members sit on the outer shell: frame the linked ones
      this.autoFrame = !ids;
      this.flyTo({ tx: b.x, ty: b.y, tz: b.z, dist: this.distFor(b.r) }, ms);
    }
    flyToNode(id, ms = 900) {
      const n = this.byId.get(id);
      if (!n) return false;
      this.autoFrame = false;
      const near = [...(this.adj.get(id) || [])].map((x) => this.byId.get(x)).filter((m) => m && this.visible(m));
      const d = near.map((m) => Math.sqrt((m.x - n.x) ** 2 + (m.y - n.y) ** 2 + (m.z - n.z) ** 2)).sort((a, b) => a - b);
      const r = clamp(d.length ? d[Math.floor(d.length * 0.8)] : 60, 45, 420);
      this.flyTo({ tx: n.x, ty: n.y, tz: n.z, dist: this.distFor(r) }, ms);
      return true;
    }
    orbit(dyaw, dpitch) { this.anim = null; this.autoFrame = false; this.cam.yaw += dyaw; this.cam.pitch = clamp(this.cam.pitch + dpitch, -1.45, 1.45); this.dirty = true; this.kick(); }
    zoom(f) { this.anim = null; this.autoFrame = false; this.cam.dist = clamp(this.cam.dist * f, 30, 60000); this.dirty = true; this.kick(); }
    pan(px, py) { // move what is seen by px, py screen pixels
      const c = this.cam, s = this.focal() / c.dist, cy = Math.cos(c.yaw), sy = Math.sin(c.yaw), cp = Math.cos(c.pitch), sp = Math.sin(c.pitch);
      c.tx -= (cy * px) / s - (-sy * sp * py) / s; c.ty += (cp * py) / s; c.tz -= (-sy * px) / s - (-cy * sp * py) / s;
      this.anim = null; this.follow = null; this.autoFrame = false; this.dirty = true; this.kick();
    }
    project() {
      const c = this.cam, cy = Math.cos(c.yaw), sy = Math.sin(c.yaw), cp = Math.cos(c.pitch), sp = Math.sin(c.pitch);
      const f = this.focal(), w2 = this.w / 2, h2 = this.h / 2;
      for (const n of this.nodes) {
        const dx = n.x - c.tx, dy = n.y - c.ty, dz = n.z - c.tz;
        const x1 = dx * cy - dz * sy, z1 = dx * sy + dz * cy;
        const y2 = dy * cp - z1 * sp, z2 = dy * sp + z1 * cp;
        const depth = c.dist - z2;
        n.depth = depth;
        if (depth < NEAR) { n.sv = false; continue; }
        const s = f / depth;
        n.sx = w2 + x1 * s; n.sy = h2 - y2 * s; n.ss = s; n.sv = true;
      }
    }
    screenOf(id) { const n = this.byId.get(id); this.project(); return n && n.sv ? { x: n.sx, y: n.sy } : null; }

    // -- the loop --------------------------------------------------------------------------------
    setActive(on) { this.active = on; if (on) { this.resize(); this.kick(); } }
    resize() {
      const r = this.c.parentElement.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
      if (r.width < 2 || r.height < 2) return;
      this.w = r.width; this.h = r.height; this.dpr = dpr;
      this.c.width = Math.round(r.width * dpr); this.c.height = Math.round(r.height * dpr);
      this.dirty = true; this.kick();
    }
    kick() {
      if (this.running || !this.active) return;
      this.running = true; this.last = performance.now();
      requestAnimationFrame((t) => this.frame(t));
    }
    moving() { return this.alpha > 0.004 || !!this.anim || this.spin || !!this.follow || !!this.down; }
    frame(now) {
      if (!this.active) { this.running = false; return; }
      const dt = Math.min(50, now - this.last); this.last = now;
      if (this.alpha > 0.004) {
        const t0 = performance.now();
        let k = 0;
        do { this.tick(); k++; } while (this.alpha > 0.004 && k < 8 && performance.now() - t0 < 9);
      }
      if (this.anim) {
        const a = this.anim, u = Math.min(1, (now - a.t0) / a.ms), e = ease(u);
        for (const key of a.keys) this.cam[key] = a.from[key] + (a.to[key] - a.from[key]) * e;
        if (u >= 1) this.anim = null;
      } else if (this.follow) { // keep the followed note in the middle while the layout settles
        const n = this.follow, c = this.cam;
        c.tx += (n.x - c.tx) * 0.12; c.ty += (n.y - c.ty) * 0.12; c.tz += (n.z - c.tz) * 0.12;
      }
      if (this.autoFrame && this.alpha > 0.004 && this.frames % 12 === 0 && this.nodes.length
          && !(this.anim && this.anim.keys.some((k) => k === "dist" || k[0] === "t"))) {
        const vis = this.core(this.nodes.filter((n) => this.visible(n)));
        if (vis.length) {
          const b = this.bounds(vis), c = this.cam, d = this.distFor(b.r);
          c.tx += (b.x - c.tx) * 0.35; c.ty += (b.y - c.ty) * 0.35; c.tz += (b.z - c.tz) * 0.35; c.dist += (d - c.dist) * 0.35;
        }
      }
      if (this.spin && !this.down && !this.anim) this.cam.yaw += dt * 0.00009;
      this.draw(now); this.frames++;
      if (this.moving() || this.dirty) { this.dirty = false; requestAnimationFrame((t) => this.frame(t)); }
      else this.running = false;
    }

    // -- drawing ---------------------------------------------------------------------------------
    rad(n) { return clamp(n.r * n.ss * R3, 1.4, 34); } // near dots grow, but never fill the screen
    matches(n) { return this.filter && (n.title || "").toLowerCase().includes(this.filter); }
    focusNode() { return this.hover || this.selected; }
    lit(n) {
      const f = this.hover;
      if (f) return n === f || this.adj.get(f.id).has(n.id);
      if (this.region) return this.region.has(n.id);
      if (this.selected) return n === this.selected || this.adj.get(this.selected.id).has(n.id);
      if (this.filter) return this.matches(n);
      if (this.marks && this.marks.size) return this.marks.has(n.id);
      return true;
    }
    linkLit(s, t) { // the same order as lit(): what the pointer is on, a region, the selection, a filter, marks
      if (this.hover) return s === this.hover || t === this.hover;
      if (this.region) return this.region.has(s.id) && this.region.has(t.id);
      if (this.selected) return s === this.selected || t === this.selected;
      if (this.filter) return this.matches(s) || this.matches(t);
      if (this.marks && this.marks.size) return this.marks.has(s.id) && this.marks.has(t.id);
      return true;
    }
    dimming() { return !!(this.hover || this.region || this.selected || this.filter || (this.marks && this.marks.size)); }
    draw(now) {
      const ctx = this.ctx, th = this.o.theme(), dim = this.dimming();
      ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
      ctx.clearRect(0, 0, this.w, this.h);
      this.project();
      const dist = this.cam.dist, fog = (d) => clamp(1.3 - (d / dist) * 0.55, 0.18, 1);
      // links, batched by colour and strength: thousands of lines in a few strokes
      const batches = new Map();
      for (const l of this.links) {
        const s = l.s, t = l.t;
        if (!s.sv || !t.sv || !this.visible(s) || !this.visible(t)) continue;
        const on = dim && this.linkLit(s, t);
        const a = (dim ? (on ? 0.85 : 0.05) : 0.32) * fog((s.depth + t.depth) / 2);
        if (a < 0.015) continue;
        const key = `${this.o.linkColor(l)}|${Math.round(a * 8)}|${on ? 1 : 0}`;
        let b = batches.get(key);
        if (!b) batches.set(key, b = []);
        b.push(l);
      }
      ctx.lineCap = "round";
      for (const [key, ls] of batches) {
        const [color, step, on] = key.split("|");
        ctx.globalAlpha = Number(step) / 8; ctx.strokeStyle = color; ctx.lineWidth = on === "1" ? 1.7 : 0.8;
        ctx.beginPath();
        for (const l of ls) { ctx.moveTo(l.s.sx, l.s.sy); ctx.lineTo(l.t.sx, l.t.sy); }
        ctx.stroke();
      }
      // a path found between two notes, on top
      if (this.path && this.path.length > 1) {
        const ps = this.path.map((id) => this.byId.get(id)).filter((n) => n && n.sv);
        ctx.globalAlpha = 0.95; ctx.strokeStyle = th.accent || "#8e7cf5"; ctx.lineWidth = 3;
        ctx.setLineDash([8, 6]); ctx.lineDashOffset = -now / 30;
        ctx.beginPath(); ps.forEach((n, i) => (i ? ctx.lineTo(n.sx, n.sy) : ctx.moveTo(n.sx, n.sy))); ctx.stroke();
        ctx.setLineDash([]);
      }
      // nodes, far to near
      const order = this.nodes.filter((n) => n.sv && this.visible(n)).sort((a, b) => b.depth - a.depth);
      for (const n of order) {
        const on = !dim || this.lit(n), r = on ? this.rad(n) : Math.min(this.rad(n), 7); // context stays small
        const a = (on ? 1 : 0.17) * fog(n.depth); // what is not lit stays as context
        ctx.globalAlpha = a; ctx.fillStyle = this.o.color(n);
        ctx.beginPath(); ctx.arc(n.sx, n.sy, r, 0, TAU); ctx.fill();
        if (r > 3.2) { // a highlight: the dot reads as a ball
          ctx.globalAlpha = a * 0.3; ctx.fillStyle = "#ffffff";
          ctx.beginPath(); ctx.arc(n.sx - r * 0.32, n.sy - r * 0.32, r * 0.42, 0, TAU); ctx.fill();
        }
        const mark = this.marks && this.marks.get(n.id);
        if (mark) {
          ctx.globalAlpha = Math.max(a, 0.6); ctx.lineWidth = 2.2; ctx.strokeStyle = mark === "changed" ? (th.warn || "#e0a34a") : "#d9d05a";
          ctx.beginPath(); ctx.arc(n.sx, n.sy, r + 2.5, 0, TAU); ctx.stroke();
        }
        if (this.filter && this.matches(n)) {
          ctx.globalAlpha = 1; ctx.lineWidth = 2; ctx.strokeStyle = th.accent || "#8e7cf5";
          ctx.beginPath(); ctx.arc(n.sx, n.sy, r + 2, 0, TAU); ctx.stroke();
        }
      }
      // the selected note: a turning ring, and a finer one while the camera follows it
      const sel = this.selected;
      if (sel && sel.sv && this.visible(sel)) {
        const r = this.rad(sel);
        ctx.globalAlpha = 1; ctx.strokeStyle = th.accent || "#8e7cf5"; ctx.lineWidth = 2;
        ctx.setLineDash([5, 4]); ctx.lineDashOffset = -now / 45;
        ctx.beginPath(); ctx.arc(sel.sx, sel.sy, r + 7, 0, TAU); ctx.stroke();
        ctx.setLineDash([]);
        if (this.follow === sel) {
          ctx.globalAlpha = 0.6; ctx.lineWidth = 1;
          ctx.beginPath(); ctx.arc(sel.sx, sel.sy, r + 13 + Math.sin(now / 400) * 2, 0, TAU); ctx.stroke();
        }
      }
      this.drawLabels(order, dim, th);
      ctx.globalAlpha = 1;
    }
    drawLabels(order, dim, th) {
      const ctx = this.ctx, want = new Set(), f = this.hover || this.selected;
      if (f) {
        want.add(f);
        const nb = [...this.adj.get(f.id)].map((id) => this.byId.get(id)).filter((n) => n && n.sv).sort((a, b) => b.deg - a.deg);
        for (const n of nb.slice(0, 14)) want.add(n);
      }
      if (this.region && !this.hover) {
        const rs = order.filter((n) => this.region.has(n.id)).sort((a, b) => b.deg - a.deg);
        for (const n of rs.slice(0, 14)) want.add(n);
      }
      if (this.path) for (const id of this.path) { const n = this.byId.get(id); if (n) want.add(n); }
      if (this.filter) for (const n of order) if (this.matches(n) && want.size < 40) want.add(n);
      if (this.marks) for (const n of order) if (this.marks.get(n.id) === "changed" && want.size < 50) want.add(n);
      if (this.o.labels && !dim) { // the hubs, for bearings, and whatever is big on screen, near ones first
        for (const n of [...order].sort((a, b) => b.deg - a.deg).slice(0, 10)) want.add(n);
        const big = order.filter((n) => n.r * n.ss * R3 > 8).sort((a, b) => a.depth - b.depth).slice(0, 30);
        for (const n of big) want.add(n);
      }
      ctx.textAlign = "center"; ctx.textBaseline = "top"; ctx.lineJoin = "round";
      for (const n of order) {
        if (!want.has(n) || !n.sv) continue;
        const r = this.rad(n), size = clamp(10 + n.ss * 3, 10, 15);
        ctx.font = `${n === f ? "600 " : ""}${size}px system-ui, -apple-system, "Segoe UI", sans-serif`;
        const label = n.title.length > 42 ? n.title.slice(0, 40) + "…" : n.title;
        ctx.globalAlpha = 0.9; ctx.strokeStyle = th.bg || "#111"; ctx.lineWidth = 3;
        ctx.strokeText(label, n.sx, n.sy + r + 3);
        ctx.globalAlpha = 1; ctx.fillStyle = n === f ? (th.accent || "#8e7cf5") : (th.text || "#ddd");
        ctx.fillText(label, n.sx, n.sy + r + 3);
      }
    }

    // -- hit testing and input -------------------------------------------------------------------
    hit(sx, sy) {
      let best = null;
      for (const n of this.nodes) {
        if (!n.sv || !this.visible(n)) continue;
        const r = Math.max(6, this.rad(n) + 3), dx = n.sx - sx, dy = n.sy - sy;
        if (dx * dx + dy * dy <= r * r && (best === null || n.depth < best.depth)) best = n;
      }
      return best;
    }
    bind() {
      const c = this.c, pos = (ev) => { const r = c.getBoundingClientRect(); return [ev.clientX - r.left, ev.clientY - r.top]; };
      c.style.touchAction = "none";
      c.addEventListener("wheel", (ev) => {
        ev.preventDefault(); this.spin = false;
        this.zoom(Math.exp(ev.deltaY * 0.0012));
      }, { passive: false });
      c.addEventListener("pointerdown", (ev) => {
        const [x, y] = pos(ev);
        this.down = { x, y, button: ev.button, pan: ev.button === 2 || ev.shiftKey, moved: false, id: ev.pointerId };
        try { c.setPointerCapture(ev.pointerId); } catch (_) { /* not every browser */ }
        this.spin = false; this.autoFrame = false; this.kick();
      });
      c.addEventListener("pointermove", (ev) => {
        const [x, y] = pos(ev);
        if (this.down) {
          const dx = x - this.down.x, dy = y - this.down.y;
          if (Math.abs(dx) + Math.abs(dy) > 3) this.down.moved = true;
          if (this.down.moved) {
            if (this.down.pan) this.pan(dx, dy); else this.orbit(-dx * 0.0055, dy * 0.0055);
            this.down.x = x; this.down.y = y;
          }
          return;
        }
        const n = this.hit(x, y);
        if (n !== this.hover) { this.hover = n; this.dirty = true; this.kick(); }
        c.style.cursor = n ? "pointer" : "grab";
        if (this.o.onHover) this.o.onHover(n, ev);
      });
      const up = (ev) => {
        const d = this.down;
        this.down = null;
        if (!d) return;
        if (!d.moved && d.button === 0 && this.o.onSelect) { const [x, y] = pos(ev); this.o.onSelect(this.hit(x, y)); }
        this.dirty = true; this.kick();
      };
      c.addEventListener("pointerup", up);
      c.addEventListener("pointercancel", () => { this.down = null; });
      c.addEventListener("contextmenu", (ev) => ev.preventDefault());
      c.addEventListener("pointerleave", () => {
        if (this.hover && !this.down) { this.hover = null; this.dirty = true; this.kick(); }
        if (this.o.onHover) this.o.onHover(null, null);
      });
      c.addEventListener("dblclick", (ev) => { const [x, y] = pos(ev), n = this.hit(x, y); if (n && this.o.onOpen) this.o.onOpen(n); });
    }
  }
  window.VerinodaGraph3D = Graph3D;
})();
