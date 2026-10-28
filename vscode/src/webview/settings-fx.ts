// Hintergrund und Orbit-Zeichen der Einstellungen, 1:1 aus dem Mockup N1 (settings-n1-liste.html) uebernommen:
// ein Punktraster, das Maus und Finger ausweicht und aufleuchtet, und das grosse Zeichen im Kopf, das beim Scrollen
// verkleinert in die Titelleiste wandert. Ruhig bei prefers-reduced-motion, pausiert bei verdecktem Tab oder
// geschlossener Ansicht.

const TAU = Math.PI * 2;
const clamp = (v: number, lo: number, hi: number): number => Math.min(hi, Math.max(lo, v));

export type FxHost = {
  view: HTMLElement; // Wurzel der Ansicht (traegt die Farbwerte)
  grid: HTMLCanvasElement;
  sign: HTMLCanvasElement;
  home: HTMLElement; // Startseite (Seite mit Kopf)
  sc: HTMLElement; // scrollender Bereich der Startseite
  slot0: HTMLElement; // Platz des grossen Zeichens
  slot1: HTMLElement; // Platz des kleinen Zeichens in der Titelleiste
  homeVisible: () => boolean;
};
export type Fx = { start(): void; stop(): void; resize(): void; place(): void };

const GAP = 24, RG = 170, RP = 120, PUSH = 14, KS = 0.16, DAMP = 0.78, DM = 26;
const NP = 56, SR = 22;
const COS: number[] = [];
const SIN: number[] = [];
for (let i = 0; i <= NP; i++) {
  COS.push(Math.cos((TAU * i) / NP));
  SIN.push(Math.sin((TAU * i) / NP));
}
type Orbit = { tilt: number; az: number; w: number; ph: number; ct: number; st: number; ca: number; sa: number; X: Float32Array; Y: Float32Array; Z: Float32Array };
const mkOrbit = (tilt: number, az: number, w: number, ph: number): Orbit => ({
  tilt, az, w, ph, ct: Math.cos(tilt), st: Math.sin(tilt), ca: Math.cos(az), sa: Math.sin(az),
  X: new Float32Array(NP + 1), Y: new Float32Array(NP + 1), Z: new Float32Array(NP + 1),
});

type Lut = { f: string; r: number; hf: string };
type Pal = { rest: string; lut: Lut[]; o: string[]; core: string };

export function createFx(h: FxHost): Fx {
  const reduceMq = matchMedia('(prefers-reduced-motion: reduce)');
  let reduce = reduceMq.matches;
  const gx = h.grid.getContext('2d') as CanvasRenderingContext2D;
  const sgx = h.sign.getContext('2d') as CanvasRenderingContext2D;
  const mouse = { x: 0, y: 0, on: false, k: 1 };
  let pal: Pal | null = null;
  let running = false;

  function readPal(): void {
    const cs = getComputedStyle(h.view);
    const g = (n: string): string => cs.getPropertyValue(n).trim();
    const num = (n: string): number[] => g(n).split(',').map(Number);
    const dot = num('--dot');
    const hot = num('--hot');
    const a = parseFloat(g('--dota'));
    const lut: Lut[] = [];
    for (let q = 0; q <= 15; q++) {
      const t = q / 15;
      const rgb = dot.map((v, i) => Math.round(v + (hot[i] - v) * t));
      lut.push({ f: `rgba(${rgb},${(a + (0.95 - a) * t).toFixed(3)})`, r: 0.7 + 1.5 * t, hf: `rgba(${hot},${(0.2 * t).toFixed(3)})` });
    }
    pal = { rest: `rgba(${dot},${a})`, lut, o: [g('--o1'), g('--o2'), g('--o3')], core: g('--core') };
  }

  // ---- Punktraster ----
  const G = { s: 1, W: 0, H: 0, cols: 0, rows: 0, ox: 0, oy: 0, awake: false };
  const D = { all: true, x0: 0, y0: 0, x1: 0, y1: 0 };
  let dx = new Float32Array(0), dy = new Float32Array(0), vx = new Float32Array(0), vy = new Float32Array(0), gl = new Float32Array(0);
  let aw = new Uint8Array(0);

  function gridInit(): void {
    const W = h.view.clientWidth, H = h.view.clientHeight;
    if (!W || !H) return;
    const s = Math.min(window.devicePixelRatio || 1, 2);
    h.grid.width = Math.round(W * s);
    h.grid.height = Math.round(H * s);
    h.grid.style.width = `${W}px`;
    h.grid.style.height = `${H}px`;
    G.s = s; G.W = W; G.H = H;
    G.cols = Math.max(2, Math.floor((W - 12) / GAP) + 1);
    G.rows = Math.max(2, Math.floor((H - 12) / GAP) + 1);
    G.ox = Math.round((W - (G.cols - 1) * GAP) / 2);
    G.oy = Math.round((H - (G.rows - 1) * GAP) / 2);
    const n = G.cols * G.rows;
    dx = new Float32Array(n); dy = new Float32Array(n); vx = new Float32Array(n); vy = new Float32Array(n); gl = new Float32Array(n);
    aw = new Uint8Array(n);
    G.awake = false;
    D.all = true;
    drawGrid();
  }

  function stepGrid(dt: number): void {
    const cols = G.cols, rows = G.rows, ox = G.ox, oy = G.oy;
    const f60 = dt * 60, damp = Math.pow(DAMP, f60), k = KS * f60;
    const rise = 1 - Math.pow(0.65, f60), fall = 1 - Math.pow(0.95, f60);
    const mx = mouse.x, my = mouse.y, on = mouse.on;
    mouse.k = 1 + (mouse.k - 1) * Math.pow(0.9, f60);
    if (mouse.k < 1.01) mouse.k = 1;
    const PU = PUSH * mouse.k;
    let busy = 0, bx0 = Infinity, by0 = Infinity, bx1 = -Infinity, by1 = -Infinity;
    for (let r = 0; r < rows; r++) {
      const y = oy + r * GAP, rn = on && y > my - RG && y < my + RG;
      for (let c = 0; c < cols; c++) {
        const i = r * cols + c, x = ox + c * GAP;
        let tx = 0, ty = 0, tg = 0;
        if (rn && x > mx - RG && x < mx + RG) {
          const ex = x - mx, ey = y - my, d2 = ex * ex + ey * ey;
          if (d2 < RG * RG) {
            const d = Math.sqrt(d2), q = 1 - d / RG;
            tg = q * q;
            if (d < RP && d > 0.5) { const f = 1 - d / RP, p = (PU * f * f) / d; tx = ex * p; ty = ey * p; }
          }
        } else if (!aw[i]) continue;
        if (x < bx0) bx0 = x;
        if (x > bx1) bx1 = x;
        if (y < by0) by0 = y;
        if (y > by1) by1 = y;
        vx[i] = (vx[i] + (tx - dx[i]) * k) * damp;
        vy[i] = (vy[i] + (ty - dy[i]) * k) * damp;
        dx[i] += vx[i] * f60; dy[i] += vy[i] * f60;
        gl[i] += (tg - gl[i]) * (tg > gl[i] ? rise : fall);
        const mv = Math.abs(vx[i]) + Math.abs(vy[i]);
        if (tg === 0 && mv + Math.abs(dx[i]) + Math.abs(dy[i]) < 0.04 && gl[i] < 0.012) {
          dx[i] = dy[i] = vx[i] = vy[i] = gl[i] = 0; aw[i] = 0;
        } else {
          aw[i] = 1;
          if (mv > 0.02 || Math.abs(tx - dx[i]) + Math.abs(ty - dy[i]) > 0.05 || Math.abs(tg - gl[i]) > 0.008) busy++;
        }
      }
    }
    D.x0 = bx0; D.y0 = by0; D.x1 = bx1; D.y1 = by1;
    if (!busy) G.awake = false;
  }

  /** Zeichnet nur das Rechteck, in dem sich seit dem letzten Bild etwas geaendert hat. */
  function drawGrid(): void {
    if (!pal || !G.cols) return;
    const cols = G.cols, rows = G.rows, ox = G.ox, oy = G.oy, s = G.s, c = gx;
    let x0 = 0, y0 = 0, x1 = G.W, y1 = G.H;
    if (!D.all) {
      if (D.x1 < D.x0) return;
      x0 = Math.max(0, D.x0 - DM); y0 = Math.max(0, D.y0 - DM);
      x1 = Math.min(G.W, D.x1 + DM); y1 = Math.min(G.H, D.y1 + DM);
    }
    D.all = false;
    const px = Math.floor(x0 * s), py = Math.floor(y0 * s), pw = Math.ceil(x1 * s) - px, ph = Math.ceil(y1 * s) - py;
    c.save();
    c.beginPath(); c.rect(px, py, pw, ph); c.clip();
    c.clearRect(px, py, pw, ph);
    const r0 = Math.max(0, Math.floor((y0 - oy) / GAP)), r1 = Math.min(rows - 1, Math.ceil((y1 - oy) / GAP));
    const c0 = Math.max(0, Math.floor((x0 - ox) / GAP)), c1 = Math.min(cols - 1, Math.ceil((x1 - ox) / GAP));
    const sz = Math.max(1, Math.round(1.2 * s)), hs = sz / 2;
    c.fillStyle = pal.rest;
    c.beginPath();
    for (let r = r0; r <= r1; r++) {
      const Y = Math.round((oy + r * GAP) * s - hs);
      for (let q = c0; q <= c1; q++) if (!aw[r * cols + q]) c.rect(Math.round((ox + q * GAP) * s - hs), Y, sz, sz);
    }
    c.fill();
    for (let r = r0; r <= r1; r++) {
      for (let q = c0; q <= c1; q++) {
        const i = r * cols + q;
        if (!aw[i]) continue;
        const lv = clamp(Math.round(gl[i] * 15), 0, 15), e = pal.lut[lv];
        const X = (ox + q * GAP + dx[i]) * s, Y = (oy + r * GAP + dy[i]) * s;
        if (lv >= 6) { c.fillStyle = e.hf; c.beginPath(); c.arc(X, Y, e.r * 3.4 * s, 0, TAU); c.fill(); }
        c.fillStyle = e.f; c.beginPath(); c.arc(X, Y, e.r * s, 0, TAU); c.fill();
      }
    }
    c.restore();
  }

  // ---- Orbit-Zeichen: Kern mit drei geneigten Bahnen ----
  const ORB = [mkOrbit(1.18, 0, 1.7, 0), mkOrbit(0.98, 2.094, -1.25, 2.1), mkOrbit(1.36, 4.189, 0.95, 4.2)];
  const SG = { S0: 104, S1: 32, x0: 0, y0: 0, x1: 0, y1: 0, cx: 0, cy: 0, e: 0, t: 0, rx: 0, ry: 0, ax: 0, ay: 0 };
  let placeRaf = 0;

  function measureSign(): void {
    const pr = h.home.getBoundingClientRect();
    const a = h.slot0.getBoundingClientRect(), b = h.slot1.getBoundingClientRect();
    SG.S0 = a.width || 104; SG.S1 = b.width || 32;
    SG.x0 = a.left - pr.left + a.width / 2;
    SG.y0 = a.top - pr.top + h.sc.scrollTop + a.height / 2;
    SG.x1 = b.left - pr.left + b.width / 2;
    SG.y1 = b.top - pr.top + b.height / 2;
  }
  function signInit(): void {
    measureSign();
    const s = Math.min(2, Math.ceil(window.devicePixelRatio || 1)), px = Math.round(SG.S0 * s);
    h.sign.width = h.sign.height = px;
    h.sign.style.width = h.sign.style.height = `${SG.S0}px`;
    sgx.setTransform(px / 64, 0, 0, px / 64, 0, 0);
    const r = h.view.getBoundingClientRect();
    SG.ax = r.left; SG.ay = r.top;
  }
  /** Das Zeichen sitzt gross im Kopf und wandert beim Scrollen verkleinert in die Titelleiste. */
  function place(): void {
    placeRaf = 0;
    const st = h.sc.scrollTop, p = clamp(st / 92, 0, 1);
    const e = reduce ? (st > 40 ? 1 : 0) : p * p * (3 - 2 * p);
    const S = SG.S0 + (SG.S1 - SG.S0) * e;
    SG.e = e; SG.cx = SG.x0 + (SG.x1 - SG.x0) * e; SG.cy = SG.y0 + (SG.y1 - SG.y0) * e;
    h.sign.style.transform = `translate3d(${(SG.cx - SG.S0 / 2).toFixed(1)}px,${(SG.cy - SG.S0 / 2).toFixed(1)}px,0) scale(${(S / SG.S0).toFixed(4)})`;
    const m = h.home.style;
    m.setProperty('--plate', clamp((e - 0.55) / 0.45, 0, 1).toFixed(3));
    m.setProperty('--bm', clamp((e - 0.7) / 0.3, 0, 1).toFixed(3));
    m.setProperty('--ht', clamp(1 - st / 46, 0, 1).toFixed(3));
    if (reduce) drawSign(0);
  }

  function drawSign(dt: number): void {
    if (!pal) return;
    const P = pal;
    const c = sgx, bst = 1 + 0.9 * SG.e, rb = 1 + 0.4 * SG.e;
    if (!reduce) {
      SG.t += dt;
      let ax = 0, ay = 0;
      if (mouse.on) {
        const mx = mouse.x - SG.cx, my = mouse.y - SG.cy, d = Math.hypot(mx, my) || 1, a = Math.min(1, d / 480) * 0.55;
        ax = (mx / d) * a; ay = (my / d) * a;
      }
      const k = 1 - Math.exp(-dt * 4);
      SG.ry += (ax - SG.ry) * k; SG.rx += (-ay - SG.rx) * k;
    }
    const spin = reduce ? 0.9 : SG.t * 0.3 + 0.9;
    const cs = Math.cos(spin), sn = Math.sin(spin), cy = Math.cos(SG.ry), sy = Math.sin(SG.ry), cx = Math.cos(SG.rx), sxn = Math.sin(SG.rx);
    let TX = 0, TY = 0, TZ = 0, TK = 1, LX = 0, LY = 0, LZ = 0;
    const loc = (o: Orbit, co: number, si: number): void => {
      const x = co * SR, y0 = si * SR, y = y0 * o.ct;
      LX = x * o.ca - y * o.sa; LY = x * o.sa + y * o.ca; LZ = y0 * o.st;
    };
    const tr = (): void => {
      const x1 = LX * cs + LZ * sn, z1 = -LX * sn + LZ * cs;
      const y2 = LY * cx - z1 * sxn, z2 = LY * sxn + z1 * cx;
      const x3 = x1 * cy + z2 * sy, z3 = -x1 * sy + z2 * cy;
      TK = 120 / (120 - z3); TX = 32 + x3 * TK; TY = 32 + y2 * TK; TZ = z3;
    };
    for (const o of ORB) for (let i = 0; i <= NP; i++) { loc(o, COS[i], SIN[i]); tr(); o.X[i] = TX; o.Y[i] = TY; o.Z[i] = TZ; }
    c.clearRect(0, 0, 64, 64);
    c.lineCap = 'round'; c.lineJoin = 'round';
    const lines = (front: boolean): void => ORB.forEach((o, k) => {
      c.beginPath();
      let pen = false;
      for (let i = 0; i < NP; i++) {
        if (o.Z[i] + o.Z[i + 1] >= 0 !== front) { pen = false; continue; }
        if (!pen) { c.moveTo(o.X[i], o.Y[i]); pen = true; }
        c.lineTo(o.X[i + 1], o.Y[i + 1]);
      }
      c.strokeStyle = `rgba(${P.o[k]},${front ? 0.95 : 0.3})`;
      c.lineWidth = (front ? 1.3 : 0.9) * bst;
      c.stroke();
    });
    const sats = (front: boolean): void => ORB.forEach((o, k) => {
      const a = o.ph + o.w * SG.t, dir = o.w > 0 ? 1 : -1;
      loc(o, Math.cos(a), Math.sin(a)); tr();
      if (TZ >= 0 !== front) return;
      const hx = TX, hy = TY, hk = TK;
      let px = hx, py = hy;
      for (let j = 1; j <= 6; j++) {
        const b = a - dir * j * 0.1;
        loc(o, Math.cos(b), Math.sin(b)); tr();
        c.strokeStyle = `rgba(${P.o[k]},${(0.55 * (1 - j / 7)).toFixed(2)})`;
        c.lineWidth = 1.3 * bst;
        c.beginPath(); c.moveTo(px, py); c.lineTo(TX, TY); c.stroke();
        px = TX; py = TY;
      }
      c.fillStyle = `rgba(${P.o[k]},0.22)`; c.beginPath(); c.arc(hx, hy, 4.6 * hk * rb, 0, TAU); c.fill();
      c.fillStyle = `rgb(${P.o[k]})`; c.beginPath(); c.arc(hx, hy, 2.1 * hk * rb, 0, TAU); c.fill();
    });
    lines(false); sats(false);
    const pulse = 1 + 0.06 * Math.sin(SG.t * 1.4);
    c.fillStyle = `rgba(${P.core},0.16)`; c.beginPath(); c.arc(32, 32, 9 * pulse * rb, 0, TAU); c.fill();
    c.fillStyle = `rgba(${P.core},0.34)`; c.beginPath(); c.arc(32, 32, 5.6 * pulse * rb, 0, TAU); c.fill();
    c.fillStyle = `rgb(${P.core})`; c.beginPath(); c.arc(32, 32, 3.2 * rb, 0, TAU); c.fill();
    c.fillStyle = 'rgba(255,255,255,0.85)'; c.beginPath(); c.arc(31, 31, 1.1 * rb, 0, TAU); c.fill();
    lines(true); sats(true);
  }

  // ---- Schleife: im Ruhezustand nur das Zeichen mit rund 30 Bildern, mit Maus das Raster in jedem Bild ----
  let raf = 0, timer = 0, tPrev = 0;
  function loop(t: number): void {
    raf = 0;
    const dt = clamp((t - tPrev) / 1000, 0, 0.1);
    tPrev = t;
    const live = h.homeVisible();
    if (live) drawSign(dt);
    if (G.awake) { stepGrid(Math.min(0.05, dt)); drawGrid(); raf = requestAnimationFrame(loop); }
    else if (live && running) timer = window.setTimeout(() => { timer = 0; raf = requestAnimationFrame(loop); }, 34);
  }
  function kickLoop(): void {
    if (!running || !pal || raf || timer || reduce || document.hidden) return;
    tPrev = performance.now();
    raf = requestAnimationFrame(loop);
  }
  function halt(): void {
    if (raf) cancelAnimationFrame(raf);
    if (timer) clearTimeout(timer);
    raf = timer = 0;
  }
  function still(): void {
    dx.fill(0); dy.fill(0); vx.fill(0); vy.fill(0); gl.fill(0); aw.fill(0);
    G.awake = false; D.all = true;
    drawGrid(); drawSign(0);
  }
  function wake(): void {
    if (reduce || !running) return;
    G.awake = true;
    if (timer) { clearTimeout(timer); timer = 0; }
    kickLoop();
  }

  document.addEventListener('visibilitychange', () => (document.hidden ? halt() : kickLoop()));
  reduceMq.addEventListener('change', (e) => {
    reduce = e.matches;
    if (reduce) { halt(); still(); place(); } else kickLoop();
  });
  new MutationObserver(() => {
    if (!running) return;
    readPal(); D.all = true; drawGrid(); drawSign(0);
  }).observe(document.body, { attributes: true, attributeFilter: ['class'] });

  // Maus, Stift und Finger: die Punkte weichen aus, ein Tipp loest einen Stoss aus
  let liftT = 0;
  const rel = (cx: number, cy: number): void => {
    const r = h.view.getBoundingClientRect();
    mouse.x = cx - r.left; mouse.y = cy - r.top; mouse.on = true;
    clearTimeout(liftT);
    wake();
  };
  const unaim = (): void => { mouse.on = false; wake(); };
  const lift = (): void => { clearTimeout(liftT); liftT = window.setTimeout(unaim, 450); };
  const kick = (): void => { mouse.k = 2.3; wake(); };
  h.view.addEventListener('pointermove', (e) => { if (!reduce && e.pointerType !== 'touch') rel(e.clientX, e.clientY); }, { passive: true });
  h.view.addEventListener('pointerdown', (e) => { if (!reduce && e.pointerType !== 'touch') kick(); }, { passive: true });
  h.view.addEventListener('touchstart', (e) => { if (reduce) return; const t = e.touches[0]; rel(t.clientX, t.clientY); kick(); }, { passive: true });
  h.view.addEventListener('touchmove', (e) => { if (reduce) return; const t = e.touches[0]; rel(t.clientX, t.clientY); }, { passive: true });
  h.view.addEventListener('touchend', lift, { passive: true });
  h.view.addEventListener('touchcancel', lift, { passive: true });
  h.view.addEventListener('mouseleave', unaim);
  window.addEventListener('blur', unaim);
  h.sc.addEventListener('scroll', () => { if (!placeRaf) placeRaf = requestAnimationFrame(place); }, { passive: true });

  function layout(): void {
    gridInit();
    signInit();
    place();
    if (reduce) drawSign(0);
  }
  let rzTimer = 0;
  if (window.ResizeObserver) {
    new ResizeObserver(() => {
      if (!running) return;
      clearTimeout(rzTimer);
      rzTimer = window.setTimeout(layout, 120);
    }).observe(h.view);
  }

  return {
    start() {
      running = true;
      readPal();
      layout();
      drawSign(0);
      kickLoop();
    },
    stop() {
      running = false;
      halt();
      mouse.on = false;
    },
    resize: layout,
    place,
  };
}
