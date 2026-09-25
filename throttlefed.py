#!/usr/bin/env python3
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

"""throttlefed - the Linux equivalent of ThrottleStop.

Controls what ThrottleStop controls on Windows, through the Linux subsystems:
RAPL/powercap (PL1/PL2/PP0/PP1), direct MSR via /dev/cpu/*/msr (lock bits, HWP/EPP,
turbo caps), ACPI platform_profile (vendor thermal mode) and the i915 sysfs
(iGPU RPS/RC6).

Typical usage:
    sudo throttlefed.py probe                 # x-ray: what the firmware allows
    sudo throttlefed.py apply gpu --dry-run   # what would change
    sudo throttlefed.py apply gpu             # budget for the iGPU
    sudo throttlefed.py apply cpu-max         # budget for the CPU
    sudo throttlefed.py status
    sudo throttlefed.py watch                 # live monitor (W, freq, throttle)
    sudo throttlefed.py restore               # back to stock
    sudo throttlefed.py install               # systemd service+timer (persists)

State: /var/lib/throttlefed/  (stock.json, active.json)
Profiles: /etc/throttlefed/profiles.json (written on install)

No external dependencies. Python 3.9+.
"""

import argparse
import base64
import glob
import importlib.util
import json
import os
import shutil
import re
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

VERSION = "1.0.0"
IS_ROOT = os.geteuid() == 0

# Update channel. The check is one unauthenticated HTTPS GET to GitHub: no query
# string, no machine identifier, nothing about this machine leaves the box. The
# answer is cached under $XDG_CACHE_HOME for a day, so a launch that finds a fresh
# entry does not touch the network at all.
GITHUB_REPO = "FrameT-bit/ThrottleFed"
API = "https://api.github.com/repos/" + GITHUB_REPO
RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases"
UPDATE_CACHE = (Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
                / "throttlefed" / "update.json")
UPDATE_TTL = 24 * 3600
ERROR_TTL = 15 * 60

# ----------------------------------------------------------------------------
# caminhos
# ----------------------------------------------------------------------------
# Only literal paths belong here. Anything whose *name* is an enumeration result
# of the kernel or the firmware (RAPL zone number, DPTF PCI address, thermal zone
# index, DRM card number) is discovered by content in the "deteccao de hardware"
# section below: an index is not a contract, and reading or writing by index is
# silently wrong on a machine that enumerates in another order.
RAPL = Path("/sys/class/powercap")
DRM = Path("/sys/class/drm")
THERMAL = Path("/sys/class/thermal")
CPU_TOPOLOGY = Path("/sys/devices/system/cpu")
PCI_DEVICES = Path("/sys/bus/pci/devices")
FIRMWARE_ATTRS = Path("/sys/class/firmware-attributes")
PLATFORM_PROFILE = Path("/sys/firmware/acpi/platform_profile")
PLATFORM_PROFILE_CHOICES = Path("/sys/firmware/acpi/platform_profile_choices")

# One state directory for every caller, root or not. A second one under $HOME made
# 'sudo apply' and a plain 'status' disagree about what had been captured, while
# the timer (root) kept applying the other copy.
STATE_DIR = Path("/var/lib/throttlefed")
STOCK = STATE_DIR / "stock.json"
ACTIVE = STATE_DIR / "active.json"
SYSTEM_PROFILES = Path("/etc/throttlefed/profiles.json")
UNIT_DIR = Path("/etc/systemd/system")
SERVICE_NAME = "throttlefed.service"
TIMER_NAME = "throttlefed.timer"
# The systemd units call this fixed path, never the checkout path the installer
# happened to run from: a unit whose ExecStart points into a home directory fails
# with 203/EXEC on any other machine, and after the checkout is moved.
WRAPPER = Path("/usr/local/bin/throttlefed")
SELF = Path(__file__).resolve()
PLUGIN_DIRS = (SELF.parent / "plugins", Path("/usr/local/share/throttlefed/plugins"))
PLUGINS_DISABLED = False

# ----------------------------------------------------------------------------
# MSRs (Intel SDM / arch/x86/include/asm/msr-index.h)
# ----------------------------------------------------------------------------
MSR_RAPL_POWER_UNIT = 0x606    # 3:0 = power unit exponent (3 => 1/8 W)
MSR_PKG_POWER_LIMIT = 0x610    # 14:0 PL1 | 15 en | 16 clamp | 23:17 win
                               # 46:32 PL2 | 47 en | 48 clamp | 54:49 win | 63 lock
MSR_PKG_POWER_INFO = 0x614     # 14:0 TDP | 30:16 min | 46:32 max | 54:48 win max
MSR_PP0_POWER_LIMIT = 0x638    # core domain (IA)
MSR_PP1_POWER_LIMIT = 0x640    # uncore domain (ring/iGPU inside the package)
MSR_HWP_REQUEST = 0x774        # 7:0 min | 15:8 max | 23:16 desired | 31:24 EPP
MSR_PM_ENABLE = 0x770          # bit 0 = HWP enabled  (NOT HWP_CAPABILITIES!)
MSR_HWP_CAPABILITIES = 0x771   # 7:0 highest | 15:8 guaranteed | 23:16 efficient | 31:24 lowest
MSR_TEMPERATURE_TARGET = 0x1A2  # 23:16 = TjMax (C) | 29:24 = TCC offset (C)
MSR_POWER_CTL = 0x1FC          # bit 0 (widely reported as BD PROCHOT) - NOT confirmed for TGL
MSR_OC_MAILBOX = 0x150         # voltage offset interface (FIVR) - raw reporting only

# Bit-level MSR writes are generation specific: the same bit of the same register
# changes meaning between families, so a bit table is only valid for the CPU it
# was confirmed on. 0x1FC bit 0 is the case that matters: the Intel SDM documents
# it as C1E enable while tooling folklore calls it BD PROCHOT, so no entry exists
# for this laptop and the write path refuses unless the user forces it.
MSR_BIT_TABLES = {
    # "family/model": {MSR address: {bit: "documented meaning"}},
}

EPP_VALUES = {
    "performance": 0,
    "balance_performance": 128,
    "balance_power": 192,
    "power": 255,
}
# tuned sometimes uses "normal" (= balance_performance)
EPP_ALIASES = {"normal": 128, "default": 128}

PROFILE_TEMPLATES = {
    "balanced": {
        "_doc": "stock - nothing forced",
    },
    "cpu": {
        "_doc": "more CPU budget: high PL1/PL2, EPP performance",
        "pl1_w": 25.0, "pl2_w": 40.0, "pp0_w": None, "epp": "balance_performance",
        "gt_min_mhz": None, "platform_profile": None,
    },
    "cpu-max": {
        "_doc": "CPU max: PL1 28W (platform cTDP ceiling), EPP performance",
        "pl1_w": 28.0, "pl2_w": 44.0, "pp0_w": None, "epp": "performance",
        "gt_min_mhz": None, "platform_profile": "performance",
    },
    "gpu": {
        "_doc": "more iGPU budget: cap PP0 (cores) so they dont starve the package",
        "pl1_w": 15.0, "pl2_w": 35.0, "pp0_w": 9.0, "epp": "balance_power",
        "gt_min_mhz": 700, "platform_profile": None,
    },
    "gpu-max": {
        "_doc": "iGPU max: looser package + capped cores + high GT min",
        "pl1_w": 25.0, "pl2_w": 44.0, "pp0_w": 11.0, "epp": "balance_power",
        "gt_min_mhz": 900, "platform_profile": "performance",
    },
    "quiet": {
        "_doc": "quiet/cool: package capped, EPP power-save",
        "pl1_w": 10.0, "pl2_w": 20.0, "pp0_w": None, "epp": "power",
        "gt_min_mhz": None, "platform_profile": "quiet",
    },
    "burst": {
        "_doc": "sustained 8 W and quiet (short platform burst)",
        "pl1_w": 8.0, "pl2_w": 28.0, "pl2_win_us": 2_000_000, "pp0_w": None,
        "epp": "balance_power", "gt_min_mhz": None, "platform_profile": "quiet",
    },
}

# ----------------------------------------------------------------------------
# utils
# ----------------------------------------------------------------------------
class Color:
    on = sys.stdout.isatty()


def c(txt, code):
    return f"\033[{code}m{txt}\033[0m" if Color.on else txt


def red(t): return c(t, "31")
def green(t): return c(t, "32")
def yellow(t): return c(t, "33")
def blue(t): return c(t, "36")
def bold(t): return c(t, "1")
def dim(t): return c(t, "2")


def w(uw):
    """Watts as clean text (no padding) - format it in the print."""
    if uw is None:
        return "n/a"
    return f"{uw / 1_000_000:.2f} W"


def read(path, default=None):
    try:
        return Path(path).read_text().strip()
    except (OSError, ValueError):
        return default


def read_int(path, default=None):
    v = read(path)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except ValueError:
        return default


def read_why(path):
    """(value, why): the honest version of read(), for values that feed a decision.

    read()/read_int() keep returning a silent default because the plugins are
    built on that contract, but a caller that turns the result into a write must
    know whether the file was absent, refused, or empty: "absent" and "denied"
    are different facts and neither of them is a value.
    """
    if path is None:
        return None, "no path to read"
    try:
        return Path(path).read_text().strip(), ""
    except FileNotFoundError:
        return None, "absent"
    except PermissionError:
        return None, "permission denied (root-only)"
    except OSError as exc:
        return None, f"{type(exc).__name__}: {exc.strerror or exc}"


def read_int_why(path):
    """(int, why)  -  int(raw) with the reason it could not be read or parsed."""
    raw, why = read_why(path)
    if why:
        return None, why
    if raw == "":
        return None, "empty"
    try:
        return int(raw), ""
    except ValueError:
        return None, f"not an integer ({raw!r})"


def read_rel(domain, field):
    """(int, why) for a field inside a RAPL domain, saying when the domain is absent.

    This is how every snapshot and plan reads a zone: a missing domain must not
    look like a reading, and it must never fall back to another zone's file.
    """
    path = rapl_file(domain, field)
    if path is None:
        return None, f"no RAPL domain '{domain}' on this machine"
    return read_int_why(path)


def wstr(uw):
    """Microwatts as text, with '?' when the value never arrived.

    Every ceiling, readback and plan value is formatted through here: printing
    '0.00 W' for something that was never read is the kind of default that ends
    up in a write decision.
    """
    return "?" if uw is None else f"{uw / 1e6:.2f} W"


def resolve_target(target):
    """Plan target -> real sysfs path.

    @fwa:<vendor>:<attribute> is the firmware attribute channel (what the hardware
    owner uses on Windows). The name never comes from outside without passing here:
    only letters, digits, _ and -, which prevents the plugin from pointing anywhere.
    Any other target remains a literal path.
    """
    if isinstance(target, str) and target.startswith("@fwa:"):
        parts = target.split(":")
        if len(parts) != 3:
            raise ValueError(f"malformed firmware target: {target}")
        _, vendor, name = parts
        for tok in (vendor, name):
            if not tok or len(tok) > 64 or not all(c.isalnum() or c in "_-" for c in tok):
                raise ValueError(f"firmware target rejected: {target}")
        return Path("/sys/class/firmware-attributes") / vendor / "attributes" / name / "current_value"
    return Path(target)


def write_sysfs(path, value):
    """Write and VERIFY. Returns (ok, msg)."""
    try:
        p = resolve_target(path)
    except ValueError as exc:
        return False, str(exc)
    if not IS_ROOT:
        return False, "root required"
    if not p.exists():
        return False, "does not exist"
    if p.name == "current_value":
        possible = [v for v in (read(p.parent / "possible_values") or "").split(";") if v]
        if possible and str(value) not in possible:
            return False, f"value outside what the firmware accepts ({'/'.join(possible)})"
    try:
        p.write_text(str(value))
    except OSError as e:
        errno = e.errno
        if errno == 1:
            return False, "EPERM - locked by the firmware"
        if errno == 22:
            return False, "EINVAL - value outside the range"
        return False, f"{e.strerror} (errno {errno})"
    got = read(p)
    if got != str(value):
        return False, f"write ignored (value stayed {got})"
    return True, "ok"


# ----------------------------------------------------------------------------
# hardware detection (capability, never enumeration)
#
# Nothing below trusts an index published by the kernel. A zone number, a card
# number and a PCI address are enumeration results, so each one is read from the
# file that names the thing and re-checked against what is about to be done with
# it. When something is missing the caller is told why, never handed a default.
# ----------------------------------------------------------------------------
def _as_int(text):
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


# --- RAPL zones -------------------------------------------------------------
def _domain_key(name):
    """'package-0' -> 'package', 'core' -> 'core', 'dram' -> 'dram'."""
    if name.startswith("package"):
        _, _, idx = name.partition("-")
        return "package" if idx in ("", "0") else f"package-{idx}"
    return name


def rapl_domains():
    """({domain: Path}, [domains reachable only through the MMIO mirror]).

    The domain of a zone is what its 'name' file says ('package-0', 'core',
    'uncore', 'psys', 'dram'), never its number: a machine that enumerates the
    subzones in another order, or exposes 'dram' where this one exposes 'uncore',
    gets its budget written into the wrong constraint by an index-based table.
    The MMIO mirror of the same tree is used only for domains the MSR tree lacks.
    """
    msr_tree, mmio_tree = {}, {}
    for base in sorted(glob.glob(str(RAPL / "intel-rapl*"))):
        zone = Path(base)
        name = read(zone / "name")
        if not name:
            continue  # the controller directory has no 'name', so it is not a domain
        tree = mmio_tree if zone.name.startswith("intel-rapl-mmio") else msr_tree
        tree.setdefault(_domain_key(name), zone)
    domains = dict(msr_tree)
    mmio_only = []
    for key, zone in mmio_tree.items():
        if key not in domains:
            domains[key] = zone
            mmio_only.append(key)
    return domains, mmio_only


RAPL_DOMAINS, RAPL_MMIO_ONLY = rapl_domains()
RAPL_KEYS = ("package", "core", "uncore", "psys")
# kept as module names because the GUI and the tools import them (core.PKG);
# they are None on a machine without that domain, which the callers now report.
PKG = RAPL_DOMAINS.get("package")
CORE = RAPL_DOMAINS.get("core")
UNCORE = RAPL_DOMAINS.get("uncore")
PSYS = RAPL_DOMAINS.get("psys")
RAPL_MSR_TREE = [z for z in RAPL_DOMAINS.values() if not z.name.startswith("intel-rapl-mmio")]


def rapl_file(domain, field):
    """Path of a field inside a discovered domain, or None when it has no zone."""
    zone = RAPL_DOMAINS.get(domain)
    return None if zone is None else zone / field


def rapl_interface_note():
    """One line explaining what RAPL exposes here, or '' when it is the full tree.

    On some parts (and with the MSR interface locked by the firmware) only the
    MMIO mirror exists. Saying so matters: the limits still apply through it, but
    nothing written through it shows up in the MSR-based readback.
    """
    if not RAPL_DOMAINS:
        return "no RAPL zone under /sys/class/powercap (intel-rapl* absent)"
    if not RAPL_MSR_TREE:
        return "RAPL MSR interface not available (MMIO only)"
    if RAPL_MMIO_ONLY:
        return "RAPL: MMIO mirror also present for " + ", ".join(RAPL_MMIO_ONLY)
    return ""


# --- DPTF / proc_thermal ----------------------------------------------------
DPTF_ATTRS = ("power_limits", "tcc_offset_degree_celsius", "workload_request")


def dptf_dir():
    """The Intel DPTF device, found by what it publishes and not by its address.

    0000:00:04.0 is where this laptop puts it, which is not a fact about any
    other machine: what identifies the device is its driver (proc_thermal) and
    the 'power_limits' directory it exposes. A hardcoded BDF turns every read
    into a None that then looks like a reading.
    """
    for dev in sorted(glob.glob(str(PCI_DEVICES / "*"))):
        dev = Path(dev)
        try:
            module = (dev / "driver").resolve().name
        except OSError:
            module = ""
        if module == "proc_thermal" and (dev / "power_limits").exists():
            return dev
    return None


DPTF = dptf_dir()


def dptf_note():
    """One line saying where DPTF is, or that it is not there at all."""
    if DPTF is None:
        return ("DPTF/proc_thermal not present: no device under "
                "/sys/bus/pci/devices with the proc_thermal driver and 'power_limits'")
    return f"DPTF at {DPTF} (driver proc_thermal)"


def dptf_value(field):
    """Just the value of a DPTF attribute, or None (for the stock snapshot)."""
    return dptf_read(field)[0]


def dptf_read(field):
    """(value, why) from the DPTF device; absence is reported, never 'None'."""
    if DPTF is None:
        return None, "DPTF/proc_thermal device not present"
    return read_why(DPTF / field)


def dptf_int(field):
    if DPTF is None:
        return None, "DPTF/proc_thermal device not present"
    return read_int_why(DPTF / field)


# --- thermal zones ----------------------------------------------------------
# A zone index is the kernel's enumeration order, not a hardware fact: on this
# laptop zone3 is the SSD ('NGFF') and zone5 the WiFi card, and both were printed
# as CPU/package temperature, so the watchdog compared the CPU against the WiFi.
# The sensor is picked by its 'type'.
THERMAL_ROLES = (
    ("pkg", ("x86_pkg_temp", "coretemp", "TCPU", "CPU")),
    ("cpu", ("TCPU", "acpitz", "acpi")),
)


def thermal_zones():
    """{type: (path, index)} built from each zone's own 'type' file."""
    out = {}
    for zone in sorted(glob.glob(str(THERMAL / "thermal_zone*")),
                       key=lambda z: _as_int(z.rsplit("thermal_zone", 1)[1]) or 0):
        kind = read(Path(zone) / "type")
        if kind:
            out.setdefault(kind, (Path(zone), zone.rsplit("thermal_zone", 1)[1]))
    return out


def thermal_by_role():
    """[(role, type, path, temp_c, why)] chosen by type, never by index.

    A role with no matching type reports itself as such instead of printing
    whatever zone happened to sit in that slot.
    """
    zones = thermal_zones()
    chosen, used = [], set()
    for role, wanted in THERMAL_ROLES:
        pick = None
        for kind in wanted:
            hit = zones.get(kind)
            if hit is not None and hit[0] not in used:
                pick = (kind, *hit)
                break
        if pick is None:
            chosen.append((role, None, None, None, "no zone of type " + "/".join(wanted)))
            continue
        kind, path, _index = pick
        used.add(path)
        raw, why = read_int_why(path / "temp")
        chosen.append((role, kind, path, None if raw is None else raw / 1000.0, why))
    return chosen


def thermal_report():
    """The legend the monitor and the probe print: role=type(path)."""
    zones = thermal_zones()
    parts = []
    for role, kind, path, _temp, why in thermal_by_role():
        if kind is None:
            parts.append(f"{role}=n/a ({why}; zones found: {', '.join(zones) or 'none'})")
        else:
            parts.append(f"{role}={kind} ({path})")
    return "  ".join(parts)


# --- platform_profile -------------------------------------------------------
def platform_profile_choices():
    """The values this firmware accepts, read at runtime  -  [] when unknown.

    The list is not hardcoded: this ACPI platform publishes 'cool quiet balanced
    performance', while other firmware says 'quiet balanced performance_power'
    (a vendor vocabulary) or publishes nothing at all. When the attribute is
    absent argparse must not restrict the value: let the firmware refuse it.
    """
    raw = read(PLATFORM_PROFILE_CHOICES)
    return [] if not raw else raw.split()


def platform_profile_note():
    """One line: the current thermal mode and which values are accepted here."""
    cur = read(PLATFORM_PROFILE)
    choices = platform_profile_choices()
    if cur is None:
        return f"{PLATFORM_PROFILE} not present: no ACPI thermal mode switch on this machine"
    if not choices:
        return (f"{PLATFORM_PROFILE} = {cur}, but {PLATFORM_PROFILE_CHOICES} is absent: the "
                f"accepted values are unknown, the firmware is the one that will refuse")
    return f"{PLATFORM_PROFILE} = {cur}  (accepted on this machine: {', '.join(choices)})"


def cmd_thermal_choices(args):
    """Print the values '--thermal' accepts here, one per line (for completions)."""
    choices = platform_profile_choices()
    if not choices:
        print(yellow(f"{PLATFORM_PROFILE_CHOICES} absent on this machine: --thermal is not "
                     f"restricted, the firmware will refuse an unknown value"))
        return 1
    for choice in choices:
        print(choice)
    return 0


# --- iGPU GT ----------------------------------------------------------------
# i915 publishes card*/gt/gt*, xe publishes card*/device/tile*/gt*, and the card
# number is an enumeration detail (this laptop only has card1). A card is taken
# only when its device is Intel and it carries the RPS/RC6 knobs, so a discrete
# card can never win the glob race.
GT_PATTERNS = ("gt/gt*", "gt", "device/tile*/gt*", "device/tile*/gt",
               "device/gt/gt*", "device/gt/gt")
GT_FILES = ("rps_min_freq_mhz", "rps_max_freq_mhz", "rps_act_freq_mhz", "rc6_enable")


def gt_candidates():
    """[(card, gt_path, driver)] for every Intel GPU that exposes the RPS knobs."""
    out = []
    for card in sorted(glob.glob(str(DRM / "card[0-9]*"))):
        card = Path(card)
        vendor = read(card / "device" / "vendor") or ""
        try:
            if int(vendor, 16) != 0x8086:
                continue
        except ValueError:
            continue
        driver = ""
        for mod in (card / "device" / "driver" / "module").glob("drivers/*"):
            driver = mod.name
            break
        for pattern in GT_PATTERNS:
            hit = next((h for h in sorted(card.glob(pattern))
                        if any((h / f).exists() for f in GT_FILES)), None)
            if hit is not None:
                out.append((card, hit, driver))
                break
    return out


_GT_CANDIDATES = gt_candidates()
GT_CARD = _GT_CANDIDATES[0][0] if _GT_CANDIDATES else None
GT = _GT_CANDIDATES[0][1] if _GT_CANDIDATES else None
GT_DRIVER = _GT_CANDIDATES[0][2] if _GT_CANDIDATES else ""


def gpu_freq_range():
    """(lowest, highest) MHz the RPS knobs accept, read from the driver itself.

    gt_RPn_freq_mhz / gt_RP0_freq_mhz are the floor and the ceiling the driver
    publishes at the card level. A profile asking for a frequency outside them is
    an EINVAL at write time; better to clamp and say so.
    """
    if GT_CARD is None:
        return None, None
    low, _ = read_int_why(GT_CARD / "gt_RPn_freq_mhz")
    high, _ = read_int_why(GT_CARD / "gt_RP0_freq_mhz")
    if low is None or high is None or low > high:
        return None, None
    return low, high


# --- CPU packages -----------------------------------------------------------
def cpu_packages():
    """{physical_package_id: [cpu, ...]} from each CPU's topology attributes."""
    out = {}
    for path in glob.glob(str(CPU_TOPOLOGY / "cpu[0-9]*" / "topology" / "physical_package_id")):
        cpu = _as_int(path.split("/cpu", 1)[1].split("/", 1)[0])
        pid, _why = read_int_why(path)
        if cpu is None or pid is None:
            continue
        out.setdefault(pid, []).append(cpu)
    return dict(sorted((pid, sorted(cpus)) for pid, cpus in out.items()))


CPU_PACKAGES = cpu_packages()
# MSR state is per package, and every msr_read/msr_write defaults to this CPU so
# the register of package 0 is never read through the device of another socket.
MSR_CPU = CPU_PACKAGES.get(0, [0])[0]


def packages_note():
    """'' when there is one package, otherwise what is *not* being controlled."""
    if len(CPU_PACKAGES) <= 1:
        return ""
    where = ", ".join(f"package {p}: cpu{cs[0]}-cpu{cs[-1]}" for p, cs in CPU_PACKAGES.items())
    return (f"{len(CPU_PACKAGES)} physical packages ({where}): only package 0 is controlled "
            f"(MSR path /dev/cpu/{MSR_CPU}/msr, RAPL zone {RAPL_DOMAINS.get('package')}); "
            f"every other package keeps its own limits")


def systemd_available():
    """(ok, why)  -  persistence needs systemd, and a container does not have it."""
    if shutil.which("systemctl") is None:
        return False, "systemctl not found"
    if not Path("/run/systemd/system").is_dir():
        return False, "systemd is not the init system here (/run/systemd/system absent)"
    return True, ""


def systemctl(*verbs, timeout=15):
    """(ok, output)  -  every systemctl call is checked.

    'install' used to report success unconditionally while a failing 'enable'
    printed nothing: a unit that is not enabled is not persistence, and the
    person installing it has to hear that from the tool, not from the next boot.
    """
    if shutil.which("systemctl") is None:
        return False, "systemctl not found"
    try:
        proc = subprocess.run(["systemctl", *verbs], capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    out = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, out


def unit_state(name):
    """(enabled?, active?, output) as the system itself reports them."""
    enabled, e_out = systemctl("is-enabled", name)
    active, a_out = systemctl("is-active", name)
    return enabled, active, f"is-enabled: {e_out or '?'} / is-active: {a_out or '?'}"


def capabilities():
    """[(name, ok, detail)]  -  what the tool needs on *this* machine."""
    return [
        ("RAPL limits", bool(RAPL_DOMAINS), rapl_interface_note() or "intel-rapl zones found"),
        ("iGPU RPS", GT is not None, f"Intel GPU not found ({'no /sys/class/drm/card*' if not DRM.exists() else 'no RPS knobs'})" if GT is None else str(GT)),
        ("ACPI platform_profile", PLATFORM_PROFILE.exists(),
         str(PLATFORM_PROFILE) if PLATFORM_PROFILE.exists() else "absent"),
        ("DPTF/proc_thermal", DPTF is not None,
         str(DPTF) if DPTF is not None else "device not present"),
    ]


def gt_absent_why():
    """Why there is no GT to control, naming what was actually looked at."""
    if not DRM.exists():
        return "/sys/class/drm absent (no DRM driver loaded)"
    cards = sorted(glob.glob(str(DRM / "card[0-9]*")))
    if not cards:
        return "no /sys/class/drm/card*"
    seen = []
    for card in cards:
        vendor = read(Path(card) / "device" / "vendor") or "?"
        knobs = [p for p in GT_PATTERNS if list(Path(card).glob(p))]
        seen.append(f"{Path(card).name} (vendor {vendor}, {len(knobs)} GT path(s), "
                    f"{'no rps_*/rc6 knob' if not knobs else 'no rps_*/rc6 knob inside'})")
    return ("no Intel GPU with the RPS knobs (" + "; ".join(seen) +
            "); nothing to control here")


def cpu_identity():
    """(family, model, name) from /proc/cpuinfo  -  needed before writing any bit.

    A bit in an MSR means different things on different generations, so the
    family/model is what decides whether a bit table applies at all.
    """
    family, model, name = None, None, None
    for line in (read("/proc/cpuinfo", "") or "").splitlines():
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key == "cpu family" and family is None:
            family = value
        elif key == "model" and model is None:
            model = value
        elif key == "model name" and name is None:
            name = value
    return family, model, name


def cpu_generation():
    """'family/model'  -  the key of MSR_BIT_TABLES  -  plus the marketing name."""
    family, model, name = cpu_identity()
    return f"{family}/{model}", name


# ----------------------------------------------------------------------------
# MSR
# ----------------------------------------------------------------------------
def msr_read(addr, cpu=None):
    if not IS_ROOT:
        raise PermissionError("root required")
    dev = f"/dev/cpu/{MSR_CPU if cpu is None else cpu}/msr"
    if not os.path.exists(dev):
        raise FileNotFoundError(f"{dev} missing (modprobe msr)")
    fd = os.open(dev, os.O_RDONLY)
    try:
        return struct.unpack("<Q", os.pread(fd, 8, addr))[0]
    finally:
        os.close(fd)


def msr_write(addr, value, cpu=None):
    if not IS_ROOT:
        raise PermissionError("root required")
    dev = f"/dev/cpu/{MSR_CPU if cpu is None else cpu}/msr"
    fd = os.open(dev, os.O_WRONLY)
    try:
        os.pwrite(fd, struct.pack("<Q", value), addr)
    finally:
        os.close(fd)


def msr_try(addr, cpu=None):
    """(value, why)  -  one register per try.

    A register that the CPU does not implement (0x640 is absent on several
    parts) or that the firmware locks raises here; when a single try wrapped the
    whole MSR block, the first one that raised hid every register after it and
    the probe looked like a machine with no MSR support at all.
    """
    try:
        return msr_read(addr, cpu), ""
    except PermissionError as exc:
        return None, f"{exc}"
    except FileNotFoundError as exc:
        return None, f"{exc} (modprobe msr)"
    except OSError as exc:
        return None, f"{type(exc).__name__}: {exc.strerror or exc}"


_POWER_UNIT_WHY = "not read yet"


def power_unit():
    """Watts per LSB of the RAPL fields, or None when it cannot be known.

    The old fallback returned 0.125 as if it had been measured: without root, or
    on a part whose 0x606 exponent is not 3, every watt decoded from an MSR was
    off by a factor and the probe printed it next to real readings. Unknown is
    reportable; a made-up constant is not.
    """
    global _POWER_UNIT_WHY
    value, why = msr_try(MSR_RAPL_POWER_UNIT)
    if why:
        _POWER_UNIT_WHY = why
        return None
    exp = value & 0xF
    if exp > 8:
        _POWER_UNIT_WHY = f"exponent {exp} out of range"
        return None
    _POWER_UNIT_WHY = ""
    return 1.0 / (1 << exp)


def power_unit_why():
    return _POWER_UNIT_WHY


def unit_crosscheck(unit):
    """Text when the MSR decode disagrees with sysfs, else ''.

    Both numbers describe the same PL1 in different units, so a ratio far from 1
    means the exponent read from 0x606 (or assumed for this part) does not match
    the machine and every watt decoded from an MSR is suspect.
    """
    if unit is None:
        return ""
    value, why = msr_try(MSR_PKG_POWER_LIMIT)
    if why:
        return ""
    sysfs, sysfs_why = read_rel("package", "constraint_0_power_limit_uw")
    if sysfs_why or not sysfs:
        return ""
    raw = ((value >> 0) & 0x7FFF) * unit
    ratio = raw / sysfs
    if 0.95 <= ratio <= 1.05:
        return (f"0x606 cross-check: MSR PL1 {raw / 1e6:.2f} W vs sysfs {sysfs / 1e6:.2f} W "
                f"(ratio {ratio:.3f})  -  matches")
    return (f"0x606 cross-check: MSR PL1 {raw / 1e6:.2f} W vs sysfs {sysfs / 1e6:.2f} W "
            f"(ratio {ratio:.3f})  -  MISMATCH, the power unit does not describe this machine, "
            f"so no MSR watt above is trustworthy")


def decode_pkg_power_limit(v, unit):
    """Decodes only what was validated empirically against sysfs
    (bits 14:0 and 46:32 match constraint_*_power_limit_uw exactly).
    Window fields (23:17 / 54:49) are NOT decoded: on this machine they give
    invalid values, so the raw is reported instead of inventing a number."""
    return {
        "pl1_w": ((v >> 0) & 0x7FFF) * unit,
        "pl1_en": (v >> 15) & 1,
        "pl1_clamp": (v >> 16) & 1,
        "pl1_win_raw": (v >> 17) & 0x7F,
        "pl2_w": ((v >> 32) & 0x7FFF) * unit,
        "pl2_en": (v >> 47) & 1,
        "pl2_clamp": (v >> 48) & 1,
        "pl2_win_raw": (v >> 49) & 0x3F,
        "lock": (v >> 63) & 1,
    }


def decode_power_info(v, unit):
    return {
        "tdp_w": ((v >> 0) & 0x7FFF) * unit,
        "min_w": ((v >> 16) & 0x7FFF) * unit,
        "max_w": ((v >> 32) & 0x7FFF) * unit,
        "max_win_raw": (v >> 48) & 0x3F,
    }


def decode_pp_limit(v, unit):
    return {
        "limit_w": ((v >> 0) & 0x7FFF) * unit,
        "en": (v >> 15) & 1,
        "clamp": (v >> 16) & 1,
        "lock": (v >> 63) & 1,
    }


def msr_hex(v):
    return f"0x{v:016X}"


# ----------------------------------------------------------------------------
# iGPU
# ----------------------------------------------------------------------------
# GT / GT_CARD / GT_DRIVER come from the "hardware detection" section: the GT is
# looked for under both drivers (i915 card*/gt/gt*, xe card*/device/tile*/gt*) and
# only on a card whose device is Intel and that exposes the RPS knobs, never on
# the first card in alphabetical order (a machine may expose only card1).
GPU_THROTTLE_BITS = [
    ("throttle_reason_pl1", "package PL1 throttling the iGPU"),
    ("throttle_reason_pl2", "PL2 bursting above its limit"),
    ("throttle_reason_pl4", "PL4 / VR current limit"),
    ("throttle_reason_prochot", "external PROCHOT (BD PROCHOT / weak charger)"),
    ("throttle_reason_thermal", "thermal limit"),
    ("throttle_reason_ratl", "RATL"),
    ("throttle_reason_vr_tdc", "VR TDC"),
    ("throttle_reason_vr_thermalert", "VR thermal alert"),
]


def gpu_snapshot():
    """RPS/RC6 state of the discovered GT, with the reason for every gap."""
    if GT is None:
        return {"present": False, "why": {"gt": gt_absent_why()}}
    snap = {"present": True, "path": str(GT), "card": str(GT_CARD), "driver": GT_DRIVER,
            "why": {}}
    for field, key in (("rps_act_freq_mhz", "act_mhz"), ("rps_cur_freq_mhz", "cur_mhz"),
                       ("rps_min_freq_mhz", "min_mhz"), ("rps_max_freq_mhz", "max_mhz"),
                       ("rps_boost_freq_mhz", "boost_mhz"), ("rc6_enable", "rc6"),
                       ("rc6_residency_ms", "rc6_ms")):
        value, why = read_int_why(GT / field)
        snap[key] = value
        if why:
            snap["why"][field] = why
    snap["throttle"] = {}
    for field, _label in GPU_THROTTLE_BITS:
        value, why = read_int_why(GT / field)
        snap["throttle"][field] = -1 if value is None else value
        if why:
            snap["why"][field] = why
    return snap


# ----------------------------------------------------------------------------
# RAPL
# ----------------------------------------------------------------------------
RAPL_SNAPSHOT_FIELDS = ("constraint_0_power_limit_uw", "constraint_1_power_limit_uw",
                        "constraint_2_power_limit_uw", "constraint_0_time_window_us",
                        "constraint_1_time_window_us", "constraint_2_time_window_us",
                        "enabled", "energy_uj")


def rapl_snapshot():
    """Per-domain RAPL state built from the discovered zones.

    Every key is always present, and 'present' says whether the domain has a zone
    at all, so a missing domain is reported as missing instead of as a None that
    reads like a value. The old snapshot knew four literals (intel-rapl:0, :0:0,
    :0:1, :1); on a machine that numbers them another way it would have read one
    domain's constraint as if it were 'core'.
    """
    snap = {}
    keys = list(RAPL_KEYS) + [k for k in sorted(RAPL_DOMAINS) if k not in RAPL_KEYS]
    for key in keys:
        zone = RAPL_DOMAINS.get(key)
        entry = {"present": zone is not None, "path": str(zone) if zone else None, "why": {}}
        entry["name"] = read(zone / "name") if zone else None
        for field in RAPL_SNAPSHOT_FIELDS:
            if zone is None:
                entry[field] = None
                continue
            entry[field], why = read_int_why(zone / field)
            if why:
                entry["why"][field] = why
        entry["pl1_uw"] = entry["constraint_0_power_limit_uw"]
        entry["pl2_uw"] = entry["constraint_1_power_limit_uw"]
        entry["peak_uw"] = entry["constraint_2_power_limit_uw"]
        entry["limit_uw"] = entry["constraint_0_power_limit_uw"]
        entry["pl1_win_us"] = entry["constraint_0_time_window_us"]
        entry["pl2_win_us"] = entry["constraint_1_time_window_us"]
        if zone is None:
            entry["why"]["zone"] = f"no RAPL domain '{key}' on this machine"
        snap[key] = entry
    return snap


def rapl_uw():
    """Accumulated package energy (uj) - root only."""
    return read_rel("package", "energy_uj")[0]


# --- what the firmware says it will take ------------------------------------
def firmware_pl_caps():
    """{0: (uw, source), 1: (uw, source)}  -  the maximum each limit accepts.

    Three independent sources, because each of them is blind on some machine: the
    RAPL constraint*_max_power_uw attributes, the DPTF power_limits ranges, and
    MSR 0x614 max_w (root only). The ceiling is the largest one reported: a
    literal 60 W does not protect a 6 W machine and rejects a legitimate value on
    a 55 W one.
    """
    caps = {}
    zone = RAPL_DOMAINS.get("package")
    for idx in (0, 1):
        field = f"constraint_{idx}_max_power_uw"
        value, _why = read_rel("package", field)
        if value:
            caps[idx] = (value, f"powercap {zone / field}" if zone else f"powercap {field}")
        dptf, _why = dptf_int(f"power_limits/power_limit_{idx}_max_uw")
        if dptf and (idx not in caps or dptf > caps[idx][0]):
            caps[idx] = (dptf, f"DPTF {DPTF}/power_limits/power_limit_{idx}_max_uw")
    unit = power_unit()
    if unit is not None:
        info, why = msr_try(MSR_PKG_POWER_INFO)
        if not why:
            max_uw = int(decode_power_info(info, unit)["max_w"] * 1_000_000)
            if max_uw > 0 and (0 not in caps or max_uw > caps[0][0]):
                caps[0] = (max_uw, f"MSR 0x614 max_w ({max_uw / 1e6:.2f} W)")
    return caps


def caps_note():
    """The ceiling in use, named with its source, for the probe and the plan."""
    caps = firmware_pl_caps()
    parts = [f"PL{idx + 1} max {uw / 1e6:.2f} W ({src})" for idx, (uw, src) in sorted(caps.items())]
    prof = read(PLATFORM_PROFILE)
    if prof:
        parts.append(f"platform_profile={prof}")
    if not parts:
        return ("no firmware-reported maximum is available here: nothing will be clamped, "
                "and nothing will protect the machine either")
    return "; ".join(parts)


def clamp_line(label, value_uw, cap_uw, source, forced=False):
    """(value_uw, note)  -  never write a limit the firmware never reported.

    Above the reported maximum the value is clamped unless --force was given, and
    either way it is said out loud: silently writing less (or more) than what was
    asked is how a profile stops meaning what its name says.
    """
    if value_uw is None or cap_uw is None or value_uw <= cap_uw:
        return value_uw, ""
    if forced:
        return value_uw, (f"{label}: {value_uw / 1e6:.2f} W is above what the firmware reports "
                          f"({cap_uw / 1e6:.2f} W from {source})  -  forced; the firmware may take "
                          f"it back without warning")
    return cap_uw, (f"{label}: {value_uw / 1e6:.2f} W is above what the firmware reports "
                    f"({cap_uw / 1e6:.2f} W from {source})  -  clamped to the reported maximum "
                    f"(use --force to write the higher value anyway)")


def clamp_mhz(label, value_mhz, low, high, forced=False):
    """(value_mhz, note) for a frequency outside the range the driver publishes."""
    if value_mhz is None or low is None or high is None:
        return value_mhz, ""
    if low <= value_mhz <= high:
        return value_mhz, ""
    if forced:
        return value_mhz, (f"{label}: {value_mhz} MHz is outside the range the driver publishes "
                           f"({low}-{high} MHz)  -  forced")
    fixed = min(max(value_mhz, low), high)
    return fixed, (f"{label}: {value_mhz} MHz is outside the range the driver publishes "
                   f"({low}-{high} MHz)  -  clamped to {fixed} MHz")


# ----------------------------------------------------------------------------
# plugins (what belongs to one vendor, not to the platform)
#
# The core covers what Intel and ACPI give on any machine: RAPL, HWP/EPP, turbo,
# i915 RPS, platform_profile. What belongs to ONE vendor lives in plugins/,
# and a plugin never imports the core: it receives the readers from here, so it
# cannot invent any privileged path of its own.
# ----------------------------------------------------------------------------
_PLUGINS = None
_PLUGIN_ERRORS = []


def _load_plugin_api():
    for d in PLUGIN_DIRS:
        api = Path(d) / "base.py"
        if not api.is_file():
            continue
        spec = importlib.util.spec_from_file_location("throttlefed_plugin_api", api)
        if spec is None or spec.loader is None:
            continue
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod
    return None


def plugins(reload=False):
    global _PLUGINS, _PLUGIN_ERRORS
    if PLUGINS_DISABLED:
        return []
    if _PLUGINS is not None and not reload:
        return _PLUGINS
    api = _load_plugin_api()
    if api is None:
        _PLUGINS, _PLUGIN_ERRORS = [], []
        return _PLUGINS
    ctx = api.Context(read=read, read_int=read_int, write_sysfs=write_sysfs,
                      plan_line=None, root=SELF.parent,
                      verbose=bool(os.environ.get("THROTTLEFED_DEBUG")))
    _PLUGINS, _PLUGIN_ERRORS = api.load(ctx, PLUGIN_DIRS)
    return _PLUGINS


def plugin_channels():
    out = []
    for plug in plugins():
        for ch in plug.channels():
            out.append((plug, ch))
    return out


def plugin_warnings():
    out = []
    for plug in plugins():
        out.extend(plug.warnings())
    return out


def vendor_thermal_plan(mode):
    """The same thermal mode may have a firmware channel that is not ACPI.

    On a vendor machine the mode is usually written in two places: platform_profile
    (runtime, owned by the OS) and the firmware attribute (state that survives a
    reboot and may apply before the kernel boots). The plan shows both, and a
    channel only enters if the firmware declares that value.
    """
    out = []
    for plug, ch in plugin_channels():
        if ch.role != "thermal":
            continue
        value = plug.thermal_map().get(str(mode))
        if not value or (ch.values and value not in ch.values):
            continue
        try:
            old = read(resolve_target(ch.target)) or "?"
        except ValueError:
            old = "?"
        out.append((f"{plug.ID.capitalize()} {ch.label}", ch.target, value, old, value))
    return out


def print_plugin_sections():
    """Read blocks a plugin contributes. A broken plugin still shows up."""
    for name, err in _PLUGIN_ERRORS:
        print(red(f"  [plugin] {name} did not load: {err}"))
    for plug in plugins():
        for section in plug.sections():
            print(bold(f"  {section.title}"))
            for label, value in section.rows:
                print(f"    {label:28s} {value}")
            if section.note:
                print(dim(f"    note: {section.note}"))


# ----------------------------------------------------------------------------
# competidores (tuned / tuned-ppd / thermald / power-profiles-daemon / lpmd)
# ----------------------------------------------------------------------------
# Not "the names the author happened to know": Fedora 44 ships tuned-ppd, which is
# what the GNOME power panel drives, and a limit written through it looks exactly
# like one written by hand. Units are listed only after they are found on this
# machine, so none is invented and none installed is missed.
COMPETITOR_UNITS = (
    "tuned",
    "tuned-ppd",
    "thermald",
    "power-profiles-daemon",
    "intel_lpmd",
)
# What each one rewrites, so the warning says what it is competing with.
COMPETITOR_EFFECT = {
    "tuned": "rewrites EPP and the power limits",
    "tuned-ppd": "D-Bus front end of tuned (the GNOME power panel writes through it)",
    "thermald": "rewrites PL1/PL2 and the TCC offset",
    "power-profiles-daemon": "rewrites platform_profile and EPP",
    "intel_lpmd": "rewrites EPP and the LP mode",
}


def unit_files():
    """{unit name: path} for the systemd units installed in the usual places."""
    found = {}
    for directory in ("/usr/lib/systemd/system", "/etc/systemd/system",
                      "/run/systemd/system", "/lib/systemd/system"):
        for path in sorted(glob.glob(f"{directory}/*.service")):
            found.setdefault(Path(path).stem, Path(path))
    return found


def svc_state(name):
    """(state, why)  -  'unknown' is not a state, it is a failure to ask."""
    available, why = systemd_available()
    if not available:
        return "n/a", why
    try:
        proc = subprocess.run(["systemctl", "is-active", name], capture_output=True,
                              text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        return "unknown", f"{type(exc).__name__}: {exc}"
    state = proc.stdout.strip() or "unknown"
    if state == "unknown":
        return state, f"systemctl is-active said nothing (exit {proc.returncode})"
    return state, ""


def competitors():
    """{unit: state} for the units that exist here  -  tuned-ppd included."""
    units = unit_files()
    names = [name for name in COMPETITOR_UNITS if name in units]
    return {name: svc_state(name)[0] for name in names}


def competitors_detail():
    """[(unit, state, why)]  -  the same list, holding on to the reason it could not answer."""
    return [(name, *svc_state(name)) for name in competitors()]


def active_competitors():
    """The ones running right now: each of them beats the timer to every knob."""
    return [name for name, state, _why in competitors_detail() if state == "active"]


def tuned_profile():
    """tuned's active profile, or n/a with the reason tuned-adm could not answer."""
    if shutil.which("tuned-adm") is None:
        return "n/a (tuned-adm not installed)"
    try:
        proc = subprocess.run(["tuned-adm", "active"], capture_output=True, text=True,
                              timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"n/a ({type(exc).__name__}: {exc})"
    out = proc.stdout.strip()
    if not out:
        return f"n/a (tuned-adm exited {proc.returncode})"
    return out.split(":", 1)[1].strip() if ":" in out else out


# ----------------------------------------------------------------------------
# estado
# ----------------------------------------------------------------------------
def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def load_state(path):
    """(data, note)  -  the note says why there is no data instead of pretending.

    Reading the single state directory as a normal user is expected to fail with
    EACCES, and the caller has to be able to say so: "nothing was captured here"
    and "your user cannot see what was captured" call for different actions.
    """
    try:
        return json.loads(Path(path).read_text()), ""
    except FileNotFoundError:
        return None, f"{path} does not exist"
    except PermissionError:
        return None, f"{path} is root-only (run with sudo to read it)"
    except (OSError, ValueError) as exc:
        return None, f"{path}: {type(exc).__name__}: {exc}"


def state_note():
    """One line summarizing the state directory contents for 'status'."""
    if not STATE_DIR.exists():
        return f"{STATE_DIR} does not exist - no stock captured yet"
    stock = STOCK.exists()
    active = ACTIVE.exists()
    parts = []
    if stock:
        parts.append("stock.json present")
    else:
        parts.append("stock.json missing")
    if active:
        parts.append("active.json present")
    else:
        parts.append("active.json missing")
    return f"{STATE_DIR}: {', '.join(parts)}"


def save_json(path, data):
    """Write state and FAIL LOUDLY: the old silent success reported a stock that
    was never captured when the directory was not writable."""
    parent = Path(path).parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SystemExit(red(f"cannot create the state directory {parent}: {exc} "
                             f"(run as root: there is one state directory for every user)"))
    try:
        Path(path).write_text(json.dumps(data, indent=2))
    except OSError as exc:
        raise SystemExit(red(f"cannot write {path}: {exc} (run as root)"))
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass


def capture_stock():
    """Everything 'restore' needs, plus the zones it was captured from.

    The RAPL domain paths are recorded so that a restore can notice when the
    numbering under it changed (another kernel, or the same laptop after a BIOS
    reset): domains are matched by name at restore time and the path is checked
    against what the snapshot saw.
    """
    cap = gpu_snapshot()
    st = {
        "ts": time.time(),
        "rapl": rapl_snapshot(),
        "rapl_domains": {k: str(v) for k, v in RAPL_DOMAINS.items()},
        "platform_profile": read(PLATFORM_PROFILE),
        "epp": {f"policy{i}": read(f"/sys/devices/system/cpu/cpufreq/policy{i}/energy_performance_preference")
                for i in range(n_policies())},
        "gpu": {k: v for k, v in cap.items() if k != "throttle"},
        "tcc_offset": dptf_value("tcc_offset_degree_celsius"),
        "workload_type": dptf_value("workload_request/workload_type"),
        "plugins": {p.ID: p.capture() for p in plugins()},
    }
    return st


def n_policies():
    return len(glob.glob("/sys/devices/system/cpu/cpufreq/policy*"))


def policies():
    return sorted(glob.glob("/sys/devices/system/cpu/cpufreq/policy*"), key=lambda p: int(p.rsplit("policy", 1)[1]))


def set_epp(value):
    results = []
    for pol in policies():
        f = Path(pol) / "energy_performance_preference"
        avail = (read(Path(pol) / "energy_performance_available_preferences") or "").split()
        if avail and value not in avail:
            results.append((f, False, f"invalid value (accepts: {' '.join(avail)})"))
            continue
        results.append((f, *write_sysfs(f, value)))
    return results


def get_epp():
    vals = {read(f"{p}/energy_performance_preference") for p in policies()}
    if len(vals) == 1:
        return vals.pop()
    return f"mixed={sorted(vals)}"


# ----------------------------------------------------------------------------
# PROBE
# ----------------------------------------------------------------------------
def cmd_probe(args):
    print(bold(f"\nthrottlefed {VERSION} - power control x-ray"))
    print(dim(f"root: {'yes' if IS_ROOT else 'NO (blocked values flagged below)'}  |  kernel: {os.uname().release}\n"))

    # --- hardware
    cpu = "?"
    for line in read("/proc/cpuinfo", "").splitlines():
        if line.startswith("model name"):
            cpu = line.split(":", 1)[1].strip()
            break
    print(bold("== HARDWARE =="))
    print(f"  CPU              : {cpu}")
    print(f"  System           : {read('/sys/class/dmi/id/sys_vendor')} {read('/sys/class/dmi/id/product_name')}"
          f"  BIOS {read('/sys/class/dmi/id/bios_version')}")
    print(f"  GT (iGPU)        : {GT if GT else 'not found'}")
    print(f"  scaling_driver   : {read('/sys/devices/system/cpu/cpufreq/policy0/scaling_driver')}")
    print(f"  governor         : {read('/sys/devices/system/cpu/cpufreq/policy0/scaling_governor')}")

    # --- RAPL
    s = rapl_snapshot()
    print(bold("\n== RAPL / powercap  (the ThrottleStop TPL) =="))
    if rapl_interface_note():
        print(yellow(f"  {rapl_interface_note()}"))
    if RAPL_DOMAINS:
        print(dim("  domains matched by 'name' (never by index): " +
                  ", ".join(f"{k}={v}" for k, v in sorted(RAPL_DOMAINS.items()))))
    else:
        print(yellow("  no intel-rapl* zone under /sys/class/powercap"))
    p = s["package"]
    if not p["present"]:
        print(yellow(f"  PL1/PL2: {p['why'].get('zone', 'package domain not found')}"))
    else:
        print(f"  PL1 long_term  : {w(p['pl1_uw'])}   window {((p['pl1_win_us'] or 0)/1e6):.2f} s   "
              f"{dim(p['path'] + '/constraint_0_power_limit_uw')}")
        print(f"  PL2 short_term : {w(p['pl2_uw'])}")
        print(f"  peak_power     : {w(p['peak_uw'])}")
        print(f"  package enabled: {p['enabled']}")
        for field, why in sorted(p["why"].items()):
            print(dim(f"      ? {field}: {why}"))
    for key, label in (("core", "PP0 core  "), ("uncore", "PP1 uncore"), ("psys", "psys (plat)")):
        d = s[key]
        if not d["present"]:
            print(f"  {label}     : {yellow('n/a')} {dim('- ' + d['why'].get('zone', 'not found'))}")
            continue
        tag = green("on") if d["enabled"] else dim("off")
        print(f"  {label}     : {tag}  PL1 {w(d['pl1_uw'])} / PL2 {w(d['pl2_uw'])}")
        for field, why in sorted(d["why"].items()):
            print(dim(f"      ? {field}: {why}"))

    if DPTF is None:
        print(dim(f"\n  DPTF (proc_thermal): {dptf_note()}"))
    else:
        print(dim(f"\n  DPTF (proc_thermal) at {DPTF} - what the firmware SAYS it accepts:"))
        for idx in (0, 1):
            parts = []
            for name, tag in (("min_uw", "min"), ("max_uw", "max"), ("step_uw", "step")):
                value, why = dptf_int(f"power_limits/power_limit_{idx}_{name}")
                parts.append(f"{tag} {w(value)}" if value is not None else
                             f"{tag} {yellow('?')} ({why})")
            win, _why = dptf_int(f"power_limits/power_limit_{idx}_tmax_us")
            parts.append(f"max window {((win or 0) / 1e6):.1f}s")
            print(dim(f"    PL{idx + 1} " + "  ".join(parts)))

    # --- MSR
    print(bold("\n== Direct MSR (/dev/cpu/*/msr)  (what the firmware really allows) =="))
    gen_key, gen_name = cpu_generation()
    print(f"  CPU pkg 0        : /dev/cpu/{MSR_CPU}/msr   {dim(packages_note() or 'one physical package')}")
    print(f"  generation       : {gen_name}  (family/model {gen_key})")
    if gen_key not in MSR_BIT_TABLES:
        print(yellow(f"  >> no bit table for {gen_key}: every bit-level MSR write refuses unless "
                     f"--force is given (a bit that means one thing here means another on the "
                     f"generation the table was written on)"))
    unit = power_unit()
    if unit is None:
        print(yellow(f"  power unit (0x606) unreadable: {power_unit_why()} - the watts below "
                     f"are omitted instead of decoded with a guessed step"))
    else:
        print(f"  power unit (0x606): {unit} W  (TPL step)")
    if not IS_ROOT:
        print(yellow("  >> without root: lock bits and raws hidden. run with sudo."))
    else:
        # One try per register: 0x640 is missing on several parts and used to abort
        # the whole block, hiding every register after it.
        def msr_show(addr, label, fmt):
            value, why = msr_try(addr)
            if why:
                print(dim(f"  {label} = {yellow('unreadable')} ({why})"))
                return None
            print(fmt(value))
            return value

        v = msr_show(MSR_PKG_POWER_LIMIT, "0x610 PKG_POWER_LIMIT",
                     lambda val: f"  0x610 PKG_POWER_LIMIT = {msr_hex(val)}")
        if v is not None and unit is None:
            print(dim("        (raw only: no power unit to decode it with)"))
        elif v is not None:
            d = decode_pkg_power_limit(v, unit)
            print(f"        PL1 {w(int(d['pl1_w']*1e6))} en={d['pl1_en']} clamp={d['pl1_clamp']}  (win raw={d['pl1_win_raw']})")
            print(f"        PL2 {w(int(d['pl2_w']*1e6))} en={d['pl2_en']} clamp={d['pl2_clamp']}  (win raw={d['pl2_win_raw']})")
            lock = d["lock"]
            print(f"        LOCK(bit 63) = {red('1 - BIOS LOCKED PL1/PL2') if lock else green('0 - unlocked, writable')}")
            ok1 = abs(d["pl1_w"] * 1e6 - (p["pl1_uw"] or 0)) < 100_000
            ok2 = abs(d["pl2_w"] * 1e6 - (p["pl2_uw"] or 0)) < 100_000
            print(dim(f"        cross-check against sysfs: PL1 {'OK' if ok1 else 'MISMATCH'} / "
                      f"PL2 {'OK' if ok2 else 'MISMATCH'}"))
        info = msr_show(MSR_PKG_POWER_INFO, "0x614 PKG_POWER_INFO",
                        lambda val: f"  0x614 PKG_POWER_INFO  = {msr_hex(val)}")
        if info is not None and unit is not None:
            di = decode_power_info(info, unit)
            print(f"        TDP (cTDP design point) {w(int(di['tdp_w']*1e6))}  "
                  f"range reported by the firmware {w(int(di['min_w']*1e6))} .. {w(int(di['max_w']*1e6))}")
            print(dim(f"        this max_w is the ceiling apply uses so nothing is written above "
                      f"what the firmware promises - together with powercap/DPTF"))
        elif info is not None:
            print(dim("        (raw only: no power unit)"))
        for addr, label in ((MSR_PP0_POWER_LIMIT, "0x638 PP0 core  "),
                            (MSR_PP1_POWER_LIMIT, "0x640 PP1 uncore")):
            vv = msr_show(addr, label, lambda val, lb=label: f"  {lb} = {msr_hex(val)}")
            if vv is None or unit is None:
                continue
            dd = decode_pp_limit(vv, unit)
            print(f"        limit {w(int(dd['limit_w']*1e6))} en={dd['en']} "
                  f"lock={red('1') if dd['lock'] else green('0')}")
        hwpe = msr_show(MSR_PM_ENABLE, "0x770 PM_ENABLE",
                        lambda val: f"  0x770 PM_ENABLE        = {msr_hex(val)}  HWP (Speed Shift) "
                                    f"{green('enabled') if val & 1 else red('disabled')}")
        msr_show(MSR_HWP_CAPABILITIES, "0x771 HWP_CAPABILITIES",
                 lambda val: f"  0x771 HWP_CAPABILITIES = {msr_hex(val)}\n"
                             f"        highest {val & 0xFF} ({(val & 0xFF) * 100} MHz)  "
                             f"guaranteed {(val>>8)&0xFF}  efficient {(val>>16)&0xFF}  "
                             f"lowest {(val>>24)&0xFF}  [ratio x100 MHz]")
        req = msr_show(MSR_HWP_REQUEST, "0x774 HWP_REQUEST",
                       lambda val: f"  0x774 HWP_REQUEST      = {msr_hex(val)}")
        if req is not None:
            epp_raw = (req >> 24) & 0xFF
            epp_name = {val: key for key, val in EPP_VALUES.items()}.get(epp_raw, "?")
            match = "" if epp_name == get_epp() else dim("  <- differs from sysfs")
            print(f"        EPP {epp_raw} ({epp_name})  sysfs says {get_epp()}{match}")
        msr_show(MSR_OC_MAILBOX, "0x150 OC_MAILBOX",
                 lambda val: f"  0x150 OC_MAILBOX       = {msr_hex(val)}  (FIVR/undervolt interface - "
                             f"TGL blocks voltage offset in microcode; nothing is written here)")
        msr_show(MSR_POWER_CTL, "0x1FC POWER_CTL",
                 lambda val: f"  0x1FC POWER_CTL        = {msr_hex(val)}"
                             f"  {dim('(BD PROCHOT bit not confirmed on this generation - see experimental)')}")
        tt = msr_show(MSR_TEMPERATURE_TARGET, "0x1A2 TEMP_TARGET",
                      lambda val: f"  0x1A2 TEMP_TARGET      = {msr_hex(val)}  TjMax {(val>>16)&0xFF} C  "
                                  f"TCC offset {(val>>24)&0x3F} C  "
                                  f"(sysfs says {dptf_value('tcc_offset_degree_celsius') or 'n/a'} C)")
        if tt is not None and DPTF is None:
            print(dim(f"        (no DPTF device to compare the TCC offset with: {dptf_note()})"))

    # --- termico / DPTF
    print(bold("\n== THERMAL / ACPI (the vendor allocation switch) =="))
    print(f"  {platform_profile_note()}")
    if DPTF is None:
        print(dim(f"  DPTF/proc_thermal : {dptf_note()}"))
    else:
        print(f"  workload_type    : {dptf_value('workload_request/workload_type')}   "
              f"(hint for the firmware: {dptf_value('workload_request/workload_available_types')})")
        print(f"  TCC offset       : {dptf_value('tcc_offset_degree_celsius')} C", end="")
        if IS_ROOT:
            # Real write test with no side effect: it rewrites the SAME value.
            cur_tcc = dptf_value("tcc_offset_degree_celsius")
            tcc_ok, tmsg = write_sysfs(DPTF / "tcc_offset_degree_celsius", cur_tcc)
            print(dim("  (write test: accepts the setting)")
                  if tcc_ok else red(f"  (read-only: {tmsg})"))
        else:
            print(dim("  (run as root to test whether writes are accepted)"))
    # Thermal zones are matched by 'type', never by index: the index differs per
    # machine, so printing zone3 as a CPU temperature is the kind of reading that
    # looks right and means nothing.
    for line in thermal_report():
        print(line)

    # --- iGPU
    g = gpu_snapshot()
    if g:
        print(bold("\n== iGPU (i915) =="))
        print(f"  active/cur freq : {g['act_mhz']} / {g['cur_mhz']} MHz   "
              f"(min {g['min_mhz']} max {g['max_mhz']} boost {g['boost_mhz']})")
        print(f"  rc6             : {g['rc6']}  ({g['rc6_ms']} ms resident)")
        print("  throttle reasons:")
        for k, (field, desc) in zip(g["throttle"], GPU_THROTTLE_BITS):
            v = g["throttle"][field]
            flag = green("0") if v == 0 else red(str(v))
            print(f"    {desc:48s} {flag}")

    # --- competidores
    print(bold("\n== WHAT COMPETES WITH YOU =="))
    for k, v in competitors().items():
        mark = green(v) if v != "active" else yellow(v + "  <= will overwrite your limits")
        print(f"  {k:22s} {mark}")
    print(f"  tuned profile active   : {tuned_profile()}")

    print(dim("\n  reading: to give the iGPU budget, the trick is to cap PP0 (cores) - the package"))
    print(dim("  (PL1/PL2) is shared between cores, uncore/ring, iGPU and memory."))
    print(dim("  On Windows DPTF (Intel Dynamic Tuning) does that split dynamically."))
    print(dim("  On Linux there is no such engine -> the split is fixed here and reaffirmed by a timer.\n"))

    # --- plugins
    plug = plugins()
    print(bold("== PLUGINS (vendor specific, not platform)"))
    for name, err in _PLUGIN_ERRORS:
        print(red(f"  {name} did not load: {err}"))
    if not plug:
        print(dim("  no applicable plugin: only what Intel and ACPI give on any machine."))
    else:
        print(f"  {'applicable':22s} {', '.join(p.ID for p in plug)}")
        print_plugin_sections()
        for warn in plugin_warnings():
            print(yellow(f"  warning: {warn}"))
    print()


# ----------------------------------------------------------------------------
# APPLY
# ----------------------------------------------------------------------------
# Names that changed: the old one keeps working, because a state file written
# before the rename is still on disk in /var/lib/throttlefed/active.json.
PROFILE_ALIASES = {"macbook": "burst"}


def resolve_profile(name):
    name = PROFILE_ALIASES.get(name, name)
    data = load_json(SYSTEM_PROFILES, None)
    if data and name in data:
        return data[name], SYSTEM_PROFILES
    if name in PROFILE_TEMPLATES:
        return PROFILE_TEMPLATES[name], "builtin"
    raise SystemExit(f"profile '{name}' does not exist. available: "
                     f"{sorted(set(list(PROFILE_TEMPLATES) + (list(data) if data else [])))}")


def current_targets():
    s = rapl_snapshot()
    return {
        "pl1_uw": s["package"]["pl1_uw"],
        "pl2_uw": s["package"]["pl2_uw"],
        "pl2_win_us": s["package"]["pl2_win_us"],
        "pp0_uw": s["core"]["limit_uw"] if s["core"]["enabled"] else None,
        "epp": get_epp(),
        "platform_profile": read(PLATFORM_PROFILE),
        "gt_min_mhz": read_int(GT / "rps_min_freq_mhz") if GT else None,
    }


class Plan(list):
    """The rows of a plan, plus the notes for what this machine cannot take.

    It is still a list because the GUI and the helper consume the rows
    positionally; the notes ride along as an attribute so a key the profile asks
    for is never dropped without a word.
    """

    def __init__(self, rows=(), warnings=(), caps=None):
        super().__init__(rows)
        self.warnings = list(warnings)
        self.caps = dict(caps or {})


def missing_domain(label, domain):
    """The sentence printed instead of a row whose target does not exist here."""
    zones = ", ".join(sorted(RAPL_DOMAINS)) or "none"
    return (f"{label}: no RAPL domain '{domain}' on this machine (zones found: {zones})  -  "
            f"the row was not emitted; never write a constraint into another domain's zone")


def pp0_cap(caps):
    """The ceiling for the core domain: its own max, else the package's.

    PP0 is a slice of the package budget, so a core limit above the package
    ceiling is not a number the firmware can honour.
    """
    own, _why = read_rel("core", "constraint_0_max_power_uw")
    if own:
        return own, f"powercap {RAPL_DOMAINS.get('core')}/constraint_0_max_power_uw"
    return caps.get(0, (None, None))


def build_plan(prof, overrides, force=False):
    """The rows to write, built against the domains and ceilings found here.

    Paths come from the discovered zone map: a domain this machine does not have
    is reported and its row is not emitted, because writing a constraint into
    whatever zone sat at the assumed index is worse than not writing at all.
    Values are clamped to what the firmware reports (--force keeps the request,
    and says out loud that it did).
    """
    plan, warns = [], []
    caps = firmware_pl_caps()
    pkg_pl1 = rapl_file("package", "constraint_0_power_limit_uw")
    pkg_pl2 = rapl_file("package", "constraint_1_power_limit_uw")
    pkg_win = rapl_file("package", "constraint_1_time_window_us")
    core_pl = rapl_file("core", "constraint_0_power_limit_uw")
    core_en = rapl_file("core", "enabled")

    pl1 = overrides.get("pl1_w", prof.get("pl1_w"))
    pl2 = overrides.get("pl2_w", prof.get("pl2_w"))
    pp0 = overrides.get("pp0_w", prof.get("pp0_w"))
    epp = overrides.get("epp", prof.get("epp"))
    plat = overrides.get("platform_profile", prof.get("platform_profile"))
    gtmin = overrides.get("gt_min_mhz", prof.get("gt_min_mhz"))
    cur = current_targets()

    def clamp_watts(label, watts, idx=None, cap=None):
        uw = None if watts is None else int(watts * 1_000_000)
        cap_uw, source = cap if cap is not None else caps.get(idx, (None, None))
        fixed, note = clamp_line(label, uw, cap_uw, source, forced=force)
        if note:
            warns.append(note)
        return fixed

    if pl1 is not None:
        if pkg_pl1 is None:
            warns.append(missing_domain("RAPL PL1 (long_term)", "package"))
        else:
            uw = clamp_watts("RAPL PL1 (long_term)", pl1, 0)
            plan.append(("RAPL PL1 (long_term)", pkg_pl1, uw, w(cur["pl1_uw"]), wstr(uw)))
    if pl2 is not None:
        if pkg_pl2 is None:
            warns.append(missing_domain("RAPL PL2 (short_term)", "package"))
        else:
            uw = clamp_watts("RAPL PL2 (short_term)", pl2, 1)
            plan.append(("RAPL PL2 (short_term)", pkg_pl2, uw, w(cur["pl2_uw"]), wstr(uw)))
    # tau (the PL1 window) does NOT go in the list: the firmware already keeps ~28s
    # (tmin/tmax 28-32s) and the field encoding is not reliable enough for me to
    # keep rewriting it. If it is ever needed, write straight into the package
    # zone discovered by name (domain 'package', constraint_0_time_window_us).
    #
    # The PL2 window, on the other hand, is the BURST SIZE: in stock it is 2.44 ms
    # (a blink - PL2 never actually sustains). The "burst" profile asks for 2 s:
    # that is what makes the click feel instant without letting go of the PL1 average.
    # The kernel rounds to the MSR logarithmic step; the readback reports the real value.
    win = overrides.get("pl2_win_us", prof.get("pl2_win_us"))
    if win is not None:
        if pkg_win is None:
            warns.append(missing_domain("RAPL PL2 window (burst)", "package"))
        else:
            cur_ms = (cur["pl2_win_us"] or 0) / 1000.0
            plan.append(("RAPL PL2 window (burst)", pkg_win, int(win),
                         f"{cur_ms:.1f} ms", f"{int(win) / 1000.0:.0f} ms"))
    if pp0 is not None:
        if core_pl is None:
            warns.append(missing_domain("RAPL PP0 core (core cap)", "core"))
        else:
            uw = clamp_watts("RAPL PP0 core (core cap)", pp0, cap=pp0_cap(caps))
            plan.append(("RAPL PP0 core (core cap)", core_pl, uw, w(cur["pp0_uw"]),
                         wstr(uw)))
            plan.append(("RAPL PP0 enabled", core_en, 1, str(read_int(core_en)), "1"))
    if epp is not None:
        if not Path("/sys/devices/system/cpu/cpufreq").exists():
            warns.append("EPP (Speed Shift): no cpufreq policy on this machine, so the "
                         "EPP register is not exposed  -  the row was not emitted")
        else:
            plan.append(("EPP (Speed Shift)", None, epp, str(cur["epp"]), epp))
    if plat is not None:
        choices = platform_profile_choices()
        if not PLATFORM_PROFILE.exists():
            warns.append(f"platform_profile (thermal mode): {PLATFORM_PROFILE} does not exist "
                         f"on this machine (no ACPI thermal mode switch)  -  the row was not emitted")
        else:
            if choices and plat not in choices:
                warns.append(f"platform_profile (thermal mode): '{plat}' is not one of the values "
                             f"this firmware accepts ({', '.join(choices)})  -  the write will fail")
            plan.append(("platform_profile (thermal mode)", PLATFORM_PROFILE, plat,
                         str(cur["platform_profile"]), plat))
            plan.extend(vendor_thermal_plan(plat))
    if gtmin is not None:
        if GT is None:
            warns.append(f"iGPU rps_min_freq (MHz): {gt_absent_why()}  -  the row was not emitted, "
                         f"there is no GT to write it to")
        else:
            low, high = gpu_freq_range()
            mhz, note = clamp_mhz("iGPU rps_min_freq (MHz)", int(gtmin), low, high, forced=force)
            if note:
                warns.append(note)
            plan.append(("iGPU rps_min_freq (MHz)", GT / "rps_min_freq_mhz", mhz,
                         str(cur["gt_min_mhz"]), str(mhz)))
    return Plan(plan, warns, caps)



def cmd_apply(args):
    prof, src = resolve_profile(args.profile)
    overrides = {
        "pl1_w": args.pl1, "pl2_w": args.pl2, "pp0_w": args.core,
        "epp": args.epp, "platform_profile": args.thermal, "gt_min_mhz": args.gt_min,
    }
    overrides = {k: v for k, v in overrides.items() if v is not None}
    plan = build_plan(prof, overrides, force=args.force)

    print(yellow(f"\nprofile: {args.profile}  (source: {src})"))
    if prof.get("_doc"):
        print(dim(f"  {prof['_doc']}"))
    # The ceiling in force is printed before the rows: a plan that was clamped
    # without saying so is a plan whose numbers no longer mean what they say.
    print(dim(f"  firmware ceilings in use: {caps_note()}"))
    for warn in plan.warnings:
        print(yellow(f"  ! {warn}"))
    print(f"{'knob':32s} {'now':>12s} -> {'target':>12s}")
    print("-" * 60)
    for label, path, value, old, new in plan:
        flag = dim("  (= already set)") if str(old) == str(new) else ""
        print(f"{label:32s} {str(old):>12s} -> {blue(f'{new:>12s}')}{flag}")
    if not plan:
        if not prof.get("pl1_w") and not prof.get("pl2_w") and not prof.get("pp0_w"):
            print(dim("  stock profile: it forces nothing by design. Apply a budget profile "
                      "(cpu, cpu-max, gpu, gpu-max, quiet) to move a number."))
        else:
            print(yellow("  empty plan: this machine has none of the targets the profile asks for "
                         " -  run 'probe' to see which capability is missing."))

    if args.dry_run:
        print(yellow("\n--dry-run: nothing was written.\n"))
        return 0

    if not IS_ROOT:
        raise SystemExit(red("\nroot required: sudo throttlefed.py apply " + args.profile))

    if not STOCK.exists() or args.reset_stock:
        try:
            save_json(STOCK, capture_stock())
        except SystemExit:
            print(red(f"  could not capture the stock into {STOCK}: the state directory is not "
                      f"writable. 'restore' will have nothing to go back to."))
            return 1
        print(green(f"stock captured at {STOCK}"))

    if args.takeover:
        # The list comes from the machine (competitors()), so tuned-ppd  -  the one
        # Fedora 44 actually runs  -  is stopped too, instead of only tuned/thermald.
        for svc, state, why in competitors_detail():
            if state != "active":
                continue
            print(yellow(f"  --takeover: parando {svc}"))
            ok, out = systemctl("stop", svc)
            if not ok:
                print(red(f"  --takeover: could not stop {svc}: {out or 'no output'}"))

    if args.if_drift:
        # A row whose current value could not be read is NOT 'no drift': that is
        # how the guard used to keep quiet while rewriting the same limits every
        # 60 s. Unreadable rows are named and the write is attempted.
        changed, unreadable = drift_rows(plan)
        for label, old in unreadable:
            print(yellow(f"  ? {label}: current value unreadable ({old!r})  -  rewriting instead of "
                         f"assuming it is already correct"))
        if not changed and not unreadable:
            print(dim("no drift - nothing to do."))
            save_json(ACTIVE, {"profile": args.profile, "ts": time.time(),
                               "plan_failures": [], "fail_streak": 0})
            return 0

    print()
    failures = []
    for label, path, value, old, new in plan:
        if path is None:  # EPP
            for f, ok, msg in set_epp(value):
                if ok:
                    print(green(f"  [ok]   {label} em {f} -> {value}"))
                else:
                    print(red(f"  [FAIL] {label} em {f}: {msg}"))
                    failures.append(f"{label}:{msg}")
            continue
        ok, msg = write_sysfs(path, value)
        (print(green(f"  [ok]   {label} -> {new}")) if ok
         else print(red(f"  [FAIL] {label} -> {new}: {msg}")))
        if not ok:
            failures.append(f"{label}: {msg}")

    # verifica de verdade
    print(bold("\nverification (read back):"))
    after = current_targets()
    print(f"  PL1={w(after['pl1_uw'])}  PL2={w(after['pl2_uw'])}  "
          f"janela_PL2={after['pl2_win_us']}us  "
          f"PP0={w(after['pp0_uw'])}  EPP={after['epp']}  "
          f"thermal={after['platform_profile']}  GTmin={after['gt_min_mhz']}")

    if IS_ROOT:
        unit = power_unit()
        if unit is None:
            print(dim(f"  0x610 readback skipped: power unit unknown ({power_unit_why()})"))
        else:
            value, why = msr_try(MSR_PKG_POWER_LIMIT)
            if why:
                print(dim(f"  0x610 readback failed: {why}"))
            else:
                d = decode_pkg_power_limit(value, unit)
                print(dim(f"  0x610 = {msr_hex(value)}  -> PL1 {d['pl1_w']:.2f} W / "
                          f"PL2 {d['pl2_w']:.2f} W / lock={d['lock']}"))

    prev = load_json(ACTIVE, {}) or {}
    streak = 0 if not failures else int(prev.get("fail_streak") or 0) + 1
    save_json(ACTIVE, {"profile": args.profile, "ts": time.time(),
                       "plan_failures": failures, "fail_streak": streak})
    if failures:
        print(yellow(f"\n{len(failures)} knob(s) refused the write. BIOS lock? see 'probe' (bit 63 of 0x610)."))
        # A non-zero return is the only thing the timer/auto can act on: an apply
        # that refuses half the plan must not look like a clean run.
        print(red("apply: " + "; ".join(failures)))
        if streak > 1:
            print(red(f"apply: {streak} consecutive runs could not put the profile back  -  the "
                      f"timer keeps retrying every 60 s; fix the knob or stop the timer."))
        return len(failures)
    print(green(f"\nprofile '{args.profile}' active. restore = undo."))
    return 0


# ----------------------------------------------------------------------------
# RESTORE
# ----------------------------------------------------------------------------
def cmd_restore(args):
    st = load_json(STOCK, None)
    if not st:
        raise SystemExit(red(f"no stock saved at {STOCK} - nothing to restore."))
    plan = []
    r = st["rapl"]
    # Paths come from the discovered zones, and a domain that is not on this
    # machine is left out with a word instead of raising on `None / "..."` (the
    # old code assumed every machine has package+core+uncore).
    pkg_pl1 = rapl_file("package", "constraint_0_power_limit_uw")
    pkg_pl2 = rapl_file("package", "constraint_1_power_limit_uw")
    if pkg_pl1 is None or pkg_pl2 is None:
        print(yellow(f"  no RAPL 'package' domain on this machine - the stock PL1/PL2 cannot "
                     f"be restored ({rapl_interface_note() or 'zone not found'})"))
    else:
        plan.append(("RAPL PL1", pkg_pl1, r["package"]["pl1_uw"],
                     w(read_int(pkg_pl1)), w(r["package"]["pl1_uw"])))
        plan.append(("RAPL PL2", pkg_pl2, r["package"]["pl2_uw"],
                     w(read_int(pkg_pl2)), w(r["package"]["pl2_uw"])))
    pkg_win1 = rapl_file("package", "constraint_0_time_window_us")
    if pkg_win1 and r["package"]["pl1_win_us"]:
        plan.append(("RAPL window", pkg_win1, r["package"]["pl1_win_us"],
                     read(pkg_win1), str(r["package"]["pl1_win_us"])))
    pkg_win2 = rapl_file("package", "constraint_1_time_window_us")
    if pkg_win2 and r["package"].get("pl2_win_us"):
        plan.append(("RAPL PL2 window (burst)", pkg_win2, r["package"]["pl2_win_us"],
                     read(pkg_win2), str(r["package"]["pl2_win_us"])))
    for key in ("core", "uncore"):
        base = RAPL_DOMAINS.get(key)
        if base is None:
            print(yellow(f"  no RAPL '{key}' domain on this machine - the stock for that domain "
                         f"cannot be restored"))
            continue
        plan.append((f"RAPL {key} limit", base / "constraint_0_power_limit_uw",
                     r[key]["limit_uw"], w(read_int(base / "constraint_0_power_limit_uw")),
                     w(r[key]["limit_uw"])))
        plan.append((f"RAPL {key} enabled", base / "enabled", r[key]["enabled"],
                     read(base / "enabled"), str(r[key]["enabled"])))
    if st.get("platform_profile"):
        plan.append(("platform_profile", PLATFORM_PROFILE, st["platform_profile"],
                     read(PLATFORM_PROFILE), st["platform_profile"]))
    if st.get("tcc_offset") is not None:
        if DPTF is None:
            print(yellow(f"  the stock TCC offset cannot be restored: {dptf_note()}"))
        else:
            plan.append(("TCC offset", DPTF / "tcc_offset_degree_celsius", st["tcc_offset"],
                         dptf_value("tcc_offset_degree_celsius"), str(st["tcc_offset"])))
    g = st.get("gpu") or {}
    if GT and g.get("min_mhz") is not None:
        plan.append(("iGPU rps_min", GT / "rps_min_freq_mhz", g["min_mhz"],
                     read(GT / "rps_min_freq_mhz"), str(g["min_mhz"])))
    if GT and g.get("max_mhz") is not None:
        plan.append(("iGPU rps_max", GT / "rps_max_freq_mhz", g["max_mhz"],
                     read(GT / "rps_max_freq_mhz"), str(g["max_mhz"])))

    print(bold(f"\nRESTORE - going back to the stock from {time.strftime('%d/%m/%Y %H:%M', time.localtime(st['ts']))}"))
    for label, path, value, old, new in plan:
        print(f"  {label:24s} {str(old):>12s} -> {str(new):>12s}")
    epp_stock = st.get("epp") or {}
    epp_vals = {v for v in epp_stock.values() if v}
    epp_target = next(iter(epp_vals)) if len(epp_vals) == 1 else None
    if epp_target:
        print(f"  {'EPP':24s} {str(get_epp()):>12s} -> {epp_target:>12s}")
    for plug in plugins():
        for ok, msg in plug.restore(st, dry=True):
            print(dim(f"  {plug.ID:24s} {msg}"))

    if args.dry_run:
        print(yellow("\n--dry-run: nothing was written.\n"))
        return
    if not IS_ROOT:
        raise SystemExit(red("\nroot required"))

    for label, path, value, old, new in plan:
        if value is None:
            continue
        ok, msg = write_sysfs(path, value)
        print(green(f"  [ok] {label}") if ok else red(f"  [FAIL] {label}: {msg}"))
    if epp_target:
        for f, ok, msg in set_epp(epp_target):
            print(green(f"  [ok] EPP {f}") if ok else red(f"  [FAIL] EPP {f}: {msg}"))
    for plug in plugins():
        for ok, msg in plug.restore(st):
            print(green(f"  [ok] {plug.ID} {msg}") if ok else red(f"  [FAIL] {plug.ID} {msg}"))
    try:
        ACTIVE.unlink()
    except OSError:
        pass
    print(green("\nstock restaurado.\n"))


# ----------------------------------------------------------------------------
# DEMO / AUTOTESTE
# ----------------------------------------------------------------------------
def demo_scenarios():
    """[(label, profile, overrides)]  -  built from the plan, not from literals.

    The old autotest compared the machine against 28 W and 9 W written into the
    test itself, so it could only ever pass on the laptop those numbers came
    from. The values here come from what the profile asks for, bounded by the
    ceiling the firmware reports, because the test has to write values the
    machine actually accepts.
    """
    caps = firmware_pl_caps()
    ceiling_uw = caps.get(0, (None, None))[0]
    want_pp0 = PROFILE_TEMPLATES["gpu"].get("pp0_w") or 9.0
    pp0 = want_pp0 if ceiling_uw is None else min(want_pp0, ceiling_uw / 1e6)
    return [
        ("A: full 'cpu-max' profile (PL1/PL2/EPP/thermal mode)", "cpu-max", {}),
        (f"B: 'gpu' profile with PP0 at {pp0:g} W (capped cores, package for the iGPU)",
         "gpu", {"core": pp0}),
    ]


def verify_plan(plan, after, check):
    """Check every plan row against the value that same plan asked for."""
    fields = {
        "RAPL PL1 (long_term)": ("pl1_uw", 1_000_000),
        "RAPL PL2 (short_term)": ("pl2_uw", 1_000_000),
        "RAPL PP0 core (core cap)": ("pp0_uw", 1_000_000),
        "iGPU rps_min_freq (MHz)": ("gt_min_mhz", 1),
    }
    for label, _path, value, _old, new in plan:
        key, tol = fields.get(label, (None, None))
        if key is None:
            if label in ("EPP (Speed Shift)", "platform_profile (thermal mode)"):
                key, tol = ("epp", None) if label.startswith("EPP") else ("platform_profile", None)
            else:
                continue
        got = after.get(key)
        if tol is None:
            ok = str(got) == str(value)
        else:
            ok = got is not None and abs(int(got) - int(value)) <= tol
        check(f"{label} -> {new} (read back)", ok, f"read: {got if got is not None else 'n/a'}")


def drift_rows(plan):
    """(changed, unreadable) rows of a plan, for the drift guard.

    A row is 'changed' only when the machine was read and disagrees; a row whose
    current value never arrived is reported separately instead of being folded
    into 'no drift'.
    """
    changed, unreadable = [], []
    for row in plan:
        _label, _path, _value, old, new = row
        if old is None or str(old).strip() in ("", "n/a", "None", "?"):
            unreadable.append((_label, old))
        elif str(old) != str(new):
            changed.append(row)
    return changed, unreadable


def cmd_demo(args):
    """Closed loop: real apply -> verify (sysfs + MSR) -> restore -> verify.
    It ALWAYS restores, even if something blows up in the middle."""
    print(bold(f"\nthrottlefed {VERSION} - selftest (writes for real and comes back)"))
    if not IS_ROOT:
        raise SystemExit(red("root required: sudo throttlefed.py demo"))
    for unit, state, why in competitors_detail():
        if state == "active":
            print(yellow(f"  warning: {unit} active - {COMPETITOR_EFFECT.get(unit, 'rewrites the same limits')}"
                         f"{f' ({why})' if why else ''}: it may rewrite in the middle of the test."))

    cmd_probe(argparse.Namespace(dry_run=False))

    before = current_targets()
    checks = []

    def check(label, cond, detail=""):
        checks.append((label, bool(cond), detail))
        print(green(f"  [ok]   {label} {detail}") if cond
              else red(f"  [FAIL] {label} {detail}"))

    def phase(desc, ns):
        print(bold(f"\n--- {desc}"))
        cmd_apply(ns)
        return current_targets()

    try:
        for label, profile, overrides in demo_scenarios():
            plan = build_plan(PROFILE_TEMPLATES[profile], overrides)
            if not plan:
                check(f"{label}: empty plan", False,
                      "this machine has none of the targets this profile asks for - see 'probe'")
                continue
            ns = argparse.Namespace(profile=profile, pl1=None, pl2=None,
                                   core=overrides.get("core"), uncore=None, epp=None,
                                   thermal=None, gt_min=None, dry_run=False, if_drift=False,
                                   takeover=False, reset_stock=False, force=False)
            after = phase(label, ns)
            verify_plan(plan, after, check)
            pl1_row = next((row for row in plan if row[0] == "RAPL PL1 (long_term)"), None)
            unit = power_unit()
            if pl1_row is None or unit is None:
                check("PL1 in MSR 0x610", False,
                      f"reading impossible ({power_unit_why() if unit is None else 'no PL1 row'})")
                continue
            value, why = msr_try(MSR_PKG_POWER_LIMIT)
            if why:
                check("PL1 in MSR 0x610", False, why)
                continue
            decoded = decode_pkg_power_limit(value, unit)
            check(f"PL1 = {pl1_row[4]} also in MSR 0x610",
                  abs(decoded["pl1_w"] * 1e6 - pl1_row[2]) <= 3 * (1 / unit) * 1000,
                  f"= {decoded['pl1_w']:.2f} W decoded (step {unit} W)")
    finally:
        print(bold("\n--- restore (always runs)"))
        try:
            cmd_restore(argparse.Namespace(dry_run=False))
        except BaseException as e:
            print(red(f"  restore failed: {type(e).__name__}: {e}"))
            print(red(f"  >> run it by hand: sudo throttlefed.py restore"))
        after = current_targets()

    check("PL1 is back to stock", after["pl1_uw"] == before["pl1_uw"],
          f"{w(before['pl1_uw'])} -> {w(after['pl1_uw'])}")
    check("PL2 is back to stock", after["pl2_uw"] == before["pl2_uw"],
          f"{w(before['pl2_uw'])} -> {w(after['pl2_uw'])}")
    check("EPP is back to stock", after["epp"] == before["epp"],
          f"{before['epp']} -> {after['epp']}")
    check("platform_profile is back", after["platform_profile"] == before["platform_profile"],
          f"{before['platform_profile']} -> {after['platform_profile']}")
    check("PP0 is back to stock", after["pp0_uw"] == before["pp0_uw"],
          f"{w(before['pp0_uw'])} -> {w(after['pp0_uw'])}")

    fails = [c for c in checks if not c[1]]
    print()
    if fails:
        print(red(f"SELFTEST: {len(fails)}/{len(checks)} failed - see the [FAIL] lines above."))
        return 1
    print(green(f"SELFTEST: {len(checks)}/{len(checks)} OK - apply and restore work on this machine."))
    return 0


# ----------------------------------------------------------------------------
# AUTO (boot + timer)
# ----------------------------------------------------------------------------
def cmd_auto(args):
    act = load_json(ACTIVE, None)
    if not act or not act.get("profile"):
        print(dim("throttlefed: no active profile - nothing to reaffirm."))
        return 0
    args.profile = act["profile"]
    args.dry_run = False
    args.if_drift = True
    args.reset_stock = False
    args.takeover = False
    args.force = False
    args.pl1 = args.pl2 = args.core = args.epp = args.thermal = args.gt_min = None
    streak = int(act.get("fail_streak") or 0)
    if streak:
        print(red(f"throttlefed auto: the last {streak} run(s) could not put the profile back "
                  f"({len(act.get('plan_failures') or [])} knob(s) refused each time)"))
    print(f"throttlefed auto: reafirmando '{act['profile']}'")
    try:
        code = cmd_apply(args)
    except SystemExit as e:
        # Swallowing this used to make a failing auto look like a clean run: the
        # timer kept firing every 60 s and the exit status never said so.
        print(red(f"auto: apply aborted: {e}"))
        return 1
    if code:
        print(red(f"auto: {code} knob(s) refused  -  the profile is not fully applied"))
    return code or 0


# ----------------------------------------------------------------------------
# WATCH
# ----------------------------------------------------------------------------
def cmd_watch(args):
    roles = thermal_by_role()
    header = " ".join(f"{role:>6s}" for role, _k, _p, _t, _w in roles)
    print(bold(f"\n{'PL1':>7s} {'PL2':>7s} {'pkg W':>7s} {'core MHz':>9s} {'GT MHz':>7s} "
               f"{header}  thr(pl1/pl2/thm/prochot)"))
    # The legend travels with the numbers: the header used to read 'TCPU PKG' while
    # the columns came from two thermal zones that are device sensors on most
    # machines, so device temperatures were shown as if they were CPU and package.
    print(dim("       " + thermal_report()))
    if not IS_ROOT:
        print(yellow("no root: package watts do not show (energy_uj is root-only). the rest works."))
    prev_e = rapl_uw()
    prev_t = time.time()
    print("-" * 78)
    i = 0
    try:
        while args.count == 0 or i < args.count:
            i += 1
            time.sleep(args.interval)
            now = time.time()
            e = rapl_uw()
            pkg_w = None
            if e is not None and prev_e is not None and now > prev_t:
                pkg_w = (e - prev_e) / (now - prev_t) / 1e6
            prev_e, prev_t = e, now
            g = gpu_snapshot()
            thr = g.get("throttle", {})
            thr_s = "/".join(str(thr.get(f, "?")) for f in
                             ("throttle_reason_pl1", "throttle_reason_pl2",
                              "throttle_reason_thermal", "throttle_reason_prochot"))
            freq = read_int("/sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq")
            temps = []
            for role, kind, path, temp, why in thermal_by_role():
                if temp is None:
                    temps.append(f"{'n/a':>6s}")
                else:
                    temps.append(f"{temp:6.1f}")
            pl1, why1 = read_rel("package", "constraint_0_power_limit_uw")
            pl2, why2 = read_rel("package", "constraint_1_power_limit_uw")
            print(f"{w(pl1):>7s} {w(pl2):>7s} "
                  f"{(f'{pkg_w:6.2f} W' if pkg_w is not None else '     n/a'):>7s} "
                  f"{freq/1000 if freq else 0:9.0f} {g.get('act_mhz') or 0:7d} "
                  f"{' '.join(temps)}  {thr_s}")
            if not IS_ROOT:
                continue
    except KeyboardInterrupt:
        print("\nend.")
    print()


# ----------------------------------------------------------------------------
# INSTALL / UNINSTALL
# ----------------------------------------------------------------------------
SERVICE = """[Unit]
Description=throttlefed - applies the active power profile (ThrottleStop style)
Documentation=file:{target}
After=multi-user.target
# No Wants=thermald.service: asking for the daemon that rewrites the same limits
# is asking for the drift this unit exists to correct.

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart={bin} auto
Nice=5
"""

TIMER = """[Unit]
Documentation=file:{target}
Description=throttlefed - drift guard (thermald/tuned/BIOS rewrite the limits)
# Skipped entirely on a machine with no powercap at all, instead of failing every
# 60 s with an error nobody reads.
ConditionPathExists=/sys/class/powercap

[Timer]
OnBootSec=25s
OnUnitActiveSec=60s
AccuracySec=1s

[Install]
WantedBy=timers.target
"""


def wrapper_script():
    """The /usr/local/bin wrapper the unit calls.

    A unit whose ExecStart is the checkout path stops working the moment the
    checkout moves, and this file is meant to be cloned anywhere. The wrapper is
    the one place that knows where the script lives: 'install' rewrites it, and
    the unit itself never carries it.
    """
    python = shutil.which("python3") or "/usr/bin/python3"
    return (f"#!/bin/sh\n"
            f"# Written by throttlefed.py install; re-run install after moving the checkout.\n"
            f'exec {python} "{SELF}" "$@"\n')


def cmd_install(args):
    files = {
        WRAPPER: wrapper_script(),
        UNIT_DIR / "throttlefed.service": SERVICE.format(bin=WRAPPER, target=SELF),
        UNIT_DIR / "throttlefed.timer": TIMER.format(target=SELF),
        SYSTEM_PROFILES: json.dumps(PROFILE_TEMPLATES, indent=2),
    }
    print(bold("\ninstallation:\n"))
    for path, content in files.items():
        print(f"  {path}")
        if args.dry_run:
            print(dim("    --dry-run, content that would be written:"))
            for line in content.splitlines():
                print(dim(f"      {line}"))
    if args.dry_run:
        print(yellow("\n--dry-run: nothing written.\n"))
        return 0
    ok, why = systemd_available()
    if not ok:
        print(red(f"\nno persistence available here: {why}"))
        print(dim("  install writes systemd units; on a machine without systemd, "
                  "call 'apply' from whatever starts your session instead.\n"))
        return 2
    if not Path(SELF).is_absolute() or not Path(SELF).exists():
        raise SystemExit(red(f"cannot install: {SELF} is not an existing absolute path "
                             f"(run install from the checkout itself)"))
    if not IS_ROOT:
        raise SystemExit(red("root required: sudo throttlefed.py install"))
    for path, content in files.items():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        except OSError as exc:
            raise SystemExit(red(f"cannot write {path}: {exc}"))
        if path == WRAPPER:
            os.chmod(path, 0o755)
        print(green(f"  wrote {path}"))
    for verb in (("daemon-reload",), ("enable", "--now", "throttlefed.timer"),
                 ("start", "throttlefed.service")):
        ok, out = systemctl(*verb)
        print(green(f"  systemctl {' '.join(verb)}: ok") if ok else
              red(f"  systemctl {' '.join(verb)}: FAILED  -  {out or 'no output'}"))
    enabled, active, state = unit_state("throttlefed.timer")
    if enabled and active:
        print(green("\nthrottlefed.timer enabled and active: the profile is reapplied on "
                    "boot and every 60 s"))
    else:
        print(yellow(f"\nthrottlefed.timer is NOT enabled/active ({state}): the profile is "
                     f"not persistent  -  fix the unit above and re-run install"))
    print("  ajuste perfis em /etc/throttlefed/profiles.json")
    print("  verifique: systemctl status throttlefed.timer --no-pager\n")
    return 0


def cmd_uninstall(args):
    if not IS_ROOT:
        raise SystemExit(red("root required"))
    ok, why = systemd_available()
    if not ok:
        print(yellow(f"no systemd here ({why})  -  removing the files only"))
    else:
        for verb in (("disable", "--now", "throttlefed.timer"), ("stop", "throttlefed.service")):
            ok, out = systemctl(*verb)
            print(dim(f"  systemctl {' '.join(verb)}: {'ok' if ok else out or 'no output'}"))
    gone = []
    for f in (UNIT_DIR / "throttlefed.service", UNIT_DIR / "throttlefed.timer", WRAPPER):
        try:
            f.unlink()
            gone.append(f)
            print(f"removed {f}")
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(red(f"did not remove {f}: {exc}"))
    if gone and ok:
        systemctl("daemon-reload")
    print(green("uninstall ok (profiles and stock preserved)."))
    return 0


# ----------------------------------------------------------------------------
# EXPERIMENTAL
# ----------------------------------------------------------------------------
def cmd_experimental(args):
    print(bold("\nexperimental: BD PROCHOT (0x1FC MSR_POWER_CTL)"))
    print("  On Windows ThrottleStop unchecks 'BD PROCHOT' to stop being")
    print("  throttled by an external signal (weak charger / dock).")
    print("  The exact bit of 0x1FC is not documented reliably enough to be")
    print("  guaranteed here - writing the wrong bit touches C-states. So:")
    if not IS_ROOT:
        print(yellow("  no root."))
        return 0
    # The read used to sit outside any try: a machine without the msr module (or
    # without the register) died with a traceback instead of the probe's hint.
    value, why = msr_try(MSR_POWER_CTL)
    if why:
        print(red(f"  0x1FC unreadable: {why}"))
        print(dim("  hint: sudo modprobe msr   (and check that /dev/cpu/0/msr exists)"))
        return 1
    assert value is not None  # msr_try only returns None alongside a reason
    print(f"  0x1FC now = {msr_hex(value)}  (bin {value:064b})")
    for b in range(0, 8):
        print(f"    bit {b} = {(value >> b) & 1}")
    print(yellow("  >> no bit is written by this command. Writing only with --force-bit N on/off"))
    if args.force_bit is not None:
        bit, state = args.force_bit
        gen_key, gen_name = cpu_generation()
        if gen_key not in MSR_BIT_TABLES:
            raise SystemExit(red(
                f"  refused: this CPU is '{gen_name}' and there is no known bit table for it "
                f"({gen_key}); a bit whose meaning was assumed from another generation can be a "
                f"C-state or a different limit. Re-run with --force if you know the bit."))
        nv = (value | (1 << bit)) if state else (value & ~(1 << bit))
        print(red(f"  --force-bit: writing {msr_hex(nv)} (bit {bit} -> {int(state)})"))
        msr_write(MSR_POWER_CTL, nv)
        reread, why2 = msr_try(MSR_POWER_CTL)
        print(f"  reread: {msr_hex(reread)}" if not why2 else red(f"  reread failed: {why2}"))
    return 0


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def cmd_status(args):
    s = rapl_snapshot()
    g = gpu_snapshot()
    act = load_json(ACTIVE, {})
    print(bold("throttlefed status"))
    print(f"  active profile : {act.get('profile', dim('none'))}")
    if act.get("fail_streak"):
        print(red(f"  last runs      : {act['fail_streak']} without being able to apply "
                  f"({'; '.join(act.get('plan_failures') or []) or '?'})"))
    pk, pp = s["package"], s["core"]
    pl1, why1 = read_rel("package", "constraint_0_power_limit_uw")
    pl2, why2 = read_rel("package", "constraint_1_power_limit_uw")
    if pk["present"]:
        print(f"  PL1 / PL2      : {w(pl1)} / {w(pl2)}"
              + dim(f"   ({RAPL_DOMAINS['package']})"))
    else:
        print(f"  PL1 / PL2      : {yellow('n/a')} {dim('- ' + pk['why'].get('zone', 'no package domain'))}")
    for key, why in ((why1, "constraint_0_power_limit_uw"), (why2, "constraint_1_power_limit_uw")):
        if key:
            print(dim(f"      ? {why} unreadable: {key}"))
    if pp["present"]:
        print(f"  PP0 core       : {'on ' + w(pp['limit_uw']) if pp['enabled'] else 'off'}")
    else:
        print(f"  PP0 core       : {dim('n/a - ' + pp['why'].get('zone', 'no core domain'))}")
    if rapl_interface_note():
        print(yellow(f"  RAPL           : {rapl_interface_note()}"))
    print(f"  EPP            : {get_epp() or dim('n/a - no cpufreq policy here')}")
    print(f"  platform_profile: {read(PLATFORM_PROFILE) or dim('n/a')}"
          + (dim(f"   choices: {', '.join(platform_profile_choices())}")
             if platform_profile_choices() else ""))
    if g:
        print(f"  iGPU           : {g['act_mhz']} MHz (min {g['min_mhz']} / max {g['max_mhz']})"
              + dim(f"   {GT_DRIVER} {GT_CARD}"))
    for svc, state, why in competitors_detail():
        mark = yellow(state) if state == "active" else dim(state)
        print(f"  {'competitors    :' if svc == competitors_detail()[0][0] else '                '} "
              f"{svc:<24s} {mark}" + (dim(f"  ({why})") if why else ""))
    if not competitors_detail():
        print(f"  competitors    : {dim('none')}")
    print(f"  stock saved    : {'yes' if STOCK.exists() else 'no'}  {dim(STOCK)}")
    print(f"  state          : {dim(state_note())}")
    for p in plugins():
        for ch in p.channels():
            if ch.role == "thermal":
                print(f"  thermal mode (firmware) : {read(resolve_target(ch.target)) or dim('root-only')}")
    return 0


def cmd_plugins(args):
    plug = plugins()
    if args.json:
        print(json.dumps([p.report() for p in plug], indent=2))
        return 0
    print(bold("throttlefed plugins"))
    print(f"  {'applicable':14s} {', '.join(p.ID for p in plug) if plug else 'none'}")
    for name, err in _PLUGIN_ERRORS:
        print(red(f"  {name}: {err}"))
    for p in plug:
        print(f"\n  {p.ID}  ({p.NAME})")
        print(f"    {p.WHY}")
        for ch in p.channels():
            vals = "/".join(ch.values) if ch.values else "-"
            print(f"    channel {ch.key:30s} [{vals}]")
            print(f"           {ch.why}")
        for s in p.sections():
            print(f"    section {s.title} ({len(s.rows)} attributes)")
    if not plug:
        print(dim("  no plugin applies to this machine."))
    return 0


# ----------------------------------------------------------------------------
# update check
# ----------------------------------------------------------------------------

def version_key(text):
    """'v1.2.3-rc1' -> (1, 2, 3). Empty tuple when there is no number in it, so a
    malformed tag can never come out as newer than this build."""
    core = str(text or "").strip().lstrip("vV").split("-")[0].split("+")[0]
    return tuple(int(n) for n in re.findall(r"\d+", core))


def fetch_published():
    """(version, source, detail) for what is published upstream.

    Two channels, in order: the newest release tag, then the VERSION file on the
    main branch. The file is there so the check means something before the first
    release is tagged; the first channel that answers with a version wins.
    """
    for url, source in ((f"{API}/releases/latest", "release"),
                        (f"{API}/tags", "tag"),
                        (f"{API}/contents/VERSION?ref=main", "branch")):
        req = urllib.request.Request(url, headers={
            "User-Agent": f"throttlefed/{VERSION}",
            "Accept": "application/vnd.github+json"})
        raw, dropped = None, None
        for attempt in (1, 2):
            # A home link drops the odd connection: one quiet retry before giving
            # up, so a single lost packet does not read as "could not check".
            try:
                with urllib.request.urlopen(req, timeout=6) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raw = ""
                    break
                if exc.code in (403, 429):
                    return None, "error", f"GitHub rate limit or block (HTTP {exc.code})"
                return None, "error", f"HTTP {exc.code} from {url}"
            except Exception as exc:
                dropped = exc
                if attempt == 1:
                    time.sleep(1)
        if raw is None:
            return None, "error", f"{type(dropped).__name__}: {dropped} ({url})"
        if raw == "":
            continue  # nothing published on this channel yet
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if source == "release":
            tag = (data or {}).get("tag_name") or ""
        elif source == "tag":
            tag = ((data or [{}])[0] or {}).get("name") or ""
        else:
            try:
                tag = base64.b64decode((data or {}).get("content") or "").decode("utf-8", "replace")
            except Exception:
                tag = ""
        tag = (tag or "").strip().splitlines()[0].strip() if (tag or "").strip() else ""
        if tag:
            return tag.lstrip("vV"), source, f"{tag} ({source} upstream)"
    return None, "none", "nothing published yet"


def update_check(force=False, offline=False):
    """This build against what is published, as a plain dict.

    The answer is cached for UPDATE_TTL, so a launch that finds a fresh entry
    never opens a socket. offline does not open one either: it reports the cache,
    including 'never checked'.
    """
    now = time.time()
    try:
        cached = json.loads(UPDATE_CACHE.read_text())
    except (OSError, ValueError):
        cached = {}
    age = now - float(cached.get("checked_at") or 0)
    # A failure expires in minutes, a success in a day: one dropped connection
    # must not be reported as the answer for the rest of the day.
    ttl = ERROR_TTL if cached.get("error") else UPDATE_TTL
    if offline or (not force and cached.get("checked_at") and age < ttl):
        out = dict(cached) if cached else {"checked_at": 0, "error": "never checked"}
        out["from_cache"] = bool(cached)
    else:
        latest, source, detail = fetch_published()
        out = {"checked_at": now, "latest": latest, "source": source, "detail": detail}
        if latest is None and source == "error":
            out["error"] = detail
        try:
            UPDATE_CACHE.parent.mkdir(parents=True, exist_ok=True)
            UPDATE_CACHE.write_text(json.dumps(out, indent=2) + "\n")
        except OSError:
            pass
        out["from_cache"] = False
    out["local"] = VERSION
    mine, theirs = version_key(VERSION), version_key(out.get("latest") or "")
    out["has_update"] = bool(theirs and mine and theirs > mine)
    out["age_s"] = int(max(0, now - float(out.get("checked_at") or 0)))
    return out


def cmd_update_check(args):
    """0 = newest, 1 = a newer version is published, 2 = could not check."""
    res = update_check(force=args.force, offline=args.offline)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(bold(f"throttlefed {res['local']}"))
        if res.get("latest"):
            print(f"  published      : {res['latest']}  {dim('(' + res['source'] + ')')}")
        elif res.get("error"):
            print(f"  published      : {yellow('could not check')}  {dim(res['error'])}")
        else:
            print(f"  published      : {dim('nothing yet (no release, tag or VERSION upstream)')}")
        if res.get("has_update"):
            print(f"  updates        : {green('yes')} - {res.get('detail') or RELEASES_PAGE}")
            print(f"  how            : git pull, or take the release at {RELEASES_PAGE}")
        elif res.get("latest"):
            print("  updates        : none, this is the newest published version")
        if res.get("from_cache") and res.get("checked_at"):
            print(f"  last check     : {dim(str(res['age_s'] // 3600) + ' h ago (cached, --force to re-check)')}")
        print(dim("  one unauthenticated HTTPS GET to GitHub; nothing about this machine is sent"))
    if res.get("has_update"):
        return 1
    if res.get("error") and not res.get("latest"):
        return 2
    return 0


def main():
    ap = argparse.ArgumentParser(
        prog="throttlefed",
        description="ThrottleStop for Linux: CPU <-> iGPU power budget (RAPL/MSR/ACPI/DRM).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  sudo throttlefed.py probe                 full x-ray
  sudo throttlefed.py apply gpu --dry-run   what would change
  sudo throttlefed.py apply gpu             budget for the iGPU
  sudo throttlefed.py apply cpu-max         CPU at the ceiling
  sudo throttlefed.py set --pl1 20 --epp balance_power
  sudo throttlefed.py watch                 live monitor
  sudo throttlefed.py restore               back to stock
  sudo throttlefed.py install               persist (systemd + 60s timer)
  throttlefed.py update-check               is there a newer release?
""")
    ap.add_argument("--version", action="version", version=f"throttlefed {VERSION}")
    ap.add_argument("--no-plugins", action="store_true",
                    help="ignore vendor plugins (platform only)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("probe", help="x-ray of everything (partially works without root)").set_defaults(func=cmd_probe)
    sub.add_parser("status", help="short summary").set_defaults(func=cmd_status)

    tc = sub.add_parser("thermal-choices", help="list the values --thermal accepts here")
    tc.set_defaults(func=cmd_thermal_choices)

    a = sub.add_parser("apply", help="apply a profile")
    a.add_argument("profile", nargs="?", default="balanced",
                   help="balanced|cpu|cpu-max|gpu|gpu-max|quiet or whatever is in /etc/throttlefed/profiles.json")
    a.add_argument("--pl1", type=float, help="PL1 in W (long_term)")
    a.add_argument("--pl2", type=float, help="PL2 in W (short_term)")
    a.add_argument("--core", type=float, help="PP0 domain (cores) cap in W - this is where you take from the CPU for the iGPU")
    a.add_argument("--epp", choices=sorted(EPP_VALUES), help="Speed Shift energy preference")
    a.add_argument("--thermal", help="ACPI platform_profile (vendor thermal mode) - accepted values: throttlefed thermal-choices")
    a.add_argument("--gt-min", type=int, dest="gt_min", help="iGPU minimum frequency in MHz")
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--if-drift", action="store_true", help="only write what changed (used by the timer)")
    a.add_argument("--takeover", action="store_true", help="stop thermald/tuned (otherwise they rewrite)")
    a.add_argument("--reset-stock", action="store_true", help="re-record the stock with the current state")
    a.add_argument("--force", action="store_true", help="allow extreme values")
    a.set_defaults(func=cmd_apply)
    sub.add_parser("set", parents=[a], add_help=False).set_defaults(func=cmd_apply)

    r = sub.add_parser("restore", help="back to the stock captured on the first apply")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(func=cmd_restore)

    sub.add_parser("auto", help="reaffirm the active profile (boot/timer)").set_defaults(func=cmd_auto)

    sub.add_parser("demo", help="selftest: real apply + verify (sysfs/MSR) + restore"
                   ).set_defaults(func=cmd_demo)

    wt = sub.add_parser("watch", help="live monitor")
    wt.add_argument("--interval", type=float, default=1.0)
    wt.add_argument("--count", type=int, default=0, help="0 = infinite")
    wt.set_defaults(func=cmd_watch)

    i = sub.add_parser("install", help="systemd service + timer (persists across reboots)")
    i.add_argument("--dry-run", action="store_true")
    i.set_defaults(func=cmd_install)

    u = sub.add_parser("uninstall", help="remove units")
    u.set_defaults(func=cmd_uninstall)

    e = sub.add_parser("experimental", help="unconfirmed things (BD PROCHOT)")
    e.add_argument("--force-bit", nargs=2, metavar=("BIT", "on|off"), default=None)
    e.set_defaults(func=cmd_experimental)

    pl = sub.add_parser("plugins", help="vendor plugins (what is theirs, not the platform's)")
    pl.add_argument("--json", action="store_true")
    pl.set_defaults(func=cmd_plugins)

    up = sub.add_parser("update-check",
                        help="is there a newer release? (one HTTPS GET, cached for a day)")
    up.add_argument("--force", action="store_true", help="ignore today's cached answer")
    up.add_argument("--offline", action="store_true",
                    help="read the cache only, never touch the network")
    up.add_argument("--json", action="store_true", help="machine-readable output")
    up.set_defaults(func=cmd_update_check)

    args = ap.parse_args()
    global PLUGINS_DISABLED
    PLUGINS_DISABLED = bool(args.no_plugins)
    if hasattr(args, "force_bit") and args.force_bit:
        args.force_bit = (int(args.force_bit[0]), args.force_bit[1].lower() in ("on", "1", "true"))
    rc = args.func(args)
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()
