"""The landing animation of Orbit: shimmer wordmark, starfield, pulsing server nodes joined by curved links
with traces, session dots with trails, handover packets with an arrival flash, the live ticker.

The drawing is fixed; only the data is real: the nodes are `servers/`, the dots
are the open chats of `runs/`, the packets carry one of those chats from node to node."""
from __future__ import annotations

import math
import random
import time
from collections import deque

from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.strip import Strip
from textual.widget import Widget

from .data import Snap, label_of, short_id, state_of
from .theme import AMBER, BG, CYAN, DIM, MAG, TXT, blend, clamp, grad, sty

Runs = list[tuple[str, str]]                      # one row of a picture: (text, style string) in order, "" = the plain style
_STYLES: dict[tuple[Style, str], Style] = {}


def runs_text(rows: list[Runs]) -> Text:
    """A picture as Rich Text, rows joined by newlines: what the widgets used to hand to Textual, and what tests read."""
    out = Text(no_wrap=True, overflow="crop")
    for y, runs in enumerate(rows):
        for text, st in runs:
            out.append(text, st or None)
        if y < len(rows) - 1:
            out.append("\n")
    return out


def _style(base: Style, s: str) -> Style:
    """The Rich style of a style string on top of the widget's own style, made once per pair."""
    key = (base, s)
    st = _STYLES.get(key)
    if st is None:
        if len(_STYLES) > 20000:
            _STYLES.clear()
        st = _STYLES[key] = base + Style.parse(s) if s else base
    return st


def strips_of(rows: list[Runs], base: Style, left: list[int] | None = None) -> list[Strip]:
    """Strips straight from the runs of a picture (the line API).  Textual's own way from Rich Text, through Content to
    Strip, costs about 45 microseconds per styled run: 36 ms for the map of the start screen, which at 20 frames a second
    was most of a core.  `left[y]` spaces of the plain style go in front of row y (centring)."""
    out = []
    for y, runs in enumerate(rows):
        segs = [Segment(text, _style(base, st)) for text, st in runs if text]
        if left and left[y]:
            segs.insert(0, Segment(" " * left[y], base))
        out.append(Strip(segs))
    return out


# ---------------------------------------------------------------- map view
def line_pts(x0, y0, x1, y1):
    pts, dx, dy = [], abs(x1 - x0), -abs(y1 - y0)
    sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
    err = dx + dy
    while True:
        pts.append((x0, y0))
        if (x0, y0) == (x1, y1) or len(pts) > 400:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy
    return pts


_curve_cache: dict = {}


def curve_pts(p0, p1, bend=0.2):
    """Quadratic curve between two cells (cell aspect ~1:2 respected), 8-connected."""
    key = (p0, p1, bend)
    r = _curve_cache.get(key)
    if r is not None:
        return r
    (x0, y0), (x1, y1) = p0, p1
    dx, dy = x1 - x0, y1 - y0
    cx, cy = (x0 + x1) / 2 + bend * (-2 * dy), (y0 + y1) / 2 + bend * (dx / 2)
    n = max(8, int(max(abs(dx), abs(dy) * 2)))
    smp = []
    for i in range(n + 1):
        u = i / n
        smp.append((round((1 - u) ** 2 * x0 + 2 * (1 - u) * u * cx + u * u * x1),
                    round((1 - u) ** 2 * y0 + 2 * (1 - u) * u * cy + u * u * y1)))
    pts = [smp[0]]
    for s in smp[1:]:
        if s != pts[-1]:
            pts.extend(line_pts(*pts[-1], *s)[1:])
    if len(_curve_cache) > 256:
        _curve_cache.clear()
    _curve_cache[key] = pts
    return pts


class MapView(Widget):
    def __init__(self, snap: Snap, **kw):
        super().__init__(**kw)
        self.t0 = time.monotonic()
        self.pulses: list = []
        self.evlog: deque = deque(maxlen=2)
        self.next_pulse = 1.5
        self.rng = random.Random(3)
        self.reveal = 1.0
        self._stars_key = None
        self._stars: list = []
        self.D, self.live, self.sel = snap, [], snap.hub
        self._strips, self._strips_size = None, None      # the last frame as strips (`render_line`)
        self._load(snap)
        n = len(snap.servers)
        self.evlog.append((f"lm ready  ·  {n} server{'s' * (n != 1)}  ·  {len(self.live)} open chat{'s' * (len(self.live) != 1)}", 0.6))

    @property
    def calm(self):
        return getattr(self.app, "calm", False)

    def _load(self, snap: Snap):
        self.D = snap
        self.live = [(s, st) for s in snap.sessions if (st := state_of(s)) != "done"][:8]
        if self.sel not in {s.name for s in snap.servers}:
            self.sel = snap.hub

    def say(self, text, ts=None):
        """A line for the ticker."""
        self.evlog.append((text, (0.0 if self.calm else time.monotonic() - self.t0) if ts is None else ts))

    def set_snap(self, snap: Snap):
        seen = {s.id for s in self.D.sessions}
        self._load(snap)
        for s in snap.sessions:
            if s.id not in seen and state_of(s) != "done":
                self.say(f"{time.strftime('%H:%M:%S')}  ● new chat  {label_of(s)}  ·  {s.model}  [{short_id(s.id)}]")

    def spawn_pulse(self, t):
        names = [s.name for s in self.D.servers]
        pool = [s for s, _ in self.live] or self.D.sessions
        if len(names) < 2 or not pool:
            return
        a = self.rng.choice(pool)
        src = a.server if a.server in names else self.D.hub
        dst = self.rng.choice([n for n in names if n != src] or names)
        self.pulses.append(dict(src=src, dst=dst, t0=t, dur=3.0))
        self.say(f"{time.strftime('%H:%M:%S')}  ⇄  {label_of(a)}  ·  {a.model}  [{short_id(a.id)}]", t)

    def stars(self, W, Hu):
        if self._stars_key != (W, Hu):
            r = random.Random(11)
            self._stars = [(r.random() * W, r.randrange(max(1, Hu)), r.randrange(2), r.random() * 6.28)
                           for _ in range(max(6, W * Hu // 45))]
            self._stars_key = (W, Hu)
        return self._stars

    def route(self, pos, hub, a, b):
        def leg(x):  # hub -> x
            return curve_pts(pos[hub], pos[x])
        if a == hub:
            return leg(b)
        if b == hub:
            return leg(a)[::-1]
        return leg(a)[::-1] + leg(b)[1:]

    def render(self):
        """The picture as Rich Text.  Textual paints through `render_line`, so this is for readers of the picture (tests)."""
        return runs_text(self.rows())

    def refresh(self, *regions, **kw):
        self._strips = None                           # a new frame is wanted: the strips of the old one are stale
        return super().refresh(*regions, **kw)

    def render_line(self, y: int) -> Strip:
        """The line API: the whole frame is made on the first call after a refresh, straight into strips (see `strips_of`)."""
        size = self.size
        if self._strips is None or self._strips_size != size:
            prof = getattr(self.app, "prof", None)
            t0 = time.perf_counter() if prof else 0.0
            self._strips = strips_of(self.rows(), self.visual_style.rich_style)
            self._strips_size = size
            self._dirty_regions.clear()
            if prof:
                prof.tick("render", t0, f"{size.width}x{size.height}")
        return self._strips[y] if 0 <= y < len(self._strips) else Strip.blank(size.width, self.visual_style.rich_style)

    def rows(self) -> list[Runs]:
        W, H = self.size.width, self.size.height
        if W < 24 or H < 8:
            return []
        D, calm = self.D, self.calm
        t = 8.0 if calm else time.monotonic() - self.t0
        rev = self.reveal
        if not calm and t > self.next_pulse:
            self.next_pulse = t + 5.5
            self.spawn_pulse(t)
        self.pulses = [p for p in self.pulses if t - p["t0"] < p["dur"] + 0.7]
        grid = [[(" ", "")] * W for _ in range(H)]
        Hu = H - 3

        def put(x, y, ch, st, over=True):
            if 0 <= x < W and 0 <= y < Hu and (over or grid[y][x][0] == " "):
                grid[y][x] = (ch, st)

        def text(x, y, s, st, over=False):
            for i, ch in enumerate(s):
                put(x + i, y, ch, st, over)

        hub = D.hub
        hx, hy = W // 2, Hu // 2
        ring = [s for s in D.servers if s.name != hub]
        rx, ry = max(8, int(W * 0.36)), max(2, (Hu - 3) // 2)
        pos = {hub: (hx, hy)}
        n = max(1, len(ring))
        for k, s in enumerate(ring):
            a = -math.pi / 2 + math.pi / n + 2 * math.pi * k / n
            pos[s.name] = (hx + int(rx * math.cos(a)), hy + int(ry * math.sin(a)))
        order = [hub] + [s.name for s in ring]
        al = {nm: clamp(rev * len(order) - i) for i, nm in enumerate(order)}
        # flashes: departure / arrival glow per node
        flash = {}
        for pl in self.pulses:
            f = (t - pl["t0"]) / pl["dur"]
            if f < 0.12:
                flash[pl["src"]] = max(flash.get(pl["src"], 0), 0.6 * (1 - f / 0.12))
            if f > 1:
                flash[pl["dst"]] = max(flash.get(pl["dst"], 0), 1 - (f - 1) * pl["dur"] / 0.7)
        # expanding sonar ring
        r = (t * 2.2) % 9
        for a in range(0, 360, 10):
            put(hx + int(r * 2 * math.cos(math.radians(a))), hy + int(r * math.sin(math.radians(a))), "·", sty(blend(CYAN, BG, 0.15 + r / 12), al[hub]))
        # links (curved spokes, faint ring)
        names = [s.name for s in ring]
        for i, nm in enumerate(names):
            nb = names[(i + 1) % len(names)]
            if nb != nm and len(names) > 2:
                a_ = min(al[nm], al[nb])
                if a_ > 0:
                    for j, (x, y) in enumerate(line_pts(*pos[nm], *pos[nb])):
                        if j % 3 == 0:
                            put(x, y, "·", sty("#232850", a_), False)
        for nm in names:
            a_ = min(al[nm], al[hub])
            if a_ <= 0:
                continue
            pts = curve_pts(pos[hub], pos[nm])
            L = len(pts)
            for j, (x, y) in enumerate(pts):
                sh = 0.0 if calm else max(0.0, math.sin((j / max(1, L)) * 6.0 - t * 1.6)) ** 6
                put(x, y, "·", sty(blend("#2f7a96", CYAN, 0.55 * sh), a_), False)
        # open chats with fading trails
        labels, taken = [], set()
        for i, (s, state) in enumerate(self.live):
            if s.server not in pos or al[s.server] < 1:
                continue
            running = state == "running"
            if s.server == hub:
                a = t * 0.8 + i * 2.1
                if running:
                    for k in range(5, 0, -1):
                        b = a - k * 0.16
                        put(hx + int(8 * math.cos(b)), hy + int(2.4 * math.sin(b)), "·", blend(AMBER, BG, 0.25 + 0.15 * (5 - k)), False)
                x, y = hx + int(8 * math.cos(a)), hy + int(2.4 * math.sin(a))
                put(x, y, *(("●", f"bold {AMBER}") if running else ("○", DIM)))
                taken.add((x, y))
                labels.append((0 if running else 1, x, y, label_of(s)))
                continue
            pts = curve_pts(pos[hub], pos[s.server])
            ph = (t * 0.28 + (sum(map(ord, s.id)) % 100) / 100) % 2 if running else 0.5
            p = ph if ph <= 1 else 2 - ph
            sgn = 1 if ph <= 1 else -1
            if running:
                for k in range(1, 7):
                    q = int(clamp(p - sgn * k * 0.03) * (len(pts) - 1))
                    put(*pts[q], "•" if k <= 2 else "·", blend(AMBER, BG, 0.2 + 0.13 * k), True)
            x, y = pts[int(p * (len(pts) - 1))]
            put(x, y, *(("●", f"bold {AMBER}") if running else ("○", DIM)))
            taken.add((x, y))
            labels.append((0 if running else 1, x, y, label_of(s)))
        # handover packets with arrival flash
        for pl in self.pulses:
            if pl["src"] not in pos or pl["dst"] not in pos:
                continue
            path = self.route(pos, hub, pl["src"], pl["dst"])
            f = (t - pl["t0"]) / pl["dur"]
            L = len(path)
            if f <= 1:
                e = f * f * (3 - 2 * f)
                for k in range(8, 0, -1):
                    q = e - k * 0.03
                    if 0 <= q <= 1:
                        put(*path[int(q * (L - 1))], "•" if k <= 3 else "·", blend(MAG, BG, 0.95 - 0.1 * k))
                hxp, hyp = path[int(e * (L - 1))]
                put(hxp - 1, hyp, "‹", blend(MAG, BG, 0.5))
                put(hxp + 1, hyp, "›", blend(MAG, BG, 0.5))
                put(hxp, hyp, "◆", "bold #ffffff")
            else:
                g = clamp((f - 1) * pl["dur"] / 0.7)
                dx_, dy_ = pos[pl["dst"]]
                rr = 1 + g * 4
                for a in range(0, 360, 45):
                    put(dx_ + round(rr * 2 * math.cos(math.radians(a))), dy_ + round(rr * math.sin(math.radians(a))),
                        "✦" if g < 0.5 else "·", blend("#ffffff", BG, 0.2 + 0.7 * g) if g < 0.5 else blend(MAG, BG, 0.3 + 0.6 * g))
        # nodes
        cnt, run = {}, {}
        for s, state in self.live:
            cnt[s.server] = cnt.get(s.server, 0) + 1
            if state == "running":
                run[s.server] = run.get(s.server, 0) + 1
        for ni, nm in enumerate(order):
            x, y = pos[nm]
            a_ = al[nm]
            if a_ <= 0:
                continue
            load = run.get(nm, 0)
            v = 0.5 + 0.5 * math.sin(t * (1.4 + 0.7 * load) + ni * 1.7) if not calm else 0.6
            fl = flash.get(nm, 0)
            base = AMBER if nm == hub else CYAN
            halo = clamp((0.25 + 0.5 * v) * (0.5 + 0.25 * load) + fl)
            hc = sty(blend(BG, "#ffffff" if fl > 0.3 else base, halo), a_)
            put(x - 1, y, "(", hc)
            put(x + 1, y, ")", hc)
            gc = sty(blend(BG, base, clamp(0.15 + 0.35 * v + fl * 0.4)), a_)
            put(x - 2, y, "·", gc, False)
            put(x + 2, y, "·", gc, False)
            put(x, y, "◈" if nm == hub else "◉", sty(blend(base, "#ffffff", clamp(0.1 + 0.4 * v + fl)), a_, True))
            lab = nm + (f" ×{cnt[nm]}" if cnt.get(nm) else "")
            if nm == self.sel:
                lab = f"▸ {lab} ◂"
                st = f"bold {BG} on {CYAN}"
            else:
                st = sty(TXT, a_)
            text(x - len(lab) // 2, y + 1, lab, st, True)
            for k in range(-2, 3):
                taken.add((x + k, y))
            for k in range(len(lab)):
                taken.add((x - len(lab) // 2 + k, y + 1))
        # session labels last: never overwritten by dots, 1-cell gap, no label overlaps another
        for _pri, x, y, name in sorted(labels, key=lambda l: l[0]):
            for dy in (0, -1, 1):
                cells = {(x + 1 + k, y + dy) for k in range(len(name) + 2)}
                if x + 2 + len(name) < W and 0 <= y + dy < Hu and not (cells & taken):
                    text(x + 2, y + dy, name, "#8a90b8", True)
                    put(x + 1, y + dy, " ", "")
                    taken |= cells
                    break
        # starfield: two depth layers, only into empty cells
        if not calm:
            for sx, sy, layer, ph in self.stars(W, Hu):
                x = int((sx + t * (0.5 if layer == 0 else 1.3)) % W)
                if grid[sy][x][0] == " " and (x, sy) not in taken and (x - 1, sy) not in taken and (x + 1, sy) not in taken:
                    v = 0.5 + 0.5 * math.sin(t * (0.7 + 0.6 * layer) + ph)
                    c = blend(BG, "#8c96d0" if layer else "#5a6288", (0.12 + 0.3 * v) * (1 if layer else 0.75))
                    grid[sy][x] = ("∙" if layer and v > 0.8 else "·", sty(c, rev))
        # slim divider + live ticker
        if H >= 12:
            for x in range(2, W - 2):
                u = (x - 2) / max(1, W - 5)
                grid[Hu][x] = ("─", sty(blend(BG, "#2a3060", math.sin(math.pi * u) ** 0.8), rev))
            for i, (ln, ts) in enumerate(self.evlog):
                last = i == len(self.evlog) - 1
                fa = clamp((t - ts) / 0.9)
                col = blend(MAG, TXT, 0.4) if last else DIM
                s = (("◆ " if last else "  ") + ln)[: W - 4]
                y = H - 2 + i
                for k, ch in enumerate(s):
                    grid[y][2 + k] = (ch, sty(MAG if (last and k == 0) else col, fa * (1 if last else 0.8)))
        rows: list[Runs] = []
        for row in grid:
            runs: Runs = []
            run_, rs = "", None
            for ch, st in row:
                if st != rs:
                    if run_:
                        runs.append((run_, rs))
                    run_, rs = "", st
                run_ += ch
            if run_:
                runs.append((run_, rs))
            rows.append(runs)
        return rows


FONT = {
    "L": ["█    ", "█    ", "█    ", "█    ", "█████"], "I": ["█████", "  █  ", "  █  ", "  █  ", "█████"],
    "G": [" ████", "█    ", "█  ██", "█   █", " ████"], "H": ["█   █", "█   █", "█████", "█   █", "█   █"],
    "T": ["█████", "  █  ", "  █  ", "  █  ", "  █  "], "M": ["█   █", "██ ██", "█ █ █", "█   █", "█   █"],
    "N": ["█   █", "██  █", "█ █ █", "█  ██", "█   █"], "D": ["████ ", "█   █", "█   █", "█   █", "████ "],
}


def _merge(runs: Runs) -> Runs:
    out: Runs = []
    for text, st in runs:
        if out and out[-1][1] == st:
            out[-1] = (out[-1][0] + text, st)
        else:
            out.append((text, st))
    return out


def wordmark_rows(t: float, reveal: float = 1.0, calm: bool = False) -> list[Runs]:
    """The five rows of the wordmark as runs: the colour drifts slowly along the letters, a light sweeps across them every
    6.5 s, and `reveal` (0 to 1) fades them in."""
    word = "LIGHTMIND"
    total = len(word) * 6 - 1
    sp = (t % 6.5) / 6.5 * (total + 44) - 22
    vshade = (0.10, 0.04, 0.0, -0.06, -0.14)
    drift = 0.0 if calm else t * 0.05
    rows: list[Runs] = []
    for r in range(5):
        runs: Runs = []
        col = 0
        for ch in word:
            for px in FONT[ch][r]:
                if px == " ":
                    runs.append((" ", ""))
                else:
                    x = col / total
                    base = grad(x * 0.9 - drift)
                    if not calm:
                        inten = max(0.0, 1 - abs(col + r * 3 - sp) / 10) ** 2
                        base = blend(base, "#ffffff", 0.85 * inten)
                    vs = vshade[r]
                    base = blend(base, "#ffffff", vs) if vs > 0 else blend(base, BG, -vs)
                    a = clamp((reveal * 1.5 - x - r * 0.025) * 5)
                    if a < 1:
                        base = blend(base, "#ffffff", 0.7 * (1 - a))
                    runs.append((px, blend(BG, base, a)) if a > 0 else (" ", ""))
                col += 1
            runs.append((" ", ""))
            col += 1
        rows.append(_merge(runs))
    return rows


def subline_runs(t: float, reveal: float = 1.0, calm: bool = False) -> Runs:
    s = "agent harness · lm · orbit"
    sp = (t % 6.5) / 6.5 * (len(s) + 30) - 9
    runs: Runs = []
    for i, ch in enumerate(s):
        c = DIM
        if not calm:
            c = blend(DIM, CYAN, 0.8 * max(0.0, 1 - abs(i - sp) / 6) ** 2)
        runs.append((ch, blend(BG, c, clamp(reveal * 2 - 0.9))))
    return _merge(runs)


def _centered(rows: list[Runs]) -> Text:
    out = Text(justify="center")
    for y, runs in enumerate(rows):
        for text, st in runs:
            out.append(text, st or None)
        if y < len(rows) - 1:
            out.append("\n")
    return out


def wordmark(t: float, reveal: float = 1.0, calm: bool = False) -> Text:
    return _centered(wordmark_rows(t, reveal, calm))


def subline(t: float, reveal: float = 1.0, calm: bool = False) -> Text:
    return _centered([subline_runs(t, reveal, calm)])


class Banner(Widget):
    """The wordmark and the line under it, seven rows (one of air, five of letters, one of text), centred.  Painted through
    the line API like the map: as two Statics with Rich Text they cost a fifth of every frame."""

    def __init__(self, calm: bool = False, **kw):
        super().__init__(**kw)
        self.calm = calm
        self.t, self.reveal = time.monotonic(), 1.0
        self._strips, self._strips_size = None, None

    def show(self, t: float, reveal: float) -> None:
        self.t, self.reveal = t, reveal
        self.refresh()

    def refresh(self, *regions, **kw):
        self._strips = None
        return super().refresh(*regions, **kw)

    def rows(self) -> list[Runs]:
        return [[]] + wordmark_rows(self.t, self.reveal, self.calm) + [subline_runs(self.t, self.reveal, self.calm)]

    def render(self):
        return runs_text(self.rows())

    def render_line(self, y: int) -> Strip:
        size = self.size
        if self._strips is None or self._strips_size != size:
            prof = getattr(self.app, "prof", None)
            t0 = time.perf_counter() if prof else 0.0
            rows = self.rows()
            left = [max(0, (size.width - sum(len(text) for text, _ in runs)) // 2) for runs in rows]
            self._strips = strips_of(rows, self.visual_style.rich_style, left)
            self._strips_size = size
            self._dirty_regions.clear()
            if prof:
                prof.tick("banner", t0, f"{size.width}x{size.height}")
        return self._strips[y] if 0 <= y < len(self._strips) else Strip.blank(size.width, self.visual_style.rich_style)
