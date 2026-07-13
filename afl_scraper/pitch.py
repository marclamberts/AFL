"""Draw an AFL oval pitch and scatter x/y event data on it (original coordinates).

x: 0..100 (goal to goal), y: -50..50 (wing to wing). The forward 50 arcs are drawn
so their ends exactly meet the boundary oval, by numerically solving for where the
goal-arc ellipse intersects the boundary ellipse.

This replaces notebook cells 5 and 6, which defined the same pitch-drawing code
twice (the second copy only added the CSV-scatter helper on top).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.patches import Arc, Circle, Ellipse, Rectangle

OVAL_HEIGHT = 82.0      # oval height in raw y-units
CENTER_SQ = 30.0
R_OUT = 3.0
R_IN = 1.8
GS_DEPTH = 5.5
GS_HALFWID = 3.2

ARC_RX = 30.0
ARC_RY = ARC_RX * (OVAL_HEIGHT / 100.0)

POST_X_OFF = 2.5
POST_GAP = 3.5


def boundary_val(x, y, H=OVAL_HEIGHT):
    """=0 on the oval boundary (ellipse centered at (50,0), width=100, height=H)."""
    return ((x - 50.0) / 50.0) ** 2 + (y / (H / 2.0)) ** 2 - 1.0


def goal_arc_point(cx, rx, ry, t):
    """Parametric point on a goal arc ellipse centered at (cx,0)."""
    return cx + rx * np.cos(t), ry * np.sin(t)


def find_theta_intersections(cx, rx, ry, t_lo, t_hi, steps=720, tol=1e-10, max_iter=60):
    """Angles (radians) where the goal-arc ellipse meets the boundary ellipse."""
    ts = np.linspace(t_lo, t_hi, steps + 1)
    vals = np.asarray([boundary_val(*goal_arc_point(cx, rx, ry, t)) for t in ts])

    roots = []
    for i in range(steps):
        v0, v1 = vals[i], vals[i + 1]
        if v0 == 0:
            roots.append(ts[i])
            continue
        if v0 * v1 < 0:
            a, b, fa = ts[i], ts[i + 1], v0
            for _ in range(max_iter):
                m = 0.5 * (a + b)
                fm = boundary_val(*goal_arc_point(cx, rx, ry, m))
                if abs(fm) < tol:
                    a = b = m
                    break
                if fa * fm <= 0:
                    b = m
                else:
                    a, fa = m, fm
            roots.append(0.5 * (a + b))

    roots.sort()
    dedup = []
    for r in roots:
        if not dedup or abs(r - dedup[-1]) > 1e-3:
            dedup.append(r)
    return dedup


def draw_forward_arc(ax, cx, rx, ry, side):
    if side == "left":
        thetas = find_theta_intersections(cx, rx, ry, -np.pi / 2, np.pi / 2)
    else:
        thetas = find_theta_intersections(cx, rx, ry, np.pi / 2, 3 * np.pi / 2)

    if len(thetas) >= 2:
        t1, t2 = thetas[0], thetas[-1]
        ax.add_patch(Arc((cx, 0), width=2 * rx, height=2 * ry, angle=0,
                          theta1=np.degrees(t1), theta2=np.degrees(t2), linewidth=1, fill=False))
    else:
        a1, a2 = (-35, 35) if side == "left" else (145, 215)
        ax.add_patch(Arc((cx, 0), width=2 * rx, height=2 * ry, angle=0, theta1=a1, theta2=a2, linewidth=1, fill=False))


def draw_afl_pitch(ax=None):
    if ax is None:
        _, ax = plt.subplots(figsize=(10, 7))

    ax.add_patch(Ellipse((50, 0), width=100, height=OVAL_HEIGHT, fill=False, linewidth=1.5))

    draw_forward_arc(ax, cx=0.0, rx=ARC_RX, ry=ARC_RY, side="left")
    draw_forward_arc(ax, cx=100.0, rx=ARC_RX, ry=ARC_RY, side="right")

    ax.add_patch(Rectangle((50 - CENTER_SQ / 2, -CENTER_SQ / 2), CENTER_SQ, CENTER_SQ, fill=False, linewidth=1))
    ax.add_patch(Circle((50, 0), radius=R_OUT, fill=False, linewidth=1))
    ax.add_patch(Circle((50, 0), radius=R_IN, fill=False, linewidth=1))

    ax.add_patch(Rectangle((0, -GS_HALFWID), GS_DEPTH, 2 * GS_HALFWID, fill=False, linewidth=1))
    ax.add_patch(Rectangle((100 - GS_DEPTH, -GS_HALFWID), GS_DEPTH, 2 * GS_HALFWID, fill=False, linewidth=1))

    base_y = -1.5 * POST_GAP
    for i in range(4):
        y = base_y + i * POST_GAP
        ax.plot([-POST_X_OFF, 0], [y, y], linewidth=2)
        ax.plot([100, 100 + POST_X_OFF], [y, y], linewidth=2)

    ax.set_xlim(0, 100)
    ax.set_ylim(-50, 50)
    ax.set_aspect("equal", "box")
    ax.set_xticks([])
    ax.set_yticks([])
    ax.grid(False)
    return ax


def pitch_scatter_from_csv(csv_path, x_col="x", y_col="y", filter_col=None, filter_value=None,
                            s=24, alpha=0.9, marker="o", out_png="afl_xy_scatter.png"):
    """Overlay a scatter on the pitch using original CSV coordinates."""
    df = pd.read_csv(csv_path)
    if filter_col is not None and filter_col in df.columns:
        df = df[df[filter_col].astype(str) == str(filter_value)]

    x = pd.to_numeric(df[x_col], errors="coerce")
    y = pd.to_numeric(df[y_col], errors="coerce")
    mask = x.notna() & y.notna()
    x, y = x[mask], y[mask]

    ax = draw_afl_pitch()
    ax.scatter(x, y, s=s, alpha=alpha, marker=marker)
    ax.set_title("x-y scatter on AFL pitch (original coordinates)")

    plt.tight_layout()
    plt.savefig(out_png, dpi=150)
    print(f"Saved -> {out_png} | points plotted: {len(x)}")
    return ax
