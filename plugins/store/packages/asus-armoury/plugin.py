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
ASUS ROG power tunables, through the firmware-attributes class.

The kernel driver `asus-armoury` publishes the WMI functions ASUS Armoury Crate drives on
Windows as /sys/class/firmware-attributes/asus-armoury/attributes/<name>/current_value.
That is the same channel ThrottleFed already writes for Dell, so this plugin needs no
vendor tool, no new privilege and no dependency on asusctl.

Four things are worth knowing before trusting a number here:

  * the tunables are kept per power source. The driver holds one set for AC and one for
    battery and answers with the set the machine is on right now, so the value read on
    battery is not the value read plugged in, and a write changes only the set in use;
  * this kernel publishes no min_value or max_value for the tunables, so the range is not
    readable from here even though the driver enforces it: an out of range value is
    refused with -EINVAL before the firmware sees it. A wrong number is a refusal, not a
    mistake on the machine, and the bounds are not invented to look helpful;
  * on some models every power limit write is refused until a custom fan curve is active.
    The driver returns -EBUSY and logs that the change requires one. If a write fails
    here, that is the first thing to check;
  * the driver marks gpu_mux_mode and panel_hd_mode as applying on the next boot, which is
    why they are reported instead of written from here. A firmware setting that only lands
    after a reboot is not a runtime knob.

Only power, thermal and panel-state attributes are exposed. The same directory carries
apu_mem (how much RAM the APU may take) and the lighting and AniMe surface belongs to
other tools entirely: neither is this tool's business.
"""

import platform
from pathlib import Path

from throttlefed_plugin_api import Channel, Plugin, Section

###############################################
# DEFINITIONS
###############################################

VENDOR_DIR = "asus-armoury"
ATTRS = Path("/sys/class/firmware-attributes") / VENDOR_DIR / "attributes"

# Package power limits and GPU power. Numbers, not lists: the driver validates them
# against the limits it carries for this model.
TUNE_SET = (
    "ppt_pl1_spl",
    "ppt_pl2_sppt",
    "ppt_pl3_fppt",
    "ppt_apu_sppt",
    "ppt_platform_sppt",
    "nv_dynamic_boost",
    "nv_temp_target",
    "nv_tgp",
)

# Firmware switches with a power or panel consequence. The driver writes these through
# the same WMI paths, values 0 and 1.
SWITCH_SET = (
    "dgpu_disable",
    "panel_od",
    "panel_hd_mode",
    "screen_auto_brightness",
    "boot_sound",
    "mcu_powersave",
)

# Read only in this kernel, or writable but with a consequence this plugin does not want
# to hand out as a one click: reported, never written.
REPORT_ONLY = (
    "charge_mode",
    "nv_base_tgp",
    "egpu_connected",
    "gpu_mux_mode",
    "mini_led_mode",
    "egpu_enable",
)

# What each attribute is, in the driver's own words where the driver has them. A name
# like ppt_pl2_sppt means nothing to a person; the kernel string does.
ATTRIBUTES = {
    "ppt_pl1_spl": "the CPU slow package limit (PL1, the sustained one)",
    "ppt_pl2_sppt": "the CPU fast package limit (PL2, the boost one)",
    "ppt_pl3_fppt": "the CPU fastest package limit (PL3, the short one)",
    "ppt_apu_sppt": "the APU package limit",
    "ppt_platform_sppt": "the platform package limit",
    "nv_dynamic_boost": "the Nvidia dynamic boost limit",
    "nv_temp_target": "the Nvidia maximum thermal limit",
    "nv_tgp": "the additional TGP on top of the base TGP",
    "nv_base_tgp": "the base TGP, read only",
    "dgpu_disable": "the discrete GPU, off when 1",
    "panel_od": "panel refresh overdrive, off when 0",
    "panel_hd_mode": "panel mode, UHD when 0 and FHD when 1",
    "screen_auto_brightness": "panel brightness by the panel itself, off when 0, on when 1",
    "boot_sound": "the POST boot sound, off when 0, on when 1",
    "mcu_powersave": "MCU powersaving mode, off when 0, on when 1",
    "charge_mode": "the charging mode, read only in this kernel, one of 0;1;2",
    "egpu_connected": "eGPU connection status, read only",
    "gpu_mux_mode": "the GPU display MUX mode, applied on the next boot",
    "mini_led_mode": "the mini-LED backlight mode, reported here; the LED class drives it",
    "egpu_enable": "the eGPU switch, which also disables the dGPU",
}

# Where asusctl shows a control, and where the same thing lives here. This is the list a
# person migrating from that app goes looking for first.
ASUSCTL_EQUIVALENT = (
    ("Battery charge thresholds",
     "reported only: this kernel marks charge_mode read only, so there is nothing to write"),
    ("Power profile management (performance profiles)",
     "the platform_profile channel the core already writes"),
    ("PPT sliders (the package power limits)",
     "channels fwa:ppt_pl1_spl, fwa:ppt_pl2_sppt, fwa:ppt_pl3_fppt, fwa:ppt_apu_sppt, fwa:ppt_platform_sppt"),
    ("Detailed GPU power (dynamic boost, TGP, thermal target)",
     "channels fwa:nv_dynamic_boost, fwa:nv_tgp, fwa:nv_temp_target; nv_base_tgp is read only"),
    ("GPU MUX toggling and the eGPU switch",
     "reported only: the driver marks them as applying on the next boot, or as disabling the dGPU"),
    ("Custom fan curves",
     "not here: ThrottleFed drives the fan policy through platform_profile, while asusctl writes fan curves the firmware keeps"),
    ("Keyboard lighting, per key RGB, AniMe Matrix, POST audio",
     "the lighting is not a power surface; boot_sound is offered as a switch"),
)

NOT_HERE = (
    ("apu_mem (how much system RAM the APU may take)",
     "it sits in the same directory and it is a memory split, not a power setting, so this plugin leaves it alone"),
    ("Anything that needs asusd or a D-Bus client",
     "the kernel already publishes the values, so no daemon is needed for these knobs"),
    ("A range to aim at for the power limits",
     "the driver carries the limits for the model and refuses what is outside them. Guessing them here would be inventing numbers"),
)

###############################################
# ENGINE
###############################################


def _attr_dir(name):
    return ATTRS / name


def _current(name):
    """Read as this user, and say root-only instead of pretending to be empty."""
    try:
        return (_attr_dir(name) / "current_value").read_text().strip()
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


def _bound(name, which):
    """The bounds only exist where this kernel publishes them, which for the power
    limits it does not. None means unknown, and unknown is not filled in."""
    try:
        return int((_attr_dir(name) / which).read_text().strip())
    except (OSError, ValueError):
        return None


def _on_ac():
    """True on AC, False on battery, None when the machine does not say. The power
    limits are kept per source, so which one is in use is part of the value."""
    for supply in sorted(Path("/sys/class/power_supply").glob("*")):
        try:
            if (supply / "type").read_text().strip() == "Mains":
                return (supply / "online").read_text().strip() == "1"
        except OSError:
            continue
    return None


class AsusArmouryPlugin(Plugin):
    ID = "asus-armoury"
    NAME = "ASUS firmware tunables (asus-armoury)"
    TAB = "Asus"
    VENDOR = "ASUS"
    WHY = ("ASUS keeps the package power limits, the GPU power and the panel state as firmware "
           "attributes: the same channel Armoury Crate drives on Windows. They are firmware "
           "state, kept per power source, not registers.")
    # Who made the thing this plugin is a compatibility layer for, and who made the channel
    # it goes through. Shown on the store row and on this plugin's own tab, one expander each.
    CREDITS = (
        {
            "name": "asusctl (asusd, rog-control-center)",
            "creator": "Luke Jones and the asus-linux project, now the Open Gaming Collective",
            "repo": "https://github.com/OpenGamingCollective/asusctl",
            "license": "MPL-2.0",
            "reuse": ("which firmware knobs matter on a ROG machine and what each one is for: the "
                      "power limits, the GPU power, the panel modes, the POST sound"),
            "note": ("This plugin is not a wrapper around it and no code was copied. asusctl is a "
                     "daemon with a D-Bus client; this plugin writes the attributes the kernel "
                     "publishes, so none of it has to be installed."),
        },
        {
            "name": "asus-armoury (the Linux kernel driver)",
            "creator": "Luke Jones, in the mainline kernel",
            "repo": "https://github.com/torvalds/linux/blob/master/drivers/platform/x86/asus-armoury.c",
            "license": "GPL-2.0-or-later",
            "reuse": ("the attribute names, what each one means, and the fact that the driver "
                      "checks a value against the limits it carries before the firmware sees it"),
            "note": ("Without this driver the attributes do not exist, and this plugin reports "
                     "nothing rather than guessing. The LED and fan curve surfaces are not part "
                     "of it."),
        },
        {
            "name": "ASUS Armoury Crate",
            "creator": "ASUSTeK Computer Inc.",
            "repo": "https://rog.asus.com/content/armoury-crate/",
            "license": "proprietary, ASUS's own, Windows only",
            "reuse": ("only the knowledge that these knobs exist in the firmware and what they are "
                      "called. The values are the firmware's, not the application's"),
            "note": ("Nothing from it is used, packaged or required here, and it is not the reason "
                     "the knobs work: the kernel driver is."),
        },
    )

    def detect(self):
        if not ATTRS.is_dir():
            return False
        try:
            vendor = Path("/sys/class/dmi/id/sys_vendor").read_text().strip().lower()
        except OSError:
            vendor = ""
        return vendor.startswith("asus") or any(_attr_dir(n).is_dir() for n in TUNE_SET)

    def sections(self):
        rows = []
        for name in TUNE_SET + SWITCH_SET + REPORT_ONLY:
            if not _attr_dir(name).is_dir():
                continue
            lo, hi = _bound(name, "min_value"), _bound(name, "max_value")
            if lo is not None and hi is not None:
                rng = f"    [{lo}..{hi}]"
            elif _possible(name):
                rng = "    [" + "/".join(_possible(name)) + "]"
            else:
                rng = ""
            rows.append((name, f"{_current(name)}{rng}  {ATTRIBUTES.get(name, '')}"))
        out = []
        if rows:
            note = ("Values are read as this user. The kernel publishes no range for the power "
                    "limits, so bounds appear above only where the machine offers them; the driver "
                    "refuses a value it does not accept before the firmware sees it.")
            if (ATTRS.parent / "pending_reboot").exists():
                note += (" pending_reboot is set: a firmware attribute is waiting for a reboot to "
                         "apply.")
            out.append(Section(self.NAME, rows, note))
        out.append(Section(
            "Compatibility: asusctl",
            list(ASUSCTL_EQUIVALENT) + list(NOT_HERE),
            "asusctl is a daemon plus a D-Bus client for these same firmware knobs. This plugin "
            "writes the attributes the kernel already publishes, so it needs no daemon, no new "
            "privilege and no vendor tool.",
        ))
        return out

    def channels(self):
        out = []
        for name in TUNE_SET:
            if not _attr_dir(name).is_dir():
                continue
            out.append(Channel(
                key=f"fwa:{name}",
                label=f"BIOS {name}",
                target=f"@fwa:{VENDOR_DIR}:{name}",
                why=ATTRIBUTES.get(name, ""),
                group="ASUS power tunables",
                number=True,
                lo=_bound(name, "min_value"),
                hi=_bound(name, "max_value"),
            ))
        for name in SWITCH_SET:
            if not _attr_dir(name).is_dir():
                continue
            out.append(Channel(
                key=f"fwa:{name}",
                label=f"BIOS {name}",
                target=f"@fwa:{VENDOR_DIR}:{name}",
                values=("0", "1"),
                why=ATTRIBUTES.get(name, ""),
                group="ASUS firmware switches",
            ))
        return out

    def capture(self):
        out = {}
        for name in TUNE_SET + SWITCH_SET + REPORT_ONLY:
            if not _attr_dir(name).is_dir():
                continue
            value = _current(name)
            if value and not value.startswith("<"):
                out[name] = value
        if not out:
            return {}
        on_ac = _on_ac()
        source = "unknown" if on_ac is None else ("AC" if on_ac else "battery")
        return {"asus_firmware": out, "source": source}

    def restore(self, stock, dry=False):
        """Put the captured attributes back. dry only reports.

        The power limits exist once per power source, so this says which source the
        captured numbers came from when the machine is on the other one.
        """
        mine = (stock.get("plugins") or {}).get(self.ID, {})
        saved = mine.get("asus_firmware") or {}
        captured_on = mine.get("source") or ""
        report = []
        on_ac = _on_ac()
        if captured_on in ("AC", "battery") and on_ac is not None:
            now_on = "AC" if on_ac else "battery"
            if now_on != captured_on:
                report.append((True, f"these values were captured on {captured_on} and this machine "
                                     f"is on {now_on}: the driver keeps one set per source, so what "
                                     f"follows puts back the {captured_on} set"))
        for name, value in saved.items():
            if name not in TUNE_SET + SWITCH_SET:
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
        tunables = any(_attr_dir(n).is_dir() for n in TUNE_SET)
        if tunables:
            out.append("the power limits are kept per power source: what is shown is the set for the "
                       "source the machine is on right now, and a write changes only that set")
        if _attr_dir("gpu_mux_mode").is_dir() or _attr_dir("panel_hd_mode").is_dir():
            out.append("gpu_mux_mode and panel_hd_mode are marked by the driver as applying on the "
                       "next boot")
        if tunables:
            out.append("on some models a power limit write is refused until a custom fan curve is "
                       "active: the driver answers -EBUSY and says so in the kernel log")
        if (ATTRS.parent / "pending_reboot").exists():
            out.append("pending_reboot is set: a firmware attribute is waiting for a reboot to apply")
        return out

    def report(self):
        data = super().report()
        data["attributes"] = {n: {"value": _current(n), "possible": _possible(n), "type": _kind(n),
                                  "min": _bound(n, "min_value"), "max": _bound(n, "max_value")}
                              for n in TUNE_SET + SWITCH_SET + REPORT_ONLY if _attr_dir(n).is_dir()}
        on_ac = _on_ac()
        data["source_name"] = "unknown" if on_ac is None else ("AC" if on_ac else "battery")
        data["kernel"] = platform.release()
        return data
