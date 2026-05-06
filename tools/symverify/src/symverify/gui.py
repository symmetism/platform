"""SymVerify GUI — single-window CustomTkinter app.

Reads `~/.symmetism/state/status.json` (written by the daemon) every
5 s, renders fold + brackets + recent events + last narrative.
Buttons trigger a manual audit, narrative explanation, the verify
page, the daemon log, and a small Settings popup.

Three custom widgets render the visual state:
  - TrinityRings: 3 concentric circles + 3 colored dots whose
    positions encode aligned/drift/alarm — same iconography as the
    public verify page (apps/attestation-service/static/verify.js).
  - PlatonicCycle: pixel-art animation that cycles through the five
    Platonic solids (tetra → cube → octa → dodeca → icosa), each
    rendered as a slowly-rotating wireframe. Purely decorative.
  - The narrative panel (now full-height) ships with a one-click
    copy button.

Why this design:
  - The daemon is the source of truth; the GUI only renders what's
    on disk. No duplicate audit pipeline. Refresh = re-read JSON.
  - All buttons that talk to the network or run audits do so on
    background threads, so the Tk event loop never blocks.
  - CustomTkinter (5.x) for modern look without leaving stdlib tk.
  - Single file so PyInstaller has one entry point to bundle.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import customtkinter as ctk

from symverify import __version__, config


# ---------------------------------------------------------------------------
# Color palette — match the rich CLI rendering for visual continuity.
# ---------------------------------------------------------------------------

COLOR_STABLE = "#7CD3A0"       # green
COLOR_DRIFT = "#E0B341"        # amber
COLOR_ALARM = "#E06D6D"        # red
COLOR_MUTED = "#5A6470"        # gray
COLOR_BG = "#0F1116"           # near-black
COLOR_PANEL = "#161B22"        # GitHub-style panel
COLOR_TEXT = "#C9D1D9"         # off-white


def _status_color(status: str) -> str:
    """Map a bracket / overall status string to a hex color."""
    if status == "conserved":
        return COLOR_STABLE
    if status in ("drift_expected", "drift"):
        return COLOR_DRIFT
    if status in ("drift_alarm", "lockdown"):
        return COLOR_ALARM
    return COLOR_MUTED


def _status_glyph(status: str) -> str:
    if status == "conserved":
        return "✓"
    if status == "drift_expected":
        return "⚠"
    if status in ("drift_alarm", "lockdown", "drift"):
        return "✗"
    return "·"


# ---------------------------------------------------------------------------
# Status loading
# ---------------------------------------------------------------------------


def status_path() -> Path:
    return config.state_dir() / "status.json"


def load_status() -> dict[str, Any] | None:
    """Read status.json. Returns None if missing or malformed."""
    p = status_path()
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def load_recent_events(limit: int = 12) -> list[dict[str, Any]]:
    """Pull the most recent events from the daemon's SQLite store."""
    from symverify import db

    try:
        with db.connect() as conn:
            return db.list_events(conn, limit=limit)
    except Exception:
        return []


def load_recent_narratives(limit: int = 5) -> list[dict[str, Any]]:
    from symverify import db

    try:
        with db.connect() as conn:
            return db.list_narratives(conn, limit=limit)
    except Exception:
        return []


def coherence_state(status: dict[str, Any] | None) -> str:
    """Map a status payload to the same 3-state coherence label the
    public verify page uses: 'aligned' | 'drift' | 'alarm'.

    Logic mirrors verify.js:
      - alarm field true → 'alarm'
      - any drift (status == 'drift' or 'lockdown') → 'drift' / 'alarm'
      - everything conserved → 'aligned'
    """
    if status is None:
        return "drift"  # daemon not running; render conservatively
    if status.get("alarm"):
        return "alarm"
    overall = status.get("status", "drift")
    if overall == "lockdown":
        return "alarm"
    if overall == "drift":
        return "drift"
    if overall == "clean":
        return "aligned"
    return "drift"


def humanize_age(iso_ts: str) -> str:
    """'2026-05-06T18:35:56Z' -> '12s ago' / '3m ago' / '2h ago'."""
    try:
        ts = datetime.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return iso_ts
    delta = datetime.now(timezone.utc) - ts
    s = int(delta.total_seconds())
    if s < 0:
        return "just now"
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


# ---------------------------------------------------------------------------
# Trinity rings — same visual language as the public verify page.
# ---------------------------------------------------------------------------


class TrinityRings(tk.Canvas):
    """Three concentric circles with three colored dots whose positions
    encode coherence state.

    Mirrors verify.js placeRingPoints() exactly so the in-app and
    in-browser indicators tell the same story:
      aligned: all 3 dots stacked at center (blue)
      drift:   2 dots offset L/R, 1 at center (amber)
      alarm:   3 dots in a triangle spread (red)
    """

    DOT_FILL = {
        "aligned": "#7eb6d9",
        "drift": "#e0a458",
        "alarm": "#cc4444",
    }
    DOT_POSITIONS = {
        "aligned": [(0, 0), (0, 0), (0, 0)],
        "drift": [(-12, 0), (12, 0), (0, 0)],
        "alarm": [(-50, 30), (50, 30), (0, -55)],
    }
    SIZE = 160  # canvas dims; rings scaled to fit

    def __init__(self, master, **kw):
        super().__init__(
            master,
            width=self.SIZE,
            height=self.SIZE,
            bg=COLOR_PANEL,
            highlightthickness=0,
            **kw,
        )
        self._state: str = "aligned"
        self._draw()

    def set_state(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        cx = cy = self.SIZE / 2
        # 3 background rings (radii proportional: 100/75/50 in 220-unit space)
        scale = self.SIZE / 220
        for r, opacity in ((100, 0.30), (75, 0.45), (50, 0.60)):
            stipple = "gray50" if opacity < 0.5 else ""
            self.create_oval(
                cx - r * scale, cy - r * scale,
                cx + r * scale, cy + r * scale,
                outline=COLOR_TEXT, width=1,
                stipple=stipple,
            )

        # 3 colored dots at state-specific positions.
        fill = self.DOT_FILL[self._state]
        for dx, dy in self.DOT_POSITIONS[self._state]:
            x = cx + dx * scale
            y = cy + dy * scale
            r = 7
            # Subtle outer glow ring.
            self.create_oval(
                x - r - 2, y - r - 2, x + r + 2, y + r + 2,
                fill="", outline=fill, width=1, stipple="gray25",
            )
            self.create_oval(
                x - r, y - r, x + r, y + r,
                fill=fill, outline="",
            )


# ---------------------------------------------------------------------------
# PlatonicCycle — pixel-art animation that cycles through the five
# Platonic solids: tetrahedron → cube → octahedron → dodecahedron →
# icosahedron, then back to tetrahedron. Each shape rotates slowly
# while held, then snap-transitions to the next.
# ---------------------------------------------------------------------------


class PlatonicCycle(tk.Canvas):
    """A small canvas that draws one of the five Platonic solids at a
    time, slowly rotating, and cycles to the next every few seconds.

    Each solid is rendered as a wireframe-pixel-art silhouette: we
    project a 3D edge list to 2D with a rotating Y-axis and rasterize
    every edge with Bresenham-style line steps onto a pixel grid,
    then paint each grid cell as a chunky `PIXEL_SCALE`-pixel rect.
    The result is a recognizable wireframe of each solid that pulses
    and rotates without leaving stdlib tk.

    No state — purely decorative. The audit pipeline drives nothing
    here, and nothing here drives the audit pipeline.
    """

    PIXEL_SCALE = 3
    GRID = 22                 # pixel grid is GRID × GRID per shape
    CANVAS_W = GRID * PIXEL_SCALE
    CANVAS_H = GRID * PIXEL_SCALE
    FRAME_MS = 60             # ~16 fps

    HOLD_FRAMES = 60          # ~3.6 s per shape before snapping to next
    EDGE_COLOR = "#7eb6d9"    # match aligned-state blue

    PHI = (1 + 5 ** 0.5) / 2  # golden ratio, used in icos/dodec verts

    # ------ vertex / edge tables -------------------------------------------
    # Vertices live on the unit sphere-ish; we scale to fit the GRID.
    # Edges are indices into the vertex list. Sources: standard CG
    # references; double-checked the edge counts to match the Platonic
    # numbers: tet=6, cube=12, oct=12, dodec=30, icos=30.

    @staticmethod
    def _tetrahedron():
        v = [
            (1, 1, 1), (1, -1, -1), (-1, 1, -1), (-1, -1, 1),
        ]
        e = [(0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3)]
        return v, e

    @staticmethod
    def _cube():
        v = [
            (-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1),
            (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1),
        ]
        e = [
            (0, 1), (1, 2), (2, 3), (3, 0),       # back face
            (4, 5), (5, 6), (6, 7), (7, 4),       # front face
            (0, 4), (1, 5), (2, 6), (3, 7),       # connecting edges
        ]
        return v, e

    @staticmethod
    def _octahedron():
        v = [
            (1, 0, 0), (-1, 0, 0),
            (0, 1, 0), (0, -1, 0),
            (0, 0, 1), (0, 0, -1),
        ]
        e = [
            (0, 2), (0, 3), (0, 4), (0, 5),
            (1, 2), (1, 3), (1, 4), (1, 5),
            (2, 4), (2, 5), (3, 4), (3, 5),
        ]
        return v, e

    @classmethod
    def _icosahedron(cls):
        p = cls.PHI
        v = [
            (0, 1, p), (0, -1, p), (0, 1, -p), (0, -1, -p),
            (1, p, 0), (-1, p, 0), (1, -p, 0), (-1, -p, 0),
            (p, 0, 1), (-p, 0, 1), (p, 0, -1), (-p, 0, -1),
        ]
        e = [
            (0, 1), (0, 4), (0, 5), (0, 8), (0, 9),
            (1, 6), (1, 7), (1, 8), (1, 9),
            (2, 3), (2, 4), (2, 5), (2, 10), (2, 11),
            (3, 6), (3, 7), (3, 10), (3, 11),
            (4, 5), (4, 8), (4, 10),
            (5, 9), (5, 11),
            (6, 7), (6, 8), (6, 10),
            (7, 9), (7, 11),
            (8, 10), (9, 11),
        ]
        return v, e

    @classmethod
    def _dodecahedron(cls):
        p = cls.PHI
        ip = 1 / p
        v = [
            ( 1,  1,  1), ( 1,  1, -1), ( 1, -1,  1), ( 1, -1, -1),
            (-1,  1,  1), (-1,  1, -1), (-1, -1,  1), (-1, -1, -1),
            ( 0,  ip,  p), ( 0,  ip, -p), ( 0, -ip,  p), ( 0, -ip, -p),
            ( ip,  p,  0), ( ip, -p,  0), (-ip,  p,  0), (-ip, -p,  0),
            ( p,  0,  ip), ( p,  0, -ip), (-p,  0,  ip), (-p,  0, -ip),
        ]
        e = [
            (0, 8), (0, 12), (0, 16), (1, 9), (1, 12), (1, 17),
            (2, 10), (2, 13), (2, 16), (3, 11), (3, 13), (3, 17),
            (4, 8), (4, 14), (4, 18), (5, 9), (5, 14), (5, 19),
            (6, 10), (6, 15), (6, 18), (7, 11), (7, 15), (7, 19),
            (8, 10), (9, 11), (12, 14), (13, 15), (16, 17), (18, 19),
        ]
        return v, e

    SHAPES: list[tuple[str, callable]] = [
        ("tetrahedron",  _tetrahedron),
        ("cube",         _cube),
        ("octahedron",   _octahedron),
        ("dodecahedron", _dodecahedron),
        ("icosahedron",  _icosahedron),
    ]

    # ------ widget ---------------------------------------------------------

    def __init__(self, master, **kw):
        super().__init__(
            master,
            width=self.CANVAS_W,
            height=self.CANVAS_H,
            bg=COLOR_BG,
            highlightthickness=0,
            **kw,
        )
        self._tick = 0
        self._shape_idx = 0
        self._draw()
        self.after(self.FRAME_MS, self._loop)

    def _loop(self) -> None:
        try:
            self._tick += 1
            if self._tick % self.HOLD_FRAMES == 0:
                self._shape_idx = (self._shape_idx + 1) % len(self.SHAPES)
            self._draw()
        finally:
            self.after(self.FRAME_MS, self._loop)

    # ------ drawing --------------------------------------------------------

    def _project(self, vert: tuple[float, float, float], theta: float):
        """Rotate around Y-axis by theta, then orthographic project to (x, y).
        We add a tiny X-axis tilt so the shape isn't head-on flat."""
        x, y, z = vert
        # Y-axis rotation
        cy, sy = math.cos(theta), math.sin(theta)
        x2 = x * cy + z * sy
        z2 = -x * sy + z * cy
        # Subtle constant X-axis tilt (~25°) for depth
        tilt = 0.45
        ct, st = math.cos(tilt), math.sin(tilt)
        y2 = y * ct - z2 * st
        return x2, y2

    def _draw(self) -> None:
        self.delete("all")
        name, fn = self.SHAPES[self._shape_idx][0], self.SHAPES[self._shape_idx][1]
        verts3, edges = fn()

        theta = (self._tick % 360) * (math.pi / 90.0)  # slow steady spin

        # Project all verts; find bounds for scaling.
        projected = [self._project(v, theta) for v in verts3]
        xs = [p[0] for p in projected]
        ys = [p[1] for p in projected]
        max_extent = max(max(abs(min(xs)), abs(max(xs))),
                          max(abs(min(ys)), abs(max(ys))), 0.001)
        # Leave a 1-pixel margin inside the grid.
        scale = (self.GRID / 2 - 1) / max_extent
        cx = self.GRID / 2
        cy = self.GRID / 2

        pts = [(cx + px * scale, cy + py * scale) for px, py in projected]

        # Rasterize each edge by stepping integer points along it.
        # Pixel set ensures we don't draw the same square twice.
        pixels: set[tuple[int, int]] = set()
        for a, b in edges:
            x1, y1 = pts[a]
            x2, y2 = pts[b]
            steps = max(int(math.hypot(x2 - x1, y2 - y1) * 1.2), 1)
            for s in range(steps + 1):
                t = s / steps
                px = int(round(x1 + (x2 - x1) * t))
                py = int(round(y1 + (y2 - y1) * t))
                if 0 <= px < self.GRID and 0 <= py < self.GRID:
                    pixels.add((px, py))

        # Paint pixels.
        ps = self.PIXEL_SCALE
        for (px, py) in pixels:
            self.create_rectangle(
                px * ps, py * ps, (px + 1) * ps, (py + 1) * ps,
                fill=self.EDGE_COLOR, outline="",
            )


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------


class SymGUI(ctk.CTk):
    """Main window."""

    REFRESH_INTERVAL_MS = 5_000
    BRACKETS_ORDER = (
        "Q_canonical",
        "Q_structure",
        "Q_trinity_R",
        "Q_trinity_P",
        "Q_cross_repo",
        "Q_secrets",
    )

    def __init__(self) -> None:
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        super().__init__()

        self.title(f"Symmetism Coherence — sym v{__version__}")
        self.geometry("680x880")
        self.minsize(560, 720)
        self.configure(fg_color=COLOR_BG)

        self._build_layout()
        self._refresh()

    # ----- layout -----------------------------------------------------------

    def _build_layout(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        # Header (fold + status)
        header = ctk.CTkFrame(self, fg_color=COLOR_PANEL, corner_radius=8)
        header.grid(row=0, column=0, padx=12, pady=(12, 6), sticky="ew")
        header.grid_columnconfigure(0, weight=1)

        self.label_title = ctk.CTkLabel(
            header,
            text="Symmetism Coherence",
            font=ctk.CTkFont(size=14, weight="normal"),
            text_color=COLOR_MUTED,
            anchor="w",
        )
        self.label_title.grid(row=0, column=0, padx=12, pady=(8, 0), sticky="w")

        self.label_fold = ctk.CTkLabel(
            header,
            text="SYM-····-····-····-····",
            font=ctk.CTkFont(size=22, weight="bold", family="Consolas"),
            text_color=COLOR_TEXT,
            anchor="w",
        )
        self.label_fold.grid(row=1, column=0, padx=12, pady=(0, 0), sticky="w")

        self.label_status = ctk.CTkLabel(
            header,
            text="· loading",
            font=ctk.CTkFont(size=12),
            text_color=COLOR_MUTED,
            anchor="w",
        )
        self.label_status.grid(row=2, column=0, padx=12, pady=(0, 8), sticky="w")

        # Trinity rings indicator (right side of header) — same visual
        # language as the public verify page so on-screen and in-browser
        # tell the same story.
        self.rings = TrinityRings(header)
        self.rings.grid(row=0, column=1, rowspan=3, padx=(8, 12), pady=8, sticky="e")
        header.grid_columnconfigure(1, weight=0)

        # Repo rows
        repos = ctk.CTkFrame(self, fg_color=COLOR_PANEL, corner_radius=8)
        repos.grid(row=1, column=0, padx=12, pady=6, sticky="ew")
        repos.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            repos, text="Trinity",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=COLOR_MUTED, anchor="w",
        ).grid(row=0, column=0, padx=12, pady=(8, 0), sticky="w")

        self.label_reflexivity = ctk.CTkLabel(
            repos, text="reflexivity   · · ·",
            font=ctk.CTkFont(size=12, family="Consolas"),
            text_color=COLOR_TEXT, anchor="w",
        )
        self.label_reflexivity.grid(row=1, column=0, padx=12, pady=2, sticky="ew")

        self.label_platform = ctk.CTkLabel(
            repos, text="platform      · · ·",
            font=ctk.CTkFont(size=12, family="Consolas"),
            text_color=COLOR_TEXT, anchor="w",
        )
        self.label_platform.grid(row=2, column=0, padx=12, pady=(2, 8), sticky="ew")

        # Brackets grid
        brackets = ctk.CTkFrame(self, fg_color=COLOR_PANEL, corner_radius=8)
        brackets.grid(row=2, column=0, padx=12, pady=6, sticky="ew")
        for c in range(2):
            brackets.grid_columnconfigure(c, weight=1)

        ctk.CTkLabel(
            brackets, text="{Q_A, H_S} = 0",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=COLOR_MUTED, anchor="w",
        ).grid(row=0, column=0, columnspan=2, padx=12, pady=(8, 4), sticky="w")

        self.bracket_widgets: dict[str, ctk.CTkLabel] = {}
        for i, charge_id in enumerate(self.BRACKETS_ORDER):
            row = 1 + i // 2
            col = i % 2
            lbl = ctk.CTkLabel(
                brackets,
                text=f"·  {charge_id}",
                font=ctk.CTkFont(size=12, family="Consolas"),
                text_color=COLOR_MUTED,
                anchor="w",
            )
            lbl.grid(row=row, column=col, padx=12, pady=2, sticky="ew")
            self.bracket_widgets[charge_id] = lbl

        ctk.CTkLabel(brackets, text="").grid(row=4, column=0, pady=(0, 4))

        # Recent events
        recent = ctk.CTkFrame(self, fg_color=COLOR_PANEL, corner_radius=8)
        recent.grid(row=3, column=0, padx=12, pady=6, sticky="ew")
        recent.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            recent, text="Recent",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=COLOR_MUTED, anchor="w",
        ).grid(row=0, column=0, padx=12, pady=(8, 0), sticky="w")

        self.label_recent = ctk.CTkLabel(
            recent,
            text="(no events yet)",
            font=ctk.CTkFont(size=11, family="Consolas"),
            text_color=COLOR_TEXT,
            anchor="nw",
            justify="left",
        )
        self.label_recent.grid(row=1, column=0, padx=12, pady=(0, 8), sticky="ew")

        # Narrative — taller textbox + one-click copy button.
        narrative = ctk.CTkFrame(self, fg_color=COLOR_PANEL, corner_radius=8)
        narrative.grid(row=4, column=0, padx=12, pady=6, sticky="nsew")
        narrative.grid_columnconfigure(0, weight=1)
        narrative.grid_rowconfigure(1, weight=1)

        narr_header = ctk.CTkFrame(narrative, fg_color=COLOR_PANEL)
        narr_header.grid(row=0, column=0, padx=12, pady=(8, 0), sticky="ew")
        narr_header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            narr_header, text="Narrative",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=COLOR_MUTED, anchor="w",
        ).grid(row=0, column=0, sticky="w")

        self.btn_copy_narrative = ctk.CTkButton(
            narr_header, text="Copy", width=64, height=24,
            font=ctk.CTkFont(size=11),
            fg_color=COLOR_MUTED, hover_color="#7B8593",
            command=self._action_copy_narrative,
        )
        self.btn_copy_narrative.grid(row=0, column=1, sticky="e")

        self.text_narrative = ctk.CTkTextbox(
            narrative,
            font=ctk.CTkFont(size=12),
            text_color=COLOR_TEXT,
            fg_color=COLOR_BG,
            wrap="word",
            height=320,         # tall enough for full narratives without scroll
        )
        self.text_narrative.grid(row=1, column=0, padx=12, pady=(4, 8), sticky="nsew")
        self.text_narrative.insert("1.0", "(no narrative yet — click Explain)")
        self.text_narrative.configure(state="disabled")

        # Buttons
        buttons = ctk.CTkFrame(self, fg_color=COLOR_BG)
        buttons.grid(row=5, column=0, padx=12, pady=(6, 12), sticky="ew")
        for c in range(5):
            buttons.grid_columnconfigure(c, weight=1)

        self.btn_audit = ctk.CTkButton(
            buttons, text="Audit Now", command=self._action_audit,
        )
        self.btn_audit.grid(row=0, column=0, padx=4, sticky="ew")

        self.btn_explain = ctk.CTkButton(
            buttons, text="Explain", command=self._action_explain,
        )
        self.btn_explain.grid(row=0, column=1, padx=4, sticky="ew")

        self.btn_verify = ctk.CTkButton(
            buttons, text="Verify Page", command=self._action_verify_page,
            fg_color=COLOR_MUTED, hover_color="#7B8593",
        )
        self.btn_verify.grid(row=0, column=2, padx=4, sticky="ew")

        self.btn_logs = ctk.CTkButton(
            buttons, text="Logs", command=self._action_open_logs,
            fg_color=COLOR_MUTED, hover_color="#7B8593",
        )
        self.btn_logs.grid(row=0, column=3, padx=4, sticky="ew")

        self.btn_settings = ctk.CTkButton(
            buttons, text="⚙", command=self._action_settings,
            fg_color=COLOR_MUTED, hover_color="#7B8593", width=40,
        )
        self.btn_settings.grid(row=0, column=4, padx=4, sticky="ew")

        # Platonic-solids cycle — purely decorative, bottom-right corner.
        # Cycles tetra → cube → octa → dodeca → icosa, slow spin per
        # shape, no reaction to status (information already lives in
        # the rings + brackets + narrative). Canvas overrides lift()
        # for canvas items, so we rely on place()'s natural z-order.
        self.solids = PlatonicCycle(self)
        self.solids.place(relx=1.0, rely=1.0, x=-12, y=-12, anchor="se")

    # ----- refresh ----------------------------------------------------------

    def _refresh(self) -> None:
        """Re-read disk state, update widgets, schedule next refresh."""
        try:
            self._render(load_status(), load_recent_events(), load_recent_narratives())
        except Exception as e:
            self.label_status.configure(
                text=f"render error: {e}", text_color=COLOR_ALARM,
            )
        self.after(self.REFRESH_INTERVAL_MS, self._refresh)

    def _render(
        self,
        status: dict[str, Any] | None,
        events: list[dict[str, Any]],
        narratives: list[dict[str, Any]],
    ) -> None:
        if status is None:
            self.label_fold.configure(text="SYM-····-····-····-····")
            self.label_status.configure(
                text="(daemon not running — start the Scheduled Task)",
                text_color=COLOR_MUTED,
            )
            self.rings.set_state("drift")
            return

        self.label_fold.configure(text=status.get("system_fold") or "SYM-?")

        overall = status.get("status", "?")
        alarm = status.get("alarm", False)
        trigger = status.get("trigger_kind", "?")
        updated = status.get("updated_at", "")
        age = humanize_age(updated) if updated else "?"
        status_text = (
            f"{_status_glyph(overall)}  {overall}  ·  trigger: {trigger}  ·  {age}"
        )
        if alarm:
            status_text = f"⚠ ALARM  ·  {status_text}"
        self.label_status.configure(
            text=status_text,
            text_color=_status_color("drift_alarm" if alarm else overall),
        )

        # Rings indicator tracks the same coherence state the public
        # verify page uses.
        self.rings.set_state(coherence_state(status))

        # Repo rows
        trinity = status.get("trinity") or {}
        for repo_key, widget in (
            ("reflexivity", self.label_reflexivity),
            ("platform", self.label_platform),
        ):
            t = trinity.get(repo_key)
            text = f"{repo_key:14s} {t}" if t else f"{repo_key:14s} ·"
            widget.configure(text=text)

        # Brackets
        brackets = status.get("brackets") or {}
        for charge_id, widget in self.bracket_widgets.items():
            b = brackets.get(charge_id) or {}
            bstatus = b.get("status", "?")
            descriptor = (b.get("descriptor") or b.get("value") or "")
            descriptor = str(descriptor)
            if len(descriptor) > 40:
                descriptor = descriptor[:38] + "…"
            text = f"{_status_glyph(bstatus)}  {charge_id:14s} {descriptor}"
            widget.configure(text=text, text_color=_status_color(bstatus))

        # Recent events
        if events:
            lines: list[str] = []
            for ev in events[:8]:
                t = (ev.get("occurred_at") or "")[11:19]  # HH:MM:SS
                kind = ev.get("kind", "?")
                detail = ev.get("detail") or ""
                if isinstance(detail, dict):
                    detail = json.dumps(detail, ensure_ascii=False)
                detail = str(detail)
                if len(detail) > 60:
                    detail = detail[:58] + "…"
                lines.append(f"{t}  {kind:11s} {detail}")
            self.label_recent.configure(text="\n".join(lines))
        else:
            self.label_recent.configure(text="(no events yet)")

        # Narrative
        if narratives:
            n = narratives[0]
            self.text_narrative.configure(state="normal")
            self.text_narrative.delete("1.0", "end")
            ts = n.get("generated_at", "")[:19].replace("T", " ")
            trig = n.get("trigger", "")
            body = n.get("text", "")
            header = f"[{ts} · {trig}]\n" if ts else ""
            self.text_narrative.insert("1.0", header + body)
            self.text_narrative.configure(state="disabled")

    # ----- button handlers --------------------------------------------------

    def _run_in_thread(self, fn) -> None:
        threading.Thread(target=fn, daemon=True).start()

    def _action_audit(self) -> None:
        """Force a fresh audit cycle. Runs in a thread to avoid blocking Tk."""
        self.btn_audit.configure(state="disabled", text="Auditing…")

        def work() -> None:
            try:
                from symverify.daemon import TriggerEvent, run_audit_cycle
                run_audit_cycle(TriggerEvent(kind="manual"))
            except Exception as e:
                self.after(0, lambda: self.label_status.configure(
                    text=f"audit error: {e}", text_color=COLOR_ALARM,
                ))
            finally:
                self.after(0, lambda: self.btn_audit.configure(
                    state="normal", text="Audit Now",
                ))
                self.after(50, self._refresh)

        self._run_in_thread(work)

    def _action_explain(self) -> None:
        """Generate a narrative for the current snapshot."""
        snap = load_status()
        if snap is None:
            self._set_narrative("(no current snapshot to explain — try Audit Now first)")
            return
        self.btn_explain.configure(state="disabled", text="Asking…")

        def work() -> None:
            try:
                from symverify import db, narrative

                snap_for_narr = {
                    "status": snap.get("status"),
                    "trinity_r": (snap.get("trinity") or {}).get("reflexivity"),
                    "trinity_p": (snap.get("trinity") or {}).get("platform"),
                    "system_fold": snap.get("system_fold"),
                    "brackets": snap.get("brackets") or {},
                }
                text = narrative.narrate(snap_for_narr, trigger="gui-explain")
                if text is None:
                    text = narrative.unavailable_text(
                        "OpenAI client unavailable or API error"
                    )
                # Persist so it shows up in `sym log` too.
                try:
                    with db.connect() as conn:
                        db.insert_narrative(
                            conn, trigger="gui-explain", text=text, snapshot_id=None,
                        )
                except Exception:
                    pass
                self.after(0, lambda: self._set_narrative(text))
            except Exception as e:
                self.after(0, lambda: self._set_narrative(f"(error: {e})"))
            finally:
                self.after(0, lambda: self.btn_explain.configure(
                    state="normal", text="Explain",
                ))
                self.after(50, self._refresh)

        self._run_in_thread(work)

    def _set_narrative(self, text: str) -> None:
        self.text_narrative.configure(state="normal")
        self.text_narrative.delete("1.0", "end")
        self.text_narrative.insert("1.0", text)
        self.text_narrative.configure(state="disabled")

    def _action_verify_page(self) -> None:
        webbrowser.open("https://symmetism.com")

    def _action_open_logs(self) -> None:
        log_path = config.state_dir() / "daemon.log"
        if not log_path.is_file():
            self.label_status.configure(
                text=f"(no log at {log_path})", text_color=COLOR_MUTED,
            )
            return
        if sys.platform == "win32":
            os.startfile(str(log_path))  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", str(log_path)])

    def _action_settings(self) -> None:
        SettingsWindow(self)

    def _action_copy_narrative(self) -> None:
        """Copy the current narrative panel text to the system clipboard."""
        text = self.text_narrative.get("1.0", "end").strip()
        if not text or text.startswith("(no narrative"):
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()  # required on Windows so clipboard sticks after window loses focus
        # Quick visual confirmation: button label flashes "Copied".
        self.btn_copy_narrative.configure(text="Copied ✓")
        self.after(1200, lambda: self.btn_copy_narrative.configure(text="Copy"))


# ---------------------------------------------------------------------------
# Settings popup — install/uninstall service, refresh interval, about
# ---------------------------------------------------------------------------


class SettingsWindow(ctk.CTkToplevel):
    """Small popup for service install/uninstall + about info."""

    def __init__(self, master: SymGUI):
        super().__init__(master)
        self.title("Settings")
        self.geometry("420x340")
        self.configure(fg_color=COLOR_BG)
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        ctk.CTkLabel(
            self, text="Daemon Service",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=COLOR_MUTED, anchor="w",
        ).pack(padx=18, pady=(18, 4), fill="x")

        self.label_service = ctk.CTkLabel(
            self, text="checking…",
            font=ctk.CTkFont(size=11, family="Consolas"),
            text_color=COLOR_TEXT, anchor="w", justify="left",
        )
        self.label_service.pack(padx=18, pady=(0, 8), fill="x")

        btn_row = ctk.CTkFrame(self, fg_color=COLOR_BG)
        btn_row.pack(padx=18, pady=4, fill="x")
        ctk.CTkButton(btn_row, text="Install", command=self._install).pack(
            side="left", padx=(0, 4), expand=True, fill="x"
        )
        ctk.CTkButton(
            btn_row, text="Uninstall", command=self._uninstall,
            fg_color=COLOR_ALARM, hover_color="#C45050",
        ).pack(side="left", padx=4, expand=True, fill="x")
        ctk.CTkButton(
            btn_row, text="Refresh", command=self._poll,
            fg_color=COLOR_MUTED, hover_color="#7B8593",
        ).pack(side="left", padx=(4, 0), expand=True, fill="x")

        ctk.CTkLabel(
            self, text="About",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=COLOR_MUTED, anchor="w",
        ).pack(padx=18, pady=(16, 4), fill="x")

        ctk.CTkLabel(
            self,
            text=(
                f"sym  v{__version__}\n"
                f"state dir:  {config.state_dir()}\n"
                f"config dir: {config.config_dir()}"
            ),
            font=ctk.CTkFont(size=11, family="Consolas"),
            text_color=COLOR_TEXT, anchor="w", justify="left",
        ).pack(padx=18, pady=(0, 12), fill="x")

        self._poll()

    def _poll(self) -> None:
        from symverify import daemon as _daemon

        info = _daemon.query_windows_service(_daemon.DEFAULT_TASK_NAME)
        if info is None:
            self.label_service.configure(
                text=f"(not installed: '{_daemon.DEFAULT_TASK_NAME}')",
                text_color=COLOR_MUTED,
            )
        else:
            lines = [f"  {k}: {v}" for k, v in info.items()
                      if k in ("TaskName", "Status", "Last Run Time", "Last Result")]
            self.label_service.configure(
                text="\n".join(lines), text_color=COLOR_TEXT,
            )

    def _install(self) -> None:
        from symverify import daemon as _daemon

        try:
            _daemon.install_windows_service(_daemon.DEFAULT_TASK_NAME)
        except Exception as e:
            self.label_service.configure(text=f"install error: {e}",
                                          text_color=COLOR_ALARM)
            return
        self._poll()

    def _uninstall(self) -> None:
        from symverify import daemon as _daemon

        try:
            _daemon.uninstall_windows_service(_daemon.DEFAULT_TASK_NAME)
        except Exception as e:
            self.label_service.configure(text=f"uninstall error: {e}",
                                          text_color=COLOR_ALARM)
            return
        self._poll()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Launch the GUI. Used by `sym gui` and the bundled exe."""
    app = SymGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
