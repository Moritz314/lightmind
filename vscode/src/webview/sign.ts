// Orbit-Zeichen: Kern mit drei geneigten Bahnen, nach Mockup A3 (Canvas 2D, ohne Bibliothek).
// Ruhig: rund 20 Bilder pro Sekunde, steht still bei prefers-reduced-motion und in verdecktem Tab.

const TAU = Math.PI * 2;
const NP = 56;
const SR = 22;
const ORB = [
  { tilt: 1.18, az: 0, w: 1.7, ph: 0 },
  { tilt: 0.98, az: 2.094, w: -1.25, ph: 2.1 },
  { tilt: 1.36, az: 4.189, w: 0.95, ph: 4.2 },
].map((o) => ({ ...o, ct: Math.cos(o.tilt), st: Math.sin(o.tilt), ca: Math.cos(o.az), sa: Math.sin(o.az), X: new Float32Array(NP + 1), Y: new Float32Array(NP + 1), Z: new Float32Array(NP + 1) }));

export type Sign = { setMode(mode: string | null): void; setBusy(on: boolean): void };

function triple(el: HTMLElement, name: string, fallback: string): string {
  const v = getComputedStyle(el).getPropertyValue(name).trim();
  return /^\d+\s*,\s*\d+\s*,\s*\d+$/.test(v) ? v : fallback;
}

export function startSign(canvas: HTMLCanvasElement): Sign {
  const c = canvas.getContext('2d');
  const reduceMq = matchMedia('(prefers-reduced-motion: reduce)');
  let mode: string | null = null;
  let busy = false;
  let t = 0;
  let timer = 0;
  if (!c) return { setMode() {}, setBusy() {} };
  const size = 32;
  const px = Math.round(size * Math.min(2, Math.ceil(window.devicePixelRatio || 1)));
  canvas.width = canvas.height = px;
  canvas.style.width = canvas.style.height = `${size}px`;
  c.setTransform(px / 64, 0, 0, px / 64, 0, 0);

  function draw(): void {
    const g = c as CanvasRenderingContext2D;
    const pal = [triple(canvas, '--o1', '53,224,255'), triple(canvas, '--o2', '245,99,181'), triple(canvas, '--o3', '157,120,255')];
    const core = triple(canvas, mode === 'auto' ? '--gr' : mode === 'plan' ? '--bl' : mode === 'bypass' ? '--am' : '--o1', '53,224,255');
    const spin = reduceMq.matches ? 0.9 : t * 0.3 + 0.9;
    const cs = Math.cos(spin);
    const sn = Math.sin(spin);
    let TX = 0, TY = 0, TZ = 0, TK = 1, LX = 0, LY = 0, LZ = 0;
    const loc = (o: (typeof ORB)[number], co: number, si: number): void => {
      const x = co * SR, y0 = si * SR, y = y0 * o.ct;
      LX = x * o.ca - y * o.sa; LY = x * o.sa + y * o.ca; LZ = y0 * o.st;
    };
    const tr = (): void => {
      const x1 = LX * cs + LZ * sn, z1 = -LX * sn + LZ * cs;
      TK = 120 / (120 - z1); TX = 32 + x1 * TK; TY = 32 + LY * TK; TZ = z1;
    };
    for (const o of ORB) for (let i = 0; i <= NP; i++) {
      loc(o, Math.cos((TAU * i) / NP), Math.sin((TAU * i) / NP)); tr();
      o.X[i] = TX; o.Y[i] = TY; o.Z[i] = TZ;
    }
    g.clearRect(0, 0, 64, 64);
    g.lineCap = 'round'; g.lineJoin = 'round';
    const lines = (front: boolean): void => ORB.forEach((o, k) => {
      g.beginPath();
      let pen = false;
      for (let i = 0; i < NP; i++) {
        if (o.Z[i] + o.Z[i + 1] >= 0 !== front) { pen = false; continue; }
        if (!pen) { g.moveTo(o.X[i], o.Y[i]); pen = true; }
        g.lineTo(o.X[i + 1], o.Y[i + 1]);
      }
      g.strokeStyle = `rgba(${pal[k]},${front ? 0.95 : 0.3})`;
      g.lineWidth = front ? 2.2 : 1.5;
      g.stroke();
    });
    const sats = (front: boolean): void => ORB.forEach((o, k) => {
      const a = o.ph + o.w * t;
      loc(o, Math.cos(a), Math.sin(a)); tr();
      if (TZ >= 0 !== front) return;
      g.fillStyle = `rgba(${pal[k]},0.25)`; g.beginPath(); g.arc(TX, TY, 6 * TK, 0, TAU); g.fill();
      g.fillStyle = `rgb(${pal[k]})`; g.beginPath(); g.arc(TX, TY, 3 * TK, 0, TAU); g.fill();
    });
    lines(false); sats(false);
    const pulse = 1 + 0.06 * Math.sin(t * 1.4);
    g.fillStyle = `rgba(${core},0.18)`; g.beginPath(); g.arc(32, 32, 11 * pulse, 0, TAU); g.fill();
    g.fillStyle = `rgba(${core},0.4)`; g.beginPath(); g.arc(32, 32, 7 * pulse, 0, TAU); g.fill();
    g.fillStyle = `rgb(${core})`; g.beginPath(); g.arc(32, 32, 4.2, 0, TAU); g.fill();
    g.fillStyle = 'rgba(255,255,255,0.85)'; g.beginPath(); g.arc(31, 31, 1.4, 0, TAU); g.fill();
    lines(true); sats(true);
  }

  function tick(): void {
    timer = 0;
    if (document.hidden || reduceMq.matches) return;
    t += busy ? 0.12 : 0.05;
    draw();
    timer = window.setTimeout(tick, 50);
  }
  function wake(): void {
    if (reduceMq.matches) { draw(); return; }
    if (!timer && !document.hidden) timer = window.setTimeout(tick, 50);
  }
  document.addEventListener('visibilitychange', wake);
  reduceMq.addEventListener('change', () => { if (timer) { clearTimeout(timer); timer = 0; } draw(); wake(); });
  draw();
  wake();
  return {
    setMode(m) { if (m !== mode) { mode = m; draw(); } },
    setBusy(on) { busy = on; },
  };
}
