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
"""
plugins/dell.py - the Dell channel: BIOS attributes published through dell-wmi-sysman.

Dell exposes part of the power and thermal policy as firmware attributes, not as
registers. On Windows that is the channel Dell Power Manager drives, and it is a
different channel from RAPL: the value is firmware state, it survives a reboot, and
it can take effect before the kernel starts.

Two consequences worth knowing before writing anything here:

  * the attribute is not the register. Writing ThermalManagement does not write
    constraint_0_power_limit_uw, and the reverse is also true;
  * `current_value` is root-only, and this kernel does not publish `is_readonly`.
    So whether an attribute is writable is only proved by writing it and reading it
    back, which is what the tool does anyway.

Only power, thermal and battery attributes are exposed. The same directory carries
security and boot attributes (SecureBoot, passwords, TPM, virtualization), and
touching those is not this tool's business.
"""

from pathlib import Path

from throttlefed_plugin_api import Channel, Plugin, Section

###############################################
# DEFINITIONS
###############################################

VENDOR_DIR = "dell-wmi-sysman"
ATTRS = Path("/sys/class/firmware-attributes") / VENDOR_DIR / "attributes"

# Attributes this plugin is willing to write: thermal policy and power behaviour.
WRITABLE_SET = (
    "ThermalManagement",
    "PeakShiftCfg",
    "PeakShiftBatteryThreshold",
    "PrimaryBattChargeCfg",
    "CustomChargeStart",
    "CustomChargeStop",
)

# Attributes worth reporting but not worth writing from here: the tool already
# drives the same behaviour through MSR and HWP, and a BIOS default set behind its
# back would fight it. They are still printed, because a BIOS setting that
# contradicts the register is the reason a limit reads back and does nothing.
REPORT_ONLY = (
    "TurboMode",
    "SpeedShift",
    "Speedstep",
    "CStatesCtrl",
    "CpuCore",
    "PowerWarn",
    "AdvancedMode",
)

# Charging behaviour belongs to the battery, not to the CPU/iGPU budget, so it is
# reported by default and only written when asked explicitly.
BATTERY_SET = ("PrimaryBattChargeCfg", "CustomChargeStart", "CustomChargeStop")

# The four thermal modes Dell names, and what each one is called in this tool.
MODE_BY_PROFILE = {
    "cool": "Cool",
    "quiet": "Quiet",
    "balanced": "Optimized",
    "performance": "UltraPerformance",
}

DEVICE_PATHS = {
    "ThermalManagement": "firmware thermal mode, the knob Dell Power Manager drives",
    "PeakShiftCfg": "discharge on battery above the battery threshold",
    "PeakShiftBatteryThreshold": "battery percentage where peak shift starts",
    "PrimaryBattChargeCfg": "charge policy (Adaptive/Standard/Express/PrimAcUse/Custom)",
    "CustomChargeStart": "custom charge window start, percent",
    "CustomChargeStop": "custom charge window stop, percent",
    "TurboMode": "BIOS turbo enable, read only from here",
    "SpeedShift": "BIOS HWP enable, read only from here",
    "Speedstep": "BIOS SpeedStep enable, read only from here",
    "CStatesCtrl": "BIOS C-states enable, read only from here",
    "CpuCore": "cores the firmware is allowed to expose, read only from here",
    "PowerWarn": "BIOS power warning, read only from here",
    "AdvancedMode": "BIOS advanced mode, read only from here",
}

###############################################
# ENGINE
###############################################


def _attr_dir(name):
    return ATTRS / name


def _current(name):
    """Root-only in this kernel: a refusal is reported as a refusal, not as empty."""
    try:
        return ( _attr_dir(name) / "current_value").read_text().strip()
    except PermissionError:
        return "<root-only>"
    except OSError as exc:
        return f"<{type(exc).__name__}>"


def _possible(name):
    try:
        raw = (_attr_dir(name) / "possible_values").read_text().strip()
    except OSError:
        return []
    return [v for v in raw.split(";") if v]


def _kind(name):
    try:
        return (_attr_dir(name) / "type").read_text().strip()
    except OSError:
        return ""


class DellPlugin(Plugin):
    ID = "dell"
    NAME = "Dell firmware attributes (dell-wmi-sysman)"
    VENDOR = "Dell"
    WHY = ("Dell keeps part of the thermal and power policy in BIOS attributes, the same "
           "channel Dell Power Manager uses on Windows. It is firmware state, not a register.")

    def detect(self):
        if not ATTRS.is_dir():
            return False
        try:
            vendor = Path("/sys/class/dmi/id/sys_vendor").read_text().strip().lower()
        except OSError:
            vendor = ""
        return vendor.startswith("dell") or any(_attr_dir(n).is_dir() for n in ("ThermalManagement",))

    def sections(self):
        rows = []
        for name in WRITABLE_SET + REPORT_ONLY:
            if not _attr_dir(name).is_dir():
                continue
            value = _current(name)
            possible = "/".join(_possible(name)) or "-"
            rows.append((name, f"{value}    [{possible}]  {DEVICE_PATHS.get(name, '')}"))
        if not rows:
            return []
        note = ("current_value is root-only. This kernel publishes no is_readonly, so whether an "
                "attribute accepts a write is only proved by writing it and reading it back.")
        pending = ATTRS.parent / "pending_reboot"
        if pending.exists():
            note += " An attribute write can set pending_reboot: the value is stored by the firmware and applies on the next boot."
        return [Section(f"{self.NAME}", rows, note)]

    def channels(self):
        out = []
        for name in WRITABLE_SET:
            if not _attr_dir(name).is_dir():
                continue
            possible = _possible(name)
            if not possible:
                continue
            group = "Battery charging" if name in BATTERY_SET else "Dell firmware"
            out.append(Channel(
                key=f"fwa:{name}",
                label=f"BIOS {name}",
                target=f"@fwa:{VENDOR_DIR}:{name}",
                values=possible,
                why=DEVICE_PATHS.get(name, ""),
                group=group,
                role="thermal" if name == "ThermalManagement" else "",
            ))
        return out

    def thermal_map(self):
        """Only the modes the firmware actually offers on this machine."""
        possible = set(_possible("ThermalManagement"))
        return {profile: value for profile, value in MODE_BY_PROFILE.items() if value in possible}

    def capture(self):
        out = {}
        for name in WRITABLE_SET + REPORT_ONLY:
            if not _attr_dir(name).is_dir():
                continue
            value = _current(name)
            if value and not value.startswith("<"):
                out[name] = value
        return {"dell_firmware": out} if out else {}

    def restore(self, stock, dry=False):
        """Put every captured firmware attribute back. dry only reports."""
        saved = (stock.get("plugins") or {}).get(self.ID, {}).get("dell_firmware") or {}
        report = []
        for name, value in saved.items():
            if name not in WRITABLE_SET:
                continue
            now = _current(name)
            if now == value:
                report.append((True, f"{name}: already {value}"))
                continue
            if dry:
                report.append((True, f"{name}: {now} -> {value}"))
                continue
            ok, msg = self.ctx.write_sysfs(f"@fwa:{VENDOR_DIR}:{name}", value)
            report.append((ok, f"{name}: {now} -> {value} {'' if ok else msg}"))
        return report

    def warnings(self):
        out = []
        if self.thermal_map():
            out.append("the firmware thermal mode and the ACPI platform_profile are two different "
                       "channels to the same policy: the ACPI one is runtime, the BIOS attribute is "
                       "firmware state and can win before the kernel loads")
        if (ATTRS.parent / "pending_reboot").exists():
            out.append("pending_reboot is set: a firmware attribute is waiting for a reboot to apply")
        return out

    def report(self):
        data = super().report()
        data["attributes"] = {n: {"value": _current(n), "possible": _possible(n), "type": _kind(n)}
                              for n in WRITABLE_SET + REPORT_ONLY if _attr_dir(n).is_dir()}
        data["thermal_map"] = self.thermal_map()
        return data
