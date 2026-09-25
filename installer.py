#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""ThrottleFed setup wizard (GTK4 + libadwaita).

Five pages: welcome, system checks, options, install with a live log, and a
summary of what was installed plus how to revert it. The wizard never runs as
root and never writes to the system: it asks installer-backend.sh to do the
work, through pkexec, and parses the records the backend prints.

Run it through setup.sh, or directly with the distribution Python:
    /usr/bin/python3 installer.py [--demo] [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("Gdk", "4.0")
    from gi.repository import Adw, Gdk, Gio, GLib, Gtk
except (ImportError, ValueError) as exc:
    print(
        f"GTK4 and libadwaita for the system Python are required ({exc}).\n"
        "Fedora: sudo dnf install python3-gobject gtk4 libadwaita\n"
        "Debian: sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1",
        file=sys.stderr,
    )
    raise SystemExit(1)

# ---------------------------------------------------------------------------
# definitions
# ---------------------------------------------------------------------------
APP_ID = "io.github.framet.ThrottleFed.Installer"
WINDOW_TITLE = "ThrottleFed Setup"

HERE = Path(__file__).resolve().parent
BACKEND = HERE / "installer-backend.sh"
BRAND_ICON = HERE / "data/io.github.framet.ThrottleFed.svg"

SYSTEM_PYTHON = "/usr/bin/python3"
INSTALL_DIR = Path("/usr/local/share/throttlefed")
CLI_FILE = INSTALL_DIR / "throttlefed.py"
HELPER = Path("/usr/local/libexec/throttlefed-helper")
POLICY = Path("/usr/share/polkit-1/actions/io.github.framet.throttlefed.policy")
LAUNCHER = Path("/usr/local/bin/throttlefed-gui")
DESKTOP_FILE = Path("/usr/local/share/applications/io.github.framet.ThrottleFed.desktop")
ICON_FILE = Path("/usr/local/share/icons/hicolor/scalable/apps/io.github.framet.ThrottleFed.svg")
STATE_DIR = Path("/var/lib/throttlefed")
STOCK_FILE = STATE_DIR / "stock.conf"
FONT_DIR = Path("/usr/local/share/fonts/throttlefed")
FONT_FILES = ("GoogleSansFlex-Bold.ttf", "GoogleSansFlex-Medium.ttf")

CPUINFO = Path("/proc/cpuinfo")
OS_RELEASE = Path("/etc/os-release")
RAPL_ZONE = Path("/sys/class/powercap/intel-rapl:0")
PSTATE_DIR = Path("/sys/devices/system/cpu/intel_pstate")
DRM_DIR = Path("/sys/class/drm")
PLATFORM_PROFILE = Path("/sys/firmware/acpi/platform_profile")
MSR_DEVICE = Path("/dev/cpu/0/msr")
MSR_MODULE = Path("/sys/module/msr")

CHECKOUT_FILES = (
    "throttlefed.py",
    "throttlefed_gui.py",
    "throttlefed_store.py",
    "plugins/base.py",
    "plugins/store/catalog.json",
    "installer-backend.sh",
    "src/throttlefed-helper.c",
    "data/io.github.framet.throttlefed.policy",
    "data/io.github.framet.ThrottleFed.desktop",
    "data/io.github.framet.ThrottleFed.svg",
    "assets/fonts/GoogleSansFlex-Bold.ttf",
    "assets/fonts/GoogleSansFlex-Medium.ttf",
)

PY_STACK_PROBE = (
    'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); '
    "from gi.repository import Gtk, Adw; "
    'print(f"GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}, '
    'libadwaita {Adw.MAJOR_VERSION}.{Adw.MINOR_VERSION}")'
)

PAGE_NAMES = ("welcome", "checks", "options", "install", "completed")

# Demo walkthrough: the same records the backend prints, so the progress bar,
# the phase label and the log behave like a real run without touching anything.
DEMO_STEPS = (
    (5, "checking what the install needs"),
    (15, "compiling the privileged helper (gcc -O2 -Wall -Wextra)"),
    (35, "helper -> /usr/local/libexec/throttlefed-helper (0755 root:root)"),
    (45, "polkit policy -> /usr/share/polkit-1/actions/io.github.framet.throttlefed.policy (0644)"),
    (55, "command line tool and GUI -> /usr/local/share/throttlefed (0644)"),
    (65, "launcher -> /usr/local/bin/throttlefed (0755)"),
    (75, "desktop entry and icon in the hicolor theme (0644)"),
    (82, "state directory -> /var/lib/throttlefed (0755)"),
    (88, "capturing the current state as the restore target"),
    (92, "boot service and 60 s drift timer"),
    (98, "verifying the installed files"),
    (100, "done"),
)
DEMO_DELAY_SECONDS = 0.35


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    blocking: bool = False


# ---------------------------------------------------------------------------
# engine: distribution helpers
# ---------------------------------------------------------------------------
def read_first_line(path: Path) -> str | None:
    try:
        with open(path, "r", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    return line
    except OSError:
        return None
    return None


def os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = OS_RELEASE.read_text(errors="replace")
    except OSError:
        return values
    for line in text.splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"')
    return values


def install_hint(packages: dict[str, str]) -> str:
    """Turn package names into the exact command for the running distribution."""
    family = os_release().get("ID", "")
    if family in ("fedora", "rhel", "centos", "rocky", "almalinux"):
        command, names = "sudo dnf install", packages.get("fedora", "")
    elif family in ("debian", "ubuntu", "linuxmint", "pop", "zorin", "kali"):
        command, names = "sudo apt install", packages.get("debian", "")
    elif family in ("arch", "manjaro", "endeavouros"):
        command = "sudo pacman -S"
        names = packages.get("arch", packages.get("debian", ""))
    else:
        return f"install the {packages.get('fedora', '')} package"
    return f"{command} {names}".strip()


def run_probe(argv: list[str], timeout: int = 30) -> tuple[int, str]:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, env=env
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return done.returncode, (done.stdout + done.stderr).strip()


# ---------------------------------------------------------------------------
# engine: the checks shown on page 2, all read-only and without root
# ---------------------------------------------------------------------------
def check_intel_platform() -> Check:
    vendor = ""
    model = ""
    try:
        for line in CPUINFO.read_text(errors="replace").splitlines():
            if not vendor and line.startswith("vendor_id"):
                vendor = line.partition(":")[2].strip()
            elif not model and line.startswith("model name"):
                model = line.partition(":")[2].strip()
            if vendor and model:
                break
    except OSError as exc:
        return Check("Intel platform", False, f"{CPUINFO} is unreadable ({exc})", True)
    if vendor == "GenuineIntel":
        return Check("Intel platform", True, f"{vendor}, {model or 'unknown model'}")
    return Check(
        "Intel platform",
        False,
        f"vendor_id is {vendor or 'unknown'}: RAPL, MSR and HWP need an Intel CPU",
        True,
    )


def check_rapl_zone() -> Check:
    if not RAPL_ZONE.is_dir():
        return Check(
            "RAPL powercap zone",
            False,
            f"{RAPL_ZONE} is missing: PL1 and PL2 cannot be read or written "
            "without the powercap driver",
        )
    name = read_first_line(RAPL_ZONE / "name") or RAPL_ZONE.name
    return Check("RAPL powercap zone", True, f"{RAPL_ZONE} present ({name})")


def check_hwp() -> Check:
    status = read_first_line(PSTATE_DIR / "status")
    if status is None:
        return Check(
            "HWP / intel_pstate",
            False,
            "intel_pstate is not the active cpufreq driver: EPP and the Speed "
            "Shift hints need it (intel_pstate=active on the kernel command line)",
        )
    return Check("HWP / intel_pstate", True, f"intel_pstate status: {status}")


def check_i915_rps() -> Check:
    for card in sorted(DRM_DIR.glob("card[0-9]*")):
        knob = card / "gt_min_freq_mhz"
        if not knob.exists():
            continue
        driver_link = card / "device" / "driver"
        driver = os.path.basename(os.path.realpath(driver_link)) if driver_link.exists() else "unknown"
        now = read_first_line(card / "gt_cur_freq_mhz") or "?"
        low = read_first_line(knob) or "?"
        high = read_first_line(card / "gt_max_freq_mhz") or "?"
        detail = f"{knob} (driver {driver}), {now} MHz now, min {low} / max {high}"
        if driver == "i915":
            return Check("i915 RPS", True, detail)
        return Check("i915 RPS", False, f"{detail}: the iGPU budget needs the i915 driver")
    return Check(
        "i915 RPS",
        False,
        "no gt_min_freq_mhz under /sys/class/drm/card*: the iGPU budget needs "
        "the i915 driver with RPS enabled",
    )


def check_platform_profile() -> Check:
    if not PLATFORM_PROFILE.exists():
        return Check(
            "ACPI platform profile",
            False,
            f"{PLATFORM_PROFILE} is missing: the thermal modes (quiet, cool, "
            "performance) need the ACPI platform profile driver",
        )
    current = read_first_line(PLATFORM_PROFILE) or "?"
    choices = read_first_line(PLATFORM_PROFILE.parent / "platform_profile_choices") or "?"
    return Check("ACPI platform profile", True, f"{current} (available: {choices})")


def check_msr() -> Check:
    if not MSR_MODULE.is_dir():
        return Check(
            "MSR access",
            False,
            "the msr module is not loaded: sudo modprobe msr, or write msr into "
            "/etc/modules-load.d/throttlefed.conf to keep it across boots",
        )
    if not MSR_DEVICE.exists():
        return Check(
            "MSR access",
            False,
            f"the msr module is loaded but {MSR_DEVICE} is missing: the locked "
            "MSRs (turbo caps, EPP) stay unreadable",
        )
    readable = os.access(MSR_DEVICE, os.R_OK)
    note = (
        "readable here"
        if readable
        else "mode is root only, which is what the privileged helper expects"
    )
    return Check("MSR access", True, f"{MSR_DEVICE} present, msr module loaded ({note})")


def check_gcc() -> Check:
    gcc = shutil.which("gcc")
    if gcc is None:
        return Check(
            "C compiler",
            False,
            "gcc is required to build the privileged helper "
            f"({install_hint({'fedora': 'gcc', 'debian': 'gcc', 'arch': 'gcc'})})",
            True,
        )
    code, out = run_probe([gcc, "--version"], timeout=15)
    version = out.splitlines()[0] if out else "unknown version"
    return Check("C compiler", code == 0, f"{gcc}: {version}")


def check_systemd() -> Check:
    if not Path("/run/systemd/system").is_dir() or shutil.which("systemctl") is None:
        return Check(
            "systemd",
            False,
            "systemd is not running this session: the boot service and the 60 s "
            "timer cannot be installed (the app itself still works)",
        )
    code, out = run_probe(["systemctl", "--version"], timeout=15)
    version = out.splitlines()[0] if out else "unknown version"
    return Check("systemd", code == 0, version)


def check_python_stack() -> Check:
    if not Path(SYSTEM_PYTHON).exists():
        return Check(
            "System Python stack",
            False,
            f"{SYSTEM_PYTHON} is missing: the launcher and the GUI run on the "
            "distribution Python",
            True,
        )
    code, out = run_probe([SYSTEM_PYTHON, "-c", PY_STACK_PROBE])
    if code == 0:
        version = run_probe([SYSTEM_PYTHON, "-V"], timeout=15)[1] or SYSTEM_PYTHON
        return Check("System Python stack", True, f"{version} with {out}")
    packages = {
        "fedora": "python3-gobject gtk4 libadwaita",
        "debian": "python3-gi gir1.2-gtk-4.0 gir1.2-adw-1",
        "arch": "python-gobject gtk4 libadwaita",
    }
    reason = out.splitlines()[-1] if out else "unknown import error"
    return Check(
        "System Python stack",
        False,
        f"{SYSTEM_PYTHON} has no GTK4/libadwaita ({install_hint(packages)}), so "
        f"the GUI and the launcher cannot run: {reason}",
    )


def check_checkout() -> Check:
    missing = [name for name in CHECKOUT_FILES if not (HERE / name).exists()]
    if missing:
        return Check(
            "Installer files",
            False,
            f"missing next to installer.py: {', '.join(missing)}",
            True,
        )
    return Check("Installer files", True, f"complete checkout at {HERE}")


def check_distribution() -> Check:
    release = os_release()
    name = release.get("PRETTY_NAME") or release.get("ID") or "unknown distribution"
    return Check("Distribution", True, f"{name} [{release.get('ID', 'unknown')}]")


def probe_checks() -> list[Check]:
    return [
        check_intel_platform(),
        check_rapl_zone(),
        check_hwp(),
        check_i915_rps(),
        check_platform_profile(),
        check_msr(),
        check_gcc(),
        check_systemd(),
        check_python_stack(),
        check_checkout(),
        check_distribution(),
    ]


# ---------------------------------------------------------------------------
# engine: running the backend
# ---------------------------------------------------------------------------
def backend_argv(mode: str, flags: list[str], dry_run: bool) -> tuple[list[str], str]:
    """Build the command line, asking for root only when a write is coming.

    The source directory travels as an argument because pkexec starts the
    backend with a clean environment and an unknown working directory.
    """
    base = [str(BACKEND), mode, "--source", str(HERE), *flags]
    if dry_run:
        return base, ""
    if shutil.which("pkexec"):
        return ["pkexec", *base], ""
    if shutil.which("sudo"):
        return ["sudo", *base], "pkexec is missing: falling back to sudo, which asks on the terminal"
    return [], "neither pkexec nor sudo is available on this system"


class BackendJob:
    """Runs installer-backend.sh and hands every record to the UI thread."""

    def __init__(self, argv: list[str], on_record, on_finished):
        self.argv = argv
        self._on_record = on_record
        self._on_finished = on_finished
        self._process: subprocess.Popen | None = None
        self._cancelled = False

    def start(self) -> None:
        # Its own session: cancelling kills this process group, so the pkexec
        # prompt and whatever it started go away and the wizard stays alive.
        self._process = subprocess.Popen(
            self.argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        for line in process.stdout:
            GLib.idle_add(self._on_record, line.rstrip("\n"))
        code = process.wait()
        GLib.idle_add(self._on_finished, code, self._cancelled)

    def cancel(self) -> None:
        self._cancelled = True
        if self._process is None or self._process.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(self._process.pid), signal.SIGTERM)
        except OSError:
            self._process.terminate()


# ---------------------------------------------------------------------------
# engine: window
# ---------------------------------------------------------------------------
class SetupWindow(Adw.ApplicationWindow):
    def __init__(self, application, demo: bool = False, dry_run: bool = False):
        super().__init__(application=application)
        self.demo = demo
        self.dry_run = dry_run or demo
        self.state = "idle"
        self.job: BackendJob | None = None
        self.current = 0
        self.saw_result = False
        self.result_ok = False
        self.result_text = ""
        self.summary_filled = False

        self.set_title(WINDOW_TITLE)
        self.set_default_size(910, 660)

        self.checks = probe_checks()

        self.carousel = Adw.Carousel()
        self.carousel.set_allow_mouse_drag(False)
        self.carousel.set_allow_scroll_wheel(False)
        self.carousel.set_allow_long_swipes(False)
        self.pages = [
            self._build_welcome(),
            self._build_checks(),
            self._build_options(),
            self._build_install(),
            self._build_completed(),
        ]
        for page in self.pages:
            # libadwaita 1.9 sizes a carousel page by its natural width unless the page
            # expands, which collapses every step into a narrow strip at the left edge.
            page.set_hexpand(True)
            self.carousel.append(page)

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=WINDOW_TITLE))

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self.carousel)
        toolbar.add_bottom_bar(self._build_footer())

        overlay = Adw.ToastOverlay()
        overlay.set_child(toolbar)
        self.set_content(overlay)
        self.connect("close-request", self._on_close_request)

        self._sync()
        GLib.idle_add(self._first_page)

    # ------------------------------------------------------------------ pages
    def _scrolled_page(self, *children):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=26)
        box.set_margin_top(28)
        box.set_margin_bottom(28)
        box.set_margin_start(20)
        box.set_margin_end(20)
        for child in children:
            box.append(child)
        clamp = Adw.Clamp()
        clamp.set_maximum_size(780)
        clamp.set_child(box)
        viewport = Gtk.ScrolledWindow()
        viewport.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        viewport.set_child(clamp)
        return viewport

    def _brand_group(self):
        group = Adw.PreferencesGroup()
        group.add(
            self._info_row(
                "Command line tool",
                f"{LAUNCHER} opens the GTK4 interface.",
            )
        )
        group.add(
            self._info_row(
                "Privileged helper",
                f"{HELPER}, a small C program that performs the writes as root "
                "and only when you ask.",
            )
        )
        group.add(
            self._info_row(
                "Nothing else runs as root",
                "The wizard and the GUI stay your normal user processes. The "
                "password is asked once, through polkit, when the install starts.",
            )
        )
        return group

    def _info_row(self, title: str, subtitle: str) -> Adw.ActionRow:
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        row.set_activatable(False)
        row.set_subtitle_selectable(True)
        return row

    def _build_welcome(self):
        page = Adw.StatusPage()
        page.set_title("Set up ThrottleFed")
        page.set_description(
            "This wizard installs the power budget tool for Intel laptops: the "
            "command line tool, the privileged helper and, if you want, the "
            "desktop application and the boot service."
        )
        # libadwaita < 1.7 has no set_paintable on StatusPage.
        if BRAND_ICON.exists():
            try:
                page.set_paintable(Gtk.Image.new_from_file(str(BRAND_ICON)).get_paintable())
            except (AttributeError, TypeError):
                page.set_icon_name("battery-good-symbolic")
        else:
            page.set_icon_name("battery-good-symbolic")
        page.set_child(self._brand_group())
        return page

    def _build_checks(self):
        group = Adw.PreferencesGroup(
            title="System checks",
            description="Everything here is read-only. A missing item is listed "
            "with the exact fix, and the rest of the install still goes ahead.",
        )
        for check in self.checks:
            row = Adw.ActionRow(title=check.name, subtitle=check.detail)
            row.set_activatable(False)
            row.set_subtitle_selectable(True)
            icon = Gtk.Image.new_from_icon_name(
                "object-select-symbolic" if check.ok else "dialog-error-symbolic"
            )
            icon.add_css_class("success" if check.ok else "error")
            row.add_prefix(icon)
            group.add(row)

        self.checks_hint = Gtk.Label(xalign=0, wrap=True)
        self.checks_hint.add_css_class("dim-label")
        self.checks_hint.set_visible(False)
        return self._scrolled_page(group, self.checks_hint)

    def _build_options(self):
        apps = next((c for c in self.checks if c.name == "System Python stack"), None)
        systemd = next((c for c in self.checks if c.name == "systemd"), None)

        self.gui_switch = Adw.SwitchRow(
            title="Desktop application",
            subtitle="The GTK4 interface, the launcher, the menu entry and the icon.",
            active=True,
        )
        if apps is not None and not apps.ok:
            self.gui_switch.set_active(False)
            self.gui_switch.set_sensitive(False)
            self.gui_switch.set_subtitle(f"Unavailable: {apps.detail}")

        self.service_switch = Adw.SwitchRow(
            title="Boot service and 60 s timer",
            subtitle="Reapplies the saved limits after a reboot and guards "
            "against firmware drift.",
            active=True,
        )
        if systemd is not None and not systemd.ok:
            self.service_switch.set_active(False)
            self.service_switch.set_sensitive(False)
            self.service_switch.set_subtitle(f"Unavailable: {systemd.detail}")

        stock_exists = STOCK_FILE.exists()
        self.stock_switch = Adw.SwitchRow(
            title="Capture the current state as the restore target",
            subtitle=(
                f"{STOCK_FILE} already exists and is kept: the installer never "
                "overwrites a factory snapshot."
                if stock_exists
                else "Saves today's PL1, PL2, EPP and thermal mode into "
                f"{STOCK_FILE}, so Restore stock has something to go back to."
            ),
            active=not stock_exists,
        )
        if stock_exists:
            self.stock_switch.set_sensitive(False)

        components = Adw.PreferencesGroup(
            title="Components",
            description="The command line tool and the privileged helper are "
            "always installed.",
        )
        components.add(self.gui_switch)
        components.add(self.service_switch)
        components.add(self.stock_switch)

        authentication = Adw.PreferencesGroup(
            title="Authentication",
            description="Only installer-backend.sh is granted root, through polkit.",
        )
        authentication.add(
            self._info_row(
                "The password is requested once",
                "Nothing is written before you accept that prompt, and closing "
                "it leaves the system untouched.",
            )
        )
        if self.dry_run:
            authentication.add(
                self._info_row(
                    "Dry run",
                    "The backend runs with --dry-run and prints what it would do. "
                    "No file is created and no password is asked.",
                )
            )
        return self._scrolled_page(components, authentication)

    def _build_install(self):
        self.phase_label = Gtk.Label(label="Waiting", xalign=0, wrap=True)
        self.phase_label.add_css_class("heading")
        self.progress = Gtk.ProgressBar(show_text=True)
        self.progress.set_fraction(0.0)

        self.log_buffer = Gtk.TextBuffer()
        self.log_view = Gtk.TextView(buffer=self.log_buffer)
        self.log_view.set_editable(False)
        self.log_view.set_cursor_visible(False)
        self.log_view.set_monospace(True)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_view.set_top_margin(8)
        self.log_view.set_bottom_margin(8)
        self.log_view.set_left_margin(8)
        self.log_view.set_right_margin(8)

        log_scroller = Gtk.ScrolledWindow()
        log_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        log_scroller.set_min_content_height(320)
        log_scroller.add_css_class("card")
        log_scroller.set_child(self.log_view)

        caption = Gtk.Label(label="Installation log", xalign=0)
        caption.add_css_class("dim-label")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.append(self.phase_label)
        box.append(self.progress)
        box.append(caption)
        box.append(log_scroller)
        return self._scrolled_page(box)

    def _build_completed(self):
        page = Adw.StatusPage()
        page.set_title("Installation complete")
        page.set_icon_name("object-select-symbolic")
        page.set_description(
            "ThrottleFed is installed. Open it from the application menu or with "
            f"{LAUNCHER} (GUI)."
        )

        self.open_button = Gtk.Button(label="Open ThrottleFed")
        self.open_button.add_css_class("suggested-action")
        self.open_button.set_halign(Gtk.Align.CENTER)
        self.open_button.connect("clicked", self._on_open_clicked)

        self.summary_group = Adw.PreferencesGroup(title="Installed")
        self.revert_group = Adw.PreferencesGroup(
            title="How to revert",
            description="Nothing here is hidden: the same paths are listed by "
            "installer-backend.sh status.",
        )
        page.set_child(self._scrolled_page(self.summary_group, self.open_button, self.revert_group))
        return page

    def _build_footer(self):
        self.cancel_button = Gtk.Button(label="Quit")
        self.cancel_button.connect("clicked", self._on_cancel_clicked)

        self.back_button = Gtk.Button(label="Back")
        self.back_button.connect("clicked", lambda *_: self._go(self.current - 1))

        self.primary_button = Gtk.Button(label="Continue")
        self.primary_button.add_css_class("suggested-action")
        self.primary_button.connect("clicked", self._on_primary_clicked)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        controls.append(self.back_button)
        controls.append(self.primary_button)

        self.dots = Adw.CarouselIndicatorDots()
        self.dots.set_carousel(self.carousel)

        footer = Gtk.CenterBox()
        footer.set_margin_top(10)
        footer.set_margin_bottom(10)
        footer.set_margin_start(20)
        footer.set_margin_end(20)
        footer.set_start_widget(self.cancel_button)
        footer.set_center_widget(self.dots)
        footer.set_end_widget(controls)
        return footer

    # ------------------------------------------------------------------- flow
    def _blocking_failures(self) -> list[Check]:
        return [check for check in self.checks if check.blocking and not check.ok]

    def _go(self, index: int) -> None:
        index = max(0, min(index, len(self.pages) - 1))
        self.current = index
        self.carousel.scroll_to(self.pages[index], True)
        self._sync()

    def _first_page(self):
        seconds = os.environ.get("INSTALLER_TEST_AUTOCLOSE", "")
        if seconds.isdigit() and int(seconds) > 0:
            GLib.timeout_add(int(seconds) * 1000, self._quit)
        if not self.demo:
            return GLib.SOURCE_REMOVE
        wanted = os.environ.get("INSTALLER_DEMO_PAGE", "")
        if wanted in PAGE_NAMES:
            index = PAGE_NAMES.index(wanted)
            if index == 4:
                self._fill_summary()
            self._go(index)
            if index == 3:
                self._start_install()
        if os.environ.get("INSTALLER_DEMO_AUTORUN") == "1":
            self._go(3)
            self._start_install()
        return GLib.SOURCE_REMOVE

    def _quit(self):
        self.close()
        return GLib.SOURCE_REMOVE

    def _sync(self) -> None:
        page = PAGE_NAMES[self.current]
        running = self.state == "running"
        failed = self.state == "failed"

        self.back_button.set_visible(self.current in (1, 2) and not running)
        self.cancel_button.set_label("Cancel installation" if page == "install" and running else "Quit")
        self.cancel_button.set_sensitive(running if page == "install" else True)
        self.primary_button.set_visible(not (page == "install" and not failed))

        if page == "welcome":
            self.primary_button.set_label("Continue")
            self.primary_button.set_sensitive(True)
        elif page == "checks":
            blocked = self._blocking_failures()
            self.checks_hint.set_visible(bool(blocked))
            if blocked:
                names = ", ".join(check.name for check in blocked)
                self.checks_hint.set_text(
                    f"Required and missing: {names}. Install the packages named "
                    "above and start the wizard again."
                )
            self.primary_button.set_label("Continue")
            self.primary_button.set_sensitive(not blocked)
        elif page == "options":
            self.primary_button.set_label("Preview the plan" if self.dry_run else "Install")
            self.primary_button.set_sensitive(True)
        elif page == "install":
            self.primary_button.set_label("Try again")
            self.primary_button.set_sensitive(failed)
        else:
            self.primary_button.set_label("Close")
            self.primary_button.set_sensitive(True)

    def _on_primary_clicked(self, _button) -> None:
        page = PAGE_NAMES[self.current]
        if page == "welcome":
            self._go(1)
        elif page == "checks":
            self._go(2)
        elif page == "options":
            self._go(3)
            self._start_install()
        elif page == "install":
            self._start_install()
        else:
            self.close()

    def _on_cancel_clicked(self, _button) -> None:
        if self.state == "running" and self.job is not None:
            self._append("--- cancelling the install")
            self.job.cancel()
            return
        self.close()

    def _on_close_request(self, *_args) -> bool:
        if self.job is not None:
            self.job.cancel()
        return False

    def _on_open_clicked(self, _button) -> None:
        if self.demo or not LAUNCHER.exists():
            # LAUNCHER is now throttlefed-gui
            self.toast("Demo run: nothing was installed, so there is nothing to open.")
            return
        try:
            subprocess.Popen([str(LAUNCHER)], start_new_session=True)
        except OSError as exc:
            self.toast(f"Could not start {LAUNCHER}: {exc}")
            return
        self.close()

    def toast(self, message: str) -> None:
        overlay = self.get_content()
        if isinstance(overlay, Adw.ToastOverlay):
            toast = Adw.Toast(title=message)
            # libadwaita < 1.9 refuses an unknown kwarg on the constructor.
            toast.set_priority(Adw.ToastPriority.NORMAL)
            overlay.add_toast(toast)
        else:
            self._append(f"--- {message}")

    # ---------------------------------------------------------------- install
    def _install_flags(self) -> list[str]:
        flags = []
        if not self.gui_switch.get_active():
            flags.append("--no-gui")
        if not self.service_switch.get_active():
            flags.append("--no-service")
        if not self.stock_switch.get_active():
            flags.append("--no-stock")
        return flags

    def _start_install(self) -> None:
        self.log_buffer.set_text("")
        self.progress.set_fraction(0.0)
        self.saw_result = False
        self.result_ok = False
        self.result_text = ""
        self.state = "running"

        if self.demo:
            self.phase_label.set_text("Demo run in progress")
            self._append("--- demo run: the machine is not touched and this log is simulated")
            self._sync()
            threading.Thread(target=self._demo_walk, daemon=True).start()
            return

        argv, problem = backend_argv("install", self._install_flags(), self.dry_run)
        if not argv:
            self._fail(problem)
            return
        self.phase_label.set_text(
            "Reading the plan from the backend"
            if self.dry_run
            else "Asking for the administrator password"
        )
        self._append(f"--- running: {' '.join(argv)}")
        self.job = BackendJob(argv, self._on_record, self._on_finished)
        self._sync()
        self.job.start()

    def _demo_walk(self) -> None:
        for percent, detail in DEMO_STEPS:
            GLib.idle_add(self._on_record, f"STEP|{percent}|install|{detail}")
            GLib.idle_add(self._on_record, f"LOG|demo: {detail}")
            time.sleep(DEMO_DELAY_SECONDS)
        GLib.idle_add(self._on_record, "RESULT|ok|demo run: nothing was installed")
        GLib.idle_add(self._on_finished, 0, False)

    def _fail(self, message: str) -> None:
        self.state = "failed"
        self.phase_label.set_text(message)
        self._append(f"--- {message}")
        self._sync()

    def _on_record(self, line: str):
        kind, _, rest = line.partition("|")
        if kind == "STEP":
            fields = rest.split("|", 2)
            percent = int(fields[0]) if fields and fields[0].isdigit() else 0
            detail = fields[2] if len(fields) > 2 else rest
            self.progress.set_fraction(percent / 100)
            self.phase_label.set_text(detail)
            self._append(f"[{percent:>3}%] {detail}")
        elif kind == "LOG":
            self._append(rest)
        elif kind == "CHECK":
            fields = rest.split("|", 2)
            state = fields[0] if fields else "?"
            self._append(f"{state}: {' | '.join(fields[1:])}")
        elif kind == "STATUS":
            fields = rest.split("|", 2)
            self._append(f"{fields[0]}: {fields[1] if len(fields) > 1 else ''} {fields[2] if len(fields) > 2 else ''}".rstrip())
        elif kind == "RESULT":
            fields = rest.split("|", 1)
            self.saw_result = True
            self.result_ok = fields[0] == "ok"
            self.result_text = fields[1] if len(fields) > 1 else ""
            self._append(f"{'OK' if self.result_ok else 'FAILED'}: {self.result_text}")
        elif line.strip():
            # Anything without a record prefix comes from a tool the backend runs.
            self._append(line)
        return GLib.SOURCE_REMOVE

    def _append(self, text: str) -> None:
        self.log_buffer.insert(self.log_buffer.get_end_iter(), text + "\n", -1)
        end = self.log_buffer.get_end_iter()
        self.log_view.scroll_to_iter(end, 0.0, False, 0.0, 1.0)

    def _on_finished(self, code: int, cancelled: bool):
        self.job = None
        if cancelled:
            self._fail("Cancelled. Check the log for how far the install got.")
        elif code == 0 and self.saw_result and self.result_ok:
            self.state = "done"
            self.progress.set_fraction(1.0)
            self.phase_label.set_text("Done")
            self._fill_summary()
            self._sync()
            self._go(4)
        elif code in (126, 127) and not self.saw_result:
            self._fail(
                "The administrator prompt was dismissed or the password was "
                "refused, so nothing was changed."
            )
            self._append(f"--- the privileged command left with status {code}")
        else:
            self._fail(self.result_text or f"The installer stopped with status {code}.")
        return GLib.SOURCE_REMOVE

    def _fill_summary(self) -> None:
        if self.summary_filled:
            return
        self.summary_filled = True

        if self.gui_switch.get_active():
            self.summary_group.add(
                self._info_row("Desktop application", f"{DESKTOP_FILE}, icon {ICON_FILE}")
            )
            self.summary_group.add(
                self._info_row("Launcher", f"{LAUNCHER} starts the GTK app")
            )
            self.summary_group.add(
                self._info_row(
                    "Bundled fonts",
                    f"{FONT_DIR}, Google Sans Flex Bold/Medium (OFL)",
                )
            )
        else:
            self.summary_group.add(
                self._info_row("Command line tool", f"{CLI_FILE} (no desktop application)")
            )
        self.summary_group.add(
            self._info_row("Privileged helper", f"{HELPER}, mode 0755, root:root")
        )
        self.summary_group.add(self._info_row("Polkit policy", str(POLICY)))
        if self.service_switch.get_active():
            self.summary_group.add(
                self._info_row(
                    "Boot service and timer",
                    "throttlefed.service plus throttlefed.timer every 60 s",
                )
            )
        if self.stock_switch.get_active():
            self.summary_group.add(
                self._info_row("Restore target", f"{STOCK_FILE} (factory snapshot)")
            )
        else:
            self.summary_group.add(
                self._info_row("Restore target", "not captured in this run")
            )
        self.summary_group.add(
            self._info_row("Saved state", f"{STATE_DIR} (0755), kept on uninstall")
        )
        if self.demo:
            self.summary_group.add(
                self._info_row("Demo run", "This machine was not changed.")
            )

        self.revert_group.add(
            self._info_row("Remove everything", f"sudo {BACKEND} uninstall")
        )
        self.revert_group.add(
            self._info_row(
                "Restore the factory limits",
                "Pick Restore stock in the app; it applies the snapshot saved in "
                f"{STATE_DIR}.",
            )
        )
        self.revert_group.add(
            self._info_row(
                "Delete the saved state too",
                f"sudo rm -rf {STATE_DIR} /etc/throttlefed",
            )
        )


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------
# Same palette and family as the window this wizard installs, so the installer and
# the app read as one product: #0A0A69 deep navy, #141414 near black, #4343FF
# accent, #FFFFFF text, Google Sans Flex Medium in the body and Bold in headings.
CSS = """
window, .background { background-color: #141414; color: #FFFFFF; }
headerbar { background-color: #0A0A69; color: #FFFFFF; }
.card, .boxed-list, list.boxed-list, listview, list { background-color: #1F1F1F; }
.card > row, .boxed-list > row { background-color: #1F1F1F; }
.hint, .dim-label { color: alpha(#FFFFFF, 0.62); }
window, headerbar { font-family: "Google Sans Flex"; }
window { font-weight: 500; }
.title-1, .title-2, .title-3, .heading { font-weight: 700; }
"""


class SetupApplication(Adw.Application):
    def __init__(self, demo: bool = False, dry_run: bool = False):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.demo = demo
        self.dry_run = dry_run
        self.window: SetupWindow | None = None

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        # the palette is dark by definition, so the wizard does not follow the system
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_USER)

    def do_activate(self) -> None:
        if self.window is None:
            self.window = SetupWindow(application=self, demo=self.demo, dry_run=self.dry_run)
        self.window.present()


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------
def run(demo: bool = False, dry_run: bool = False) -> int:
    if os.geteuid() == 0:
        print(
            "Run the wizard as your normal user: it asks for the administrator "
            "password through polkit when the install starts.",
            file=sys.stderr,
        )
        return 1
    if not BACKEND.exists():
        print(f"installer-backend.sh is missing next to installer.py ({BACKEND})", file=sys.stderr)
        return 1
    return SetupApplication(demo=demo, dry_run=dry_run).run(sys.argv[:1])


def main() -> int:
    parser = argparse.ArgumentParser(description="ThrottleFed setup wizard")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="walk through the wizard without touching the system",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="ask the backend for its plan only; writes nothing",
    )
    args = parser.parse_args()
    return run(demo=args.demo, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
