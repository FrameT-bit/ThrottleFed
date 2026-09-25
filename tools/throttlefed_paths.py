#!/usr/bin/python3
"""Hardware path discovery shared by the ThrottleFed tools.

Every path here is resolved at runtime instead of being frozen to a device
index: powercap zones are matched by the 'name' field the kernel reports,
thermal zones by their 'type', the cpufreq policy by the glob that exists, and
the iGPU GT directory in both layouts (i915: card*/gt/gt*, xe:
card*/device/tile*/gt*/). Fixed indices (card1/gt/gt0, thermal_zone0, policy0,
intel-rapl:0) were specific to the development laptop and made these tools
fail with FileNotFoundError - or worse, measure the wrong sensor - anywhere
else.

Every function returns a pathlib.Path or None and never raises: a missing
subsystem is the caller's decision, not a crash here. Import it from any script
living in this directory (the script's own directory is on sys.path):

    from throttlefed_paths import pkg_zone, core_zone, pkg_temp_zone, gt_dir
"""
from __future__ import annotations

import glob
from pathlib import Path

RAPL_ROOT = Path("/sys/class/powercap")
THERMAL_ROOT = Path("/sys/class/thermal")
CPU_ROOT = Path("/sys/devices/system/cpu")
CPUFREQ_ROOT = CPU_ROOT / "cpufreq"
DRM_ROOT = Path("/sys/class/drm")
PCI_ROOT = Path("/sys/bus/pci/devices")
MSR_ROOT = Path("/dev/cpu")
PLATFORM_PROFILE = Path("/sys/firmware/acpi/platform_profile")


def _read(path) -> str | None:
    """Stripped text of a sysfs file, or None if it is absent/unreadable."""
    try:
        return Path(path).read_text().strip()
    except Exception:
        return None


# --------------------------------------------------------------------------
# powercap / RAPL
# --------------------------------------------------------------------------
def rapl_zones(root=RAPL_ROOT):
    """Powercap zones that expose a 'name' (the intel-rapl parent has none)."""
    out = []
    for d in sorted(glob.glob(str(Path(root) / "intel-rapl*"))):
        p = Path(d)
        if (p / "name").exists():
            out.append(p)
    return out


def zone_by_name(*names, root=RAPL_ROOT):
    """First powercap zone whose 'name' matches one of names (case-folded)."""
    want = {n.lower() for n in names}
    for z in rapl_zones(root):
        if (_read(z / "name") or "").lower() in want:
            return z
    return None


def pkg_zone(root=RAPL_ROOT):
    """Package domain of the RAPL tree ('package-0', older code says 'package').

    The MMIO copy (intel-rapl-mmio:0) reports the same 'name' and sorts first, so
    it must be skipped: the constraints the tools write live on the MSR/powercap
    zone. The MMIO zone is only used when the kernel exposes nothing else.
    """
    for z in rapl_zones(root):
        if z.name.startswith("intel-rapl-mmio"):
            continue
        if (_read(z / "name") or "").lower() in ("package-0", "package"):
            return z
    return mmio_pkg_zone(root)


def core_zone(root=RAPL_ROOT):
    """PP0 / core subzone of the package ('core' on every modern Intel part)."""
    return zone_by_name("core", root=root)


def uncore_zone(root=RAPL_ROOT):
    """PP1 / uncore subzone of the package."""
    return zone_by_name("uncore", root=root)


def psys_zone(root=RAPL_ROOT):
    """Platform (psys) zone, whole-machine domain above the package."""
    return zone_by_name("psys", root=root)


def mmio_pkg_zone(root=RAPL_ROOT):
    """Package zone on the MMIO path (intel-rapl-mmio:*), same 'name' field."""
    for z in rapl_zones(root):
        if not z.name.startswith("intel-rapl-mmio"):
            continue
        if (_read(z / "name") or "").lower().startswith("package"):
            return z
    return None


def constraint(zone, n=0, field="power_limit_uw"):
    """constraint_<n>_<field> of a zone, or None (not every zone has every field)."""
    if not zone:
        return None
    return Path(zone) / f"constraint_{n}_{field}"


# --------------------------------------------------------------------------
# thermal
# --------------------------------------------------------------------------
def thermal_zones():
    return [Path(d) for d in sorted(glob.glob(str(THERMAL_ROOT / "thermal_zone*")))]


def thermal_zone(*types, contains=None):
    """Zone whose 'type' is one of types (exact, case-folded) or contains a string."""
    want = {t.lower() for t in types}
    for z in thermal_zones():
        t = (_read(z / "type") or "").lower()
        if not t:
            continue
        if t in want or (contains and contains in t):
            return z
    return None


def pkg_temp_zone():
    """CPU package sensor: 'x86_pkg_temp' on most Intel parts, 'pkg_temp' elsewhere.

    The index is NOT stable: the first zones are often DPTF/vendor sensors
    (z0 = INT3400 Thermal on the development laptop, whose temp is not the CPU).
    """
    return (thermal_zone("x86_pkg_temp", "pkg_temp", contains="pkg_temp")
            or thermal_zone("cpu_thermal", "soc_thermal", "cpu-thermal"))


def cooling_device(*types):
    """Cooling device matched by 'type' - never by index.

    cooling_device10 is the Dell SMM fan only on the development laptop; on
    other machines that index is typically a 'Processor' clock limiter, so
    writing to it throttles the CPU instead of moving the fan. Fan control only
    exists on some machines at all: this returns None when there is no match.
    """
    patterns = tuple(types) or ("dell-smm-fan", "fan")
    for d in sorted(glob.glob(str(THERMAL_ROOT / "cooling_device*"))):
        t = (_read(Path(d) / "type") or "").lower()
        if t and any(w in t for w in patterns):
            return Path(d)
    return None


# --------------------------------------------------------------------------
# cpufreq
# --------------------------------------------------------------------------
def policies():
    """Every cpufreq policy, numerically sorted (policy0 is not guaranteed)."""
    out = [Path(d) for d in glob.glob(str(CPUFREQ_ROOT / "policy*"))]

    def key(p):
        try:
            return int(p.name.rsplit("policy", 1)[1])
        except Exception:
            return 1 << 30

    return sorted(out, key=key)


def policy(n=0):
    ps = policies()
    return ps[n] if len(ps) > n else None


def epp_file():
    """energy_performance_preference of the first policy that has one."""
    for p in policies():
        f = p / "energy_performance_preference"
        if f.exists():
            return f
    legacy = CPU_ROOT / "cpu0/cpufreq/energy_performance_preference"
    return legacy if legacy.exists() else None


def epp_files():
    """Every per-policy EPP file (falls back to the per-cpu nodes)."""
    out = [p / "energy_performance_preference" for p in policies()
           if (p / "energy_performance_preference").exists()]
    if out:
        return out
    return [Path(d) for d in sorted(
        glob.glob(str(CPU_ROOT / "cpu*/cpufreq/energy_performance_preference")))]


def scaling_cur_freq(pol=None):
    """scaling_cur_freq of the first policy, or None."""
    for p in ([pol] if pol else policies()):
        f = p / "scaling_cur_freq"
        if f.exists():
            return f
    return None


# --------------------------------------------------------------------------
# iGPU / PCI
# --------------------------------------------------------------------------
def gt_dir():
    """iGPU GT directory: i915 (card*/gt/gt*) first, then xe (card*/device/tile*/gt*)."""
    cands = []
    for pat in ("card*/gt/gt*", "card*/device/tile*/gt*", "card*/gt"):
        cands += [Path(d) for d in sorted(glob.glob(str(DRM_ROOT / pat)))]
    for p in cands:
        if (p / "rps_min_freq_mhz").exists():
            return p
    return cands[0] if cands else None


def gt_file(name="rps_min_freq_mhz"):
    """A GT knob; on xe the same knobs live one level down (gt*/freq0/)."""
    g = gt_dir()
    if g is None:
        return None
    f = g / name
    if f.exists():
        return f
    for d in sorted(glob.glob(str(g / "freq*"))):
        f = Path(d) / name
        if f.exists():
            return f
    return f


def dptf_dir():
    """Intel DPTF / processor_thermal PCI function (firmware/EC channel).

    Discovered by the attributes it exposes (power_limits, workload_request,
    tcc_offset, fivr); the BDF 0000:00:04.0 is a platform address, not a
    constant across models/buses.
    """
    for d in sorted(glob.glob(str(PCI_ROOT / "*"))):
        p = Path(d)
        for probe in ("power_limits", "workload_request",
                      "tcc_offset_degree_celsius", "fivr"):
            if (p / probe).exists():
                return p
    return None


def dptf_file(relpath):
    """Path under the DPTF device, or None when the device is not there."""
    d = dptf_dir()
    return (d / relpath) if d is not None else None


# --------------------------------------------------------------------------
# MSR
# --------------------------------------------------------------------------
def msr(cpu=0):
    """MSR character device of a CPU (root-only; reads return EPERM otherwise)."""
    return MSR_ROOT / str(cpu) / "msr"


def energy_uj(zone):
    return (Path(zone) / "energy_uj") if zone else None


def max_energy_range_uj(zone):
    return (Path(zone) / "max_energy_range_uj") if zone else None


def file_int(path, default=0):
    """Integer content of a sysfs file, or default when it is absent/unreadable."""
    try:
        return int(Path(path).read_text().strip())
    except Exception:
        return default


def need(**what):
    """Raise SystemExit listing every path this machine does not expose.

    Tools that write to these paths call this once, before touching anything, so
    a machine without RAPL powercap (container, some AMD parts, iGPU disabled in
    firmware) says what is missing instead of failing later with an obscure
    FileNotFoundError halfway through a measurement.
    """
    missing = sorted(k for k, v in what.items() if v is None or not Path(v).exists())
    if missing:
        raise SystemExit("this machine does not expose: " + ", ".join(missing)
                         + " - RAPL powercap zones, the iGPU GT directory and the package"
                           " thermal zone are required for this measurement")
    return what


if __name__ == "__main__":
    # Self-check: print what this machine exposes. No writes, no root needed.
    print("powercap zones:")
    for z in rapl_zones():
        print(f"  {z}  name={_read(z / 'name')}")
    print("thermal zones:")
    for z in thermal_zones():
        print(f"  {z}  type={_read(z / 'type')}  temp={_read(z / 'temp')}")
    print(f"pkg={pkg_zone()} core={core_zone()} uncore={uncore_zone()}"
          f" psys={psys_zone()} mmio={mmio_pkg_zone()}")
    print(f"pkg_temp={pkg_temp_zone()} gt={gt_dir()} gt_file={gt_file()}")
    print(f"fan={cooling_device()} epp={epp_file()}")
    print(f"policies={[str(p) for p in policies()]}")
    print(f"dptf={dptf_dir()}")
