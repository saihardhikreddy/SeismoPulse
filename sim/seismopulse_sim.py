"""
SeismoPulse simulator + algorithm port.

1. Physiological model (30 min, 500 Hz): blood-pressure protocol (rest, mental stress,
   recovery, exercise, recovery), heart rate with respiratory sinus arrhythmia,
   pre-ejection period (PEP) that shortens with sympathetic activation, and
   pulse transit time from the Moens-Korteweg / exponential-elasticity model
       PTT = PTT0 * exp(-alpha * (MAP - MAP0) / 2)
2. Sensor models: MPU-6050 SCG (aortic-opening complex, respiration, 16-bit
   quantisation, motion artefacts) and generic PPG (10-bit ADC, baseline wander).
3. Port of the firmware pipeline: SCG band-pass, PPG intersecting-tangent foot,
   AO look-back search, SQI, EMA, log-PTT calibration.
4. Comparison with the conventional ECG-style pulse ARRIVAL time (PAT = PEP + PTT).

Run:  python seismopulse_sim.py   (writes figures + results.json to ../docs/)
"""
import json, os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs")
os.makedirs(OUT, exist_ok=True)
rng = np.random.default_rng(23)
FS = 500
DUR = 30 * 60
N = FS * DUR
t = np.arange(N) / FS

# ---------------- 1. physiology ----------------
def smooth_step(t, t0, t1, rise=60.0):
    return 1 / (1 + np.exp(-(t - t0) / (rise / 6))) - 1 / (1 + np.exp(-(t - t1) / (rise / 6)))

mental = smooth_step(t, 5 * 60, 10 * 60, 40)          # 5-10 min mental arithmetic
exercise = smooth_step(t, 15 * 60, 21 * 60, 90)       # 15-21 min handgrip/stepping
drift = 4 * np.sin(2 * np.pi * t / 1500)
mayer = 3 * np.sin(2 * np.pi * 0.1 * t + 1.0)
MAP = 92 + 12 * mental + 22 * exercise + drift + mayer
PP = 40 + 6 * mental + 18 * exercise                  # pulse pressure widens with exercise
SBP = MAP + 2 * PP / 3; DBP = MAP - PP / 3
resp = np.sin(2 * np.pi * 0.25 * t)
HR = 68 + 14 * mental + 30 * exercise + 3 * resp

beats_R = [1.0]
while beats_R[-1] < DUR - 2:
    i = int(beats_R[-1] * FS)
    beats_R.append(beats_R[-1] + 60 / HR[i] * (1 + 0.01 * rng.standard_normal()))
beats_R = np.array(beats_R[:-1])
bi = (beats_R * FS).astype(int)
PEP = 0.105 - 0.0007 * (HR[bi] - 68) - 0.012 * mental[bi] + 0.003 * rng.standard_normal(len(bi))
alpha, PTT0, MAP0 = 0.017, 0.180, 92.0
PTT = PTT0 * np.exp(-alpha * (MAP[bi] - MAP0) / 2) * (1 + 0.01 * rng.standard_normal(len(bi)))
AO = beats_R + PEP
FOOT = AO + PTT

# ---------------- 2. sensors ----------------
scg = np.zeros(N)
for ao in AO:
    k = np.arange(-60, 150); tt = k / FS
    w = (1.0 * np.exp(-(tt / 0.012) ** 2) * np.cos(2 * np.pi * 28 * tt)       # AO complex
         - 0.45 * np.exp(-((tt + 0.03) / 0.010) ** 2)                         # IM
         + 0.35 * np.exp(-((tt - 0.20) / 0.02) ** 2) * np.cos(2 * np.pi * 22 * (tt - 0.2)))  # AC/RE
    j = int(ao * FS) + k; m = (j >= 0) & (j < N)
    scg[j[m]] += w[m] * rng.uniform(0.8, 1.2)
amp_mg = 8.0                                            # AO peak ~8 mg on sternum
scg_g = amp_mg / 1000 * scg + 0.03 * resp + 0.0006 * rng.standard_normal(N) + 1.0
# motion artefacts: 12 bursts
art = np.zeros(N, bool)
for _ in range(12):
    s = rng.integers(0, N - 3 * FS); d = int(rng.uniform(1, 3) * FS)
    scg_g[s:s + d] += 0.03 * rng.standard_normal(d); art[s:s + d] = True
az = np.round(scg_g * 16384).astype(np.int32)           # MPU-6050 +-2 g

ppg = np.zeros(N)
for f in FOOT:
    k = np.arange(0, int(0.9 * FS)); tt = k / FS
    w = (1 / (1 + np.exp(-(tt - 0.06) / 0.018)) * np.exp(-tt / 0.45)
         + 0.18 * np.exp(-((tt - 0.33) / 0.05) ** 2))
    w -= w[0]
    j = int(f * FS) + k; m = j < N
    ppg[j[m]] += w[m]
ppg_v = 2.5 + 0.35 * ppg + 0.08 * resp + 0.004 * rng.standard_normal(N)
ppg_v[art] += 0.15 * rng.standard_normal(art.sum())
adc = np.clip(np.round(ppg_v / 5 * 1023), 0, 1023).astype(np.int32)

# ---------------- 3. firmware port ----------------
def run_pipeline(az, adc, ref_times=None):
    """ref_times=None -> SCG AO reference (SeismoPulse); else array of R times (ECG PAT baseline)."""
    hp = lp = 0; prev = int(az[0])
    scg_f = np.zeros(N, np.int32)
    for n in range(N):
        a = int(az[n]); hp = ((hp * 15) >> 4) + a - prev; prev = a
        lp += ((hp - lp) * 2) >> 2; scg_f[n] = lp
    ppgLp = prevLp = int(adc[0]) << 4; ppgMin = 10 ** 9; rising = False; pk = pkI = pkV = 0
    thr = 8; ampThr = 150; refr = 0; last_foot = 0
    out = []
    for n in range(N):
        raw = int(adc[n]) << 4; ppgLp += (raw - ppgLp) >> 3
        sl = ppgLp - prevLp; prevLp = ppgLp
        if n - max(last_foot, refr) > 2 * FS: thr, ampThr = 8, 150   # lost lock -> reset thresholds
        if not rising and ppgLp < ppgMin: ppgMin = ppgLp
        if n > refr and sl > thr:
            rising = True
            if sl > pk: pk, pkI, pkV = sl, n, ppgLp
        elif rising and sl <= 0:
            amp = ppgLp - ppgMin
            if amp > ampThr:
                dt = (pkV - ppgMin) // pk if pk > 0 else 0
                foot = pkI - int(np.clip(dt, 0, 50))
                thr = max(4, (thr * 3 + (min(pk, 3 * thr) * 2) // 5) >> 2)
                ampThr = max(60, (ampThr * 3 + (min(amp, 3 * ampThr) * 2) // 5) >> 2)
                rr = (foot - last_foot) * 2; last_foot = foot
                if ref_times is None:
                    lo, hi = foot - 175, foot - 30
                    if lo > 0:
                        k = lo + int(np.argmax(scg_f[lo:hi])); ptt = (foot - k) * 2.0
                        out.append((foot / FS, rr, ptt))
                else:
                    prior = ref_times[ref_times * FS < foot - 30]
                    if len(prior): out.append((foot / FS, rr, (foot - prior[-1] * FS) * 2.0))
                refr = n + 125
            rising = False; pk = 0; ppgMin = 10 ** 9
    return np.array(out), scg_f

def sqi_ema(beats, lo, hi):
    ema = 0; res = []; good = 0
    for (tf, rr, p) in beats:
        ok = 330 <= rr <= 1500 and lo < p < hi
        if ok and ema > 0 and abs(p - ema) > 0.25 * ema: ok = False
        if not ok:
            if ema == 0 and 330 <= rr <= 1500 and lo < p < hi: ema = p
            continue
        good += 1; ema = p if ema == 0 else 0.85 * ema + 0.15 * p
        res.append((tf, ema))
    return np.array(res), good / max(1, len(beats))

beats_scg, scg_f = run_pipeline(az, adc)
beats_pat, _ = run_pipeline(az, adc, ref_times=beats_R)
est_scg, sqi_scg = sqi_ema(beats_scg, 60, 350)
est_pat, sqi_pat = sqi_ema(beats_pat, 120, 500)

def truth_at(tt, arr): return np.interp(tt, t, arr)

def calibrate(est, cal_t, two_point, b_pop=-55.0):
    lnp = np.log(est[:, 1]); tt = est[:, 0]
    def ln_at(c):
        m = (tt > c - 20) & (tt < c)
        if not m.any():   # an empty window would silently turn every estimate into NaN
            raise ValueError(f"no accepted beats in the 20 s before the cuff reading at t={c:.0f} s")
        return lnp[m].mean()
    m1 = truth_at(cal_t[0], MAP); l1 = ln_at(cal_t[0])
    b = b_pop
    if two_point:
        m2 = truth_at(cal_t[1], MAP); l2 = ln_at(cal_t[1]); b = (m2 - m1) / (l2 - l1)
    a = m1 - b * l1
    return a + b * lnp, a, b

res = {}
CAL = (120.0, 19 * 60.0)
for name, est, sqi in (("SeismoPulse_SCG_PTT", est_scg, sqi_scg), ("ECG_style_PAT", est_pat, sqi_pat)):
    for mode in ("one_point", "two_point"):
        mapE, a, b = calibrate(est, CAL, mode == "two_point")
        tt = est[:, 0]; ev = tt > 150
        if mode == "two_point": ev &= ~((tt > CAL[1] - 20) & (tt < CAL[1]))
        mt = truth_at(tt, MAP)
        err = mapE[ev] - mt[ev]
        res[f"{name}/{mode}"] = dict(MAE_mmHg=round(float(np.mean(np.abs(err))), 2),
                                     bias=round(float(err.mean()), 2), SD=round(float(err.std()), 2),
                                     r=round(float(np.corrcoef(mapE[ev], mt[ev])[0, 1]), 3),
                                     slope_b=round(float(b), 1),
                                     # Bland-Altman 95 % limits of agreement (bias +/- 1.96 SD)
                                     LoA_mmHg=[round(float(err.mean() - 1.96 * err.std()), 2),
                                               round(float(err.mean() + 1.96 * err.std()), 2)])
        if name.startswith("Seismo") and mode == "two_point": best = (tt, mapE, mt, ev)
# AO timing accuracy
ao_err = []
for tf, rr, p in beats_scg:
    j = np.argmin(np.abs(FOOT - tf))
    if abs(FOOT[j] - tf) < 0.05: ao_err.append(p - PTT[j] * 1000)
ao_err = np.array(ao_err)
res["beats_true"] = int(len(beats_R)); res["beats_detected"] = int(len(beats_scg))
res["SQI_pass_rate"] = round(float(sqi_scg), 3)
res["PTT_constant_offset_ms"] = round(float(np.median(ao_err)), 1)
res["PTT_jitter_ms_SD_after_offset"] = round(float(np.std(ao_err[np.abs(ao_err - np.median(ao_err)) < 30])), 2)
res["PEP_range_ms"] = [round(float(PEP.min() * 1000), 1), round(float(PEP.max() * 1000), 1)]
res["MAP_range_true"] = [round(float(MAP.min()), 1), round(float(MAP.max()), 1)]
print(json.dumps(res, indent=1))
with open(os.path.join(OUT, "results.json"), "w") as f:
    json.dump(res, f, indent=1)

# ---------------- figures ----------------
plt.rcParams.update({"font.size": 9})
# Fig A: waveforms of 3 beats
z0 = 60.0; m = (t >= z0) & (t < z0 + 3)
fig, ax = plt.subplots(2, 1, figsize=(8, 4), sharex=True)
ax[0].plot(t[m], scg_f[m] / 16.384, color="#1f6f8b", lw=0.8); ax[0].set_ylabel("SCG (mg,\nband-passed)")
ax[1].plot(t[m], adc[m], color="#c0392b", lw=0.9); ax[1].set_ylabel("PPG (ADC)")
for a_, f in zip(AO, FOOT):
    if z0 < a_ < z0 + 3 and f < z0 + 3:
        ax[0].axvline(a_, color="k", ls=":", lw=0.8); ax[1].axvline(a_, color="k", ls=":", lw=0.8)
        ax[1].axvline(f, color="#c0392b", ls="--", lw=0.8)
        ax[1].annotate("", xy=(f, 560), xytext=(a_, 560), arrowprops=dict(arrowstyle="<->"))
        ax[1].text((a_ + f) / 2, 568, "PTT", ha="center", fontsize=8)
ax[1].set_xlabel("time (s)"); ax[0].set_title("Aortic-valve opening in the SCG (dotted) to PPG foot (dashed) = pulse transit time", fontsize=10)
fig.tight_layout(); fig.savefig(os.path.join(OUT, "fig_waveforms.png"), dpi=170); plt.close(fig)

# Fig B: tracking
tt, mapE, mt, ev = best
mapPat, _, _ = calibrate(est_pat, CAL, True)
fig, ax = plt.subplots(figsize=(8, 3.3))
ax.plot(t[::25] / 60, MAP[::25], color="k", lw=0.6, label="true MAP")
ax.plot(est_pat[:, 0] / 60, mapPat, color="#e67e22", lw=0.7, alpha=0.8, label="ECG-style PAT estimate")
ax.plot(tt / 60, mapE, color="#1f6f8b", lw=0.8, label="SeismoPulse (SCG-PTT)")
for c in CAL: ax.axvline(c / 60, color="#27ae60", ls="--", lw=0.8)
ax.axvspan(5, 10, color="#9b59b6", alpha=0.07); ax.axvspan(15, 21, color="#c0392b", alpha=0.07)
ax.text(7.5, ax.get_ylim()[1] - 3, "mental stress", ha="center", fontsize=8)
ax.text(18, ax.get_ylim()[1] - 3, "exercise", ha="center", fontsize=8)
ax.set_xlabel("time (min)"); ax.set_ylabel("MAP (mmHg)"); ax.legend(fontsize=8, frameon=True, loc="upper left")
ax.set_title("30-min protocol, two-point cuff calibration (green dashed)", fontsize=10)
fig.tight_layout(); fig.savefig(os.path.join(OUT, "fig_tracking.png"), dpi=170); plt.close(fig)

# Fig C: Bland-Altman
d = mapE[ev] - mt[ev]; mean_ = (mapE[ev] + mt[ev]) / 2
fig, ax = plt.subplots(figsize=(4.6, 3.3))
ax.scatter(mean_, d, s=2, alpha=0.3, color="#1f6f8b")
for y, ls in ((d.mean(), "-"), (d.mean() + 1.96 * d.std(), "--"), (d.mean() - 1.96 * d.std(), "--")):
    ax.axhline(y, color="#c0392b", ls=ls, lw=1)
ax.set_xlabel("mean of estimate and truth (mmHg)"); ax.set_ylabel("estimate - truth (mmHg)")
ax.set_title(f"Bland-Altman: bias {d.mean():.1f}, LoA ±{1.96*d.std():.1f} mmHg", fontsize=9)
fig.tight_layout(); fig.savefig(os.path.join(OUT, "fig_bland_altman.png"), dpi=170); plt.close(fig)
