import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
fig, ax = plt.subplots(figsize=(9, 5.2)); ax.set_xlim(0, 18); ax.set_ylim(0, 10.5); ax.axis("off")
def box(x, y, w, h, title, pins, side, col):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.05", fc=col, ec="k", lw=1.5))
    ax.text(x + w / 2, y + h + 0.25, title, ha="center", va="bottom", fontsize=11, weight="bold")
    pos = {}
    for i, p in enumerate(pins):
        py = y + h - 0.6 - i * (h - 1.0) / max(1, len(pins) - 1)
        px = x + w if side == "r" else x
        ax.plot([px, px + (0.5 if side == "r" else -0.5)], [py, py], color="k", lw=1.5)
        ax.text(px - 0.15 if side == "r" else px + 0.15, py, p, ha="right" if side == "r" else "left", va="center", fontsize=10)
        pos[p] = (px + (0.5 if side == "r" else -0.5), py)
    return pos
N = box(1.0, 2.2, 3.6, 6.4, "Arduino Nano V3", ["5V", "GND", "A4 (SDA)", "A5 (SCL)", "A0"], "r", "#eaf2f8")
M = box(12.0, 5.6, 3.4, 3.6, "MPU-6050 (on sternum)", ["VCC", "GND", "SDA", "SCL"], "l", "#fdf2e9")
P = box(12.0, 0.9, 3.4, 2.8, "Pulse sensor (fingertip)", ["+", "−", "S"], "l", "#fdedec")
def route(a, b, x, c, lab):
    ax.plot([a[0], x, x, b[0]], [a[1], a[1], b[1], b[1]], color=c, lw=2.2, solid_joinstyle="round")
    ax.plot(x, b[1], "o", color=c, ms=4)
route(N["5V"], M["VCC"], 6.0, "#d62728", ""); route(N["5V"], P["+"], 6.0, "#d62728", "")
route(N["GND"], M["GND"], 7.0, "#222", ""); route(N["GND"], P["−"], 7.0, "#222", "")
route(N["A4 (SDA)"], M["SDA"], 8.0, "#1f77b4", ""); route(N["A5 (SCL)"], M["SCL"], 9.0, "#2ca02c", "")
route(N["A0"], P["S"], 10.0, "#9467bd", "")
ax.annotate("USB: 5 V power\n+ serial log", xy=(1.0, 5.4), xytext=(-0.2, 9.6), fontsize=9, arrowprops=dict(arrowstyle="-", lw=1.2))
for x, c, t in ((6.0, "#d62728", "5 V"), (7.0, "#222", "GND"), (8.0, "#1f77b4", "SDA"), (9.0, "#2ca02c", "SCL"), (10.0, "#9467bd", "PPG")):
    ax.text(x, 9.9, t, color=c, ha="center", fontsize=9, weight="bold")
ax.text(0.2, 0.1, "No discrete parts needed: MPU-6050 module has its own 3.3 V regulator + I²C pull-ups (5 V tolerant on the Nano);\n"
        "pulse sensor has its own LED driver + amplifier. 7 jumper wires on one half breadboard.", fontsize=8.5)
fig.tight_layout(); fig.savefig("/tmp/sp/hardware/circuit.png", dpi=190)
