#!/usr/bin/python3
# Hello, welcome to my code!
# I like to keep things organized, so this code follows this order: Definitions, Engine and Execution.
# Functions are grouped together too, this makes debugging more easy
# You will also find very big headers telling what that portion of the code does, for example:
###############################################
# CONTROLLER PCB LOGIC
###############################################
# Pretty cool huh?
# You may also find stuff like:
# --------------- Something Something --------------------
# Good forking!
"""Where the clamp bites + what the real clock is - two questions, one root round.

WHY THIS SCRIPT EXISTS
  (a) The clock came out 0 in rounds 2 and 3 silently: turbostat only emits the
      Bzy_MHz/PkgWatt columns with MSR access; without it, it prints TWO columns
      (Core CPU) and exits with rc=0. A parser that demanded 4 fields received
      nothing and reported 0. Here turbostat runs with stderr CAPTURED and the raw
      output is dumped.
  (b) The clock derived from APERF/MPERF came out 7085 MHz (impossible) in the old
      rounds. CAUSE FOUND (24/09): the MPERF reference is the maximum efficiency
      ratio (MSR_PLATFORM_INFO bits 15:8; here 15 -> 1500 MHz), NOT the 2600 MHz of
      the TSC. Here the RAW DELTAS are printed per CPU, with a sanity check: any
      clock > fused turbo (4400 MHz, MSR 0x1AD) is marked INVALID and never reported
      as truth.
  (c) The 15 W ceiling is platform policy, but so far we only tried asking for 45 W
      (far above everything). Asking for 18 W - just above the granted ceiling
      (15 W) and inside what RAPL declares it accepts (28 W) - marks EXACTLY where
      the clamp bites.

WHAT IT WRITES TO THE HARDWARE (all reversible, restored in the finally block)
  - MSR 0x610 (RAPL PKG_POWER_LIMIT): only the PL1 field (bits 14:0), to 18 W.
    It does NOT touch tau/window (bits 23:16) nor PL2 (bits 46:32, stays at 60 W >= PL1).
  - Nothing else: EPP, platform_profile, iGPU, TCC offset stay untouched.

SAFETY
  - It reads the ORIGINAL value of 0x610 before anything else and restores it in the
    finally block, with a confirmation readback at the end.
  - It never types a BIOS password anywhere; it does not touch firmware.

Usage:  pkexec /usr/bin/python3 /absolute/path/tools/clock_and_clamp.py [--check]
      --check validates paths and binaries and exits (no root needed).
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from throttlefed_paths import pkg_zone

_PKG = pkg_zone()
# Empty string when this machine has no powercap package zone: --check then
# reports the missing paths instead of the run failing halfway through.
ENERGY = str(_PKG / "energy_uj") if _PKG else ""
ENERGY_MAX = str(_PKG / "max_energy_range_uj") if _PKG else ""

def _scratch_dir():
    """Instrument log directory, outside the repo and outside any personal path."""
    base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    d = base / "throttlefed"
    d.mkdir(parents=True, exist_ok=True)
    return d



TOOLS = str(Path(__file__).resolve().parent)
FLOPS = TOOLS + "/flops"                 # AVX-512 (flops_avx2 = AVX2)
SCRATCH = str(_scratch_dir())
JSON_OUT = SCRATCH + "/clock_clamp.json"
LOG = print

MSR_PKG_POWER_LIMIT = 0x610
MSR_APERF = 0xE8
MSR_MPERF = 0xE7
MSR_TURBO_RATIO_LIMITS = 0x1AD

BASE_MHZ = 1500.0          # MPERF reference (CALIBRATED by calib_base_mhz())
                           # history: 2600 (TSC) was WRONG -> it produced "7085 MHz"
MSR_PLATFORM_INFO = 0xCE   # bits 15:8 = maximum efficiency ratio = MPERF reference
FUSED_MAX_MHZ = 4400.0     # MSR 0x1AD: 1-2 cores 44x; 3-8 cores 40x (4.0 GHz)
PL1_NEW_W = 18.0           # just above the granted ceiling (15 W); RAPL declares 28 W

NCPU = os.cpu_count()
results = {"phases": []}


# ---------------------------------------------------------------- MSR / energy
def msr_path(cpu):
    return "/dev/cpu/%d/msr" % cpu


def rd(cpu, off):
    try:
        with open(msr_path(cpu), "rb") as f:
            return int.from_bytes(os.pread(f.fileno(), 8, off), "little")
    except OSError as e:
        return "ERRO:%s" % e.errno


def wr(cpu, off, val):
    try:
        with open(msr_path(cpu), "r+b") as f:
            os.pwrite(f.fileno(), val.to_bytes(8, "little"), off)
        return None
    except OSError as e:
        return "errno=%s %s" % (e.errno, e.strerror)


def energy():
    try:
        return int(open(ENERGY).read().strip()), int(open(ENERGY_MAX).read().strip())
    except OSError as e:
        return None, None


def calib_base_mhz():
    """REAL MPERF reference = "maximum efficiency ratio" (MSR_PLATFORM_INFO bits 15:8).
    On this machine: 15 -> 1500 MHz. Measured 2026-09-24: APERF advanced at 2.777 GHz while
    MPERF advanced at 1.4966 GHz -> ratio 1.8559 x 1500 = 2778 MHz (matches turbostat).
    Assuming 2600 MHz (TSC) here was what produced the impossible 7085 MHz clock."""
    v = rd(0, MSR_PLATFORM_INFO)
    if isinstance(v, str):
        return BASE_MHZ
    rr = (v >> 8) & 0xFF
    return float(rr * 100) if 4 <= rr <= 60 else BASE_MHZ


def watts_run(nt, secs):
    """Runs the FMA for `secs` and returns (watts, gflops, turbostat_stats, aperf_info)."""
    e0, emax = energy()
    a0 = {c: (rd(c, MSR_APERF), rd(c, MSR_MPERF)) for c in range(NCPU)}
    pkgw, bzy, rows_sem_msr, n_rows = [], [], 0, 0

    ts = subprocess.Popen(
        ["/usr/bin/turbostat", "--quiet", "--interval", "10",
         "--show", "CPU,Core,Bzy_MHz,PkgWatt", "--", "sleep", str(secs)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    t0 = time.monotonic()
    load = subprocess.run([FLOPS, str(nt), str(secs)], capture_output=True, text=True)
    el = time.monotonic() - t0
    e1, _ = energy()
    a1 = {c: (rd(c, MSR_APERF), rd(c, MSR_MPERF)) for c in range(NCPU)}
    try:
        ts_out, ts_err = ts.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        ts.kill()
        ts_out, ts_err = ts.communicate()

    # --- turbostat: TOLERANT parsing + keeps the raw output
    # this turbostat version (2026.04.21) sends the DATA LINES to STDERR,
    # format "core(hex) cpu Bzy_MHz" (no header) and it may come without PkgWatt.
    # That is why: it joins stdout+stderr and accepts 3 OR 4 fields.
    for line in ((ts_out or "") + "\n" + (ts_err or "")).splitlines():
        f = line.split()
        if not f or set(line) <= set("=+- ") or "turbostat:" in line:
            continue
        n_rows += 1
        try:
            bzy.append(float(f[2]))          # 3rd field = Bzy_MHz
            if len(f) >= 4:
                pkgw.append(float(f[3]))
        except (ValueError, IndexError):
            pass
    ts_stats = {"linhas": n_rows, "linhas_sem_coluna_msr": rows_sem_msr,
                "bzy_n": len(bzy), "pkgw_n": len(pkgw),
                "bzy_medio": round(sum(bzy) / len(bzy), 0) if bzy else None,
                "pkgw_medio": round(sum(pkgw) / len(pkgw), 2) if pkgw else None,
                "stdout_cru": ts_out.strip().splitlines()[-8:],
                "stderr_cru": ts_err.strip().splitlines()[-6:]}

    # --- GFLOPS from the binary
    gf = 0.0
    tok = load.stdout.split()
    for i, t in enumerate(tok):
        if t == "GFLOPS" and i > 0:
            try:
                gf = float(tok[i - 1])
            except ValueError:
                pass

    # --- watts from energy_uj (modular delta; long window = steady state, not burst)
    w = 0.0
    if e0 and e1 is not None:
        d = e1 - e0
        if d < 0 and emax:
            d += emax
        w = (d / 1e6) / el

    # --- APERF/MPERF: RAW deltas per CPU + sanity check
    per_cpu, invalidos = [], 0
    for c in range(NCPU):
        if isinstance(a0[c][0], str) or isinstance(a1[c][0], str):
            continue
        da = a1[c][0] - a0[c][0]
        dm = a1[c][1] - a0[c][1]
        if dm <= 0 or da <= 0:
            continue
        mhz = BASE_MHZ * da / dm
        ok = mhz <= FUSED_MAX_MHZ
        if not ok:
            invalidos += 1
        per_cpu.append({"cpu": c, "d_aperf": da, "d_mperf": dm,
                        "ratio": round(da / dm, 4),
                        "mhz": round(mhz), "valido": ok})
    validos = [p["mhz"] for p in per_cpu if p["valido"]]
    aperf_info = {"por_cpu": per_cpu, "invalidos": invalidos,
                  "mhz_max_valido": max(validos) if validos else None,
                  "mhz_medio_valido": round(sum(validos) / len(validos)) if validos else None,
                  "mhz_de_gflops_por_core": round(gf * 1000.0 / (32.0 * max(1.0, nt / 2.0)))}

    return round(w, 2), round(gf, 1), ts_stats, aperf_info, round(el, 1)


# ------------------------------------------------------------------ main
def main():
    global BASE_MHZ
    if "--check" in sys.argv:
        LOG("== --check (no root needed, writes nothing) ==")
        for p in (FLOPS, FLOPS + "_avx2", ENERGY, ENERGY_MAX, "/dev/cpu/0/msr"):
            LOG("  %-45s %s" % (p, "OK" if os.path.exists(p) else "MISSING"))
        LOG("  npu (cpu_count) = %d   base=%.0f MHz   fused turbo=%.0f MHz" % (NCPU, BASE_MHZ, FUSED_MAX_MHZ))
        return 0

    BASE_MHZ = calib_base_mhz()
    LOG("== initial state ==")
    orig = rd(0, MSR_PKG_POWER_LIMIT)
    if isinstance(orig, str):
        LOG("  FAILED to read MSR 0x610 (%s) - no root? aborting without writing anything." % orig)
        return 2
    LOG("  MSR 0x610 original = 0x%016x" % orig)
    LOG("    PL1 = %.1f W | PL2 = %.1f W | LOCK(b63) = %d"
        % ((orig & 0x7FFF) / 8.0, ((orig >> 32) & 0x7FFF) / 8.0, (orig >> 63) & 1))
    LOG("  MSR 0x1AD (fused turbo) = 0x%016x" % (rd(0, MSR_TURBO_RATIO_LIMITS) if not isinstance(rd(0, MSR_TURBO_RATIO_LIMITS), str) else 0))
    LOG("  MPERF reference (PLATFORM_INFO[15:8] x 100) = %.0f MHz" % BASE_MHZ)
    LOG("  energy_uj = %s | max = %s" % energy())

    try:
        # phase 1: stock, 1 thread (single-core turbo; it is not power-limited)
        LOG("\n== phase 1: STOCK, 1 thread, 45 s ==", flush=True)
        w, gf, ts, ap, el = watts_run(1, 45)
        LOG("  %.2f W | %.1f GFLOPS | %.1f s" % (w, gf, el))
        LOG("  turbostat: %s" % json.dumps({k: v for k, v in ts.items() if not k.endswith("_cru")}))
        LOG("  turbostat raw stdout: %s" % ts["stdout_cru"])
        LOG("  turbostat raw stderr: %s" % ts["stderr_cru"])
        LOG("  APERF/MPERF cpu0..3: %s" % ap["por_cpu"][:4])
        LOG("  valid clock: max=%s avg=%s | invalid=%d" % (ap["mhz_max_valido"], ap["mhz_medio_valido"], ap["invalidos"]))
        results["phases"].append({"nome": "stock_1t", "threads": 1, "pl1_w": 15.0, "watts": w,
                                  "gflops": gf, "el_s": el, "turbostat": ts, "aperf": ap})

        # phase 2: stock, 8 threads (steady state at the factory ceiling)
        LOG("\n== phase 2: STOCK, 8 threads, 60 s ==", flush=True)
        w, gf, ts, ap, el = watts_run(min(8, NCPU), 60)
        LOG("  %.2f W | %.1f GFLOPS | %.1f s" % (w, gf, el))
        LOG("  turbostat: %s" % json.dumps({k: v for k, v in ts.items() if not k.endswith("_cru")}))
        LOG("  APERF/MPERF cpu0..3: %s" % ap["por_cpu"][:4])
        LOG("  valid clock: max=%s avg=%s | invalid=%d" % (ap["mhz_max_valido"], ap["mhz_medio_valido"], ap["invalidos"]))
        results["phases"].append({"nome": "stock_8t", "threads": min(8, NCPU), "pl1_w": 15.0, "watts": w,
                                  "gflops": gf, "el_s": el, "turbostat": ts, "aperf": ap})

        # phase 3: PL1 = 18 W (only the PL1 field; PL2 and tau untouched) + 8 threads
        LOG("\n== phase 3: PL1 = %.0f W in MSR 0x610, 8 threads, 60 s ==" % PL1_NEW_W, flush=True)
        novo = (orig & ~0x7FFF) | int(round(PL1_NEW_W * 8))
        LOG("  writing 0x%016x to cpu0 ..." % novo)
        err = wr(0, MSR_PKG_POWER_LIMIT, novo)
        rb = rd(0, MSR_PKG_POWER_LIMIT)
        LOG("  write: %s | readback = 0x%016x -> PL1 = %.1f W"
            % (err if err else "OK", rb, (rb & 0x7FFF) / 8.0))
        w, gf, ts, ap, el = watts_run(min(8, NCPU), 60)
        LOG("  %.2f W | %.1f GFLOPS | %.1f s" % (w, gf, el))
        LOG("  VERDICT phase 3: asked for %.0f W, measured %.2f W -> %s"
            % (PL1_NEW_W, w, "the clamp is NOT the register (the platform limits it)" if w < PL1_NEW_W - 1.0
               else "the envelope OPENED above 15 W (!)" ))
        results["phases"].append({"nome": "pl1_18w_8t", "threads": min(8, NCPU), "pl1_w": PL1_NEW_W,
                                  "write": err or "OK", "readback_pl1_w": (rb & 0x7FFF) / 8.0,
                                  "watts": w, "gflops": gf, "el_s": el, "turbostat": ts, "aperf": ap})
    finally:
        # restoration ALWAYS, with a confirmation readback
        LOG("\n== restoration ==")
        err = wr(0, MSR_PKG_POWER_LIMIT, orig)
        rb = rd(0, MSR_PKG_POWER_LIMIT)
        LOG("  restore 0x610: write %s | readback = 0x%016x | equal to the original: %s"
            % (err if err else "OK", rb if not isinstance(rb, str) else 0, rb == orig))
        with open(JSON_OUT, "w") as fh:
            json.dump(results, fh, indent=1)
        LOG("  JSON: %s" % JSON_OUT)

    LOG("\n== summary ==")
    for p in results["phases"]:
        LOG("  %-14s threads=%d pl1=%.0fW -> %6.2f W  %7.1f GFLOPS  clock(max valid)=%s"
            % (p["nome"], p["threads"], p["pl1_w"], p["watts"], p["gflops"], p["aperf"]["mhz_max_valido"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
