#!/usr/bin/python3
"""ThrottleFed - power budget control (PL1/PL2, PP0, EPP) with a GTK4 + libadwaita GUI.

Architecture:
  * Python (this file)     = interface + POLICY. It reuses the `throttlefed.py`
    module (the already tested CLI) as the single source of truth for the
    profiles and the write plan, so no value is reimplemented here.
  * C (throttlefed-helper) = the only writer as root: it takes the plan built
    in Python, validates it against a whitelist and returns the value RE-READ
    from sysfs. The GUI never runs as root.

Run: /usr/bin/python3 throttlefed_gui.py    (the system python ships the gi bindings)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

APP_ID = "io.github.framet.ThrottleFed"
ACTIVE_FILE = Path("/var/lib/throttlefed/active.txt")
STOCK_FILE = Path("/var/lib/throttlefed/stock.conf")

HERE = Path(__file__).resolve().parent
HELPER_CANDIDATES = [
    "/usr/local/libexec/throttlefed-helper",
    str(HERE / "build" / "throttlefed-helper"),
    str(HERE / "throttlefed-helper"),
]

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk, Gio, GLib, Gtk
except (ImportError, ValueError) as exc:
    sys.exit(
        "ThrottleFed needs PyGObject with GTK4/libadwaita.\n"
        f"Run it with the system python: /usr/bin/python3 throttlefed_gui.py\n({exc})"
    )

sys.path.insert(0, str(HERE))
import throttlefed as core  # noqa: E402  (single source of truth for profiles and plan)

# ---- presentation metadata
PROFILE_TITLES = {
    "balanced": "Balanced",
    "cpu": "CPU",
    "cpu-max": "Max CPU",
    "gpu": "GPU",
    "gpu-max": "Max GPU",
    "quiet": "Quiet",
    "burst": "Burst",
}
PROFILE_DOCS = {
    "balanced": "stock, nothing forced",
    "cpu": "more budget for the CPU: high PL1/PL2, EPP performance",
    "cpu-max": "CPU maxed out: PL1 28 W (platform cTDP ceiling), EPP performance",
    "gpu": "more budget for the iGPU: caps PP0 (cores) so it cannot swallow the package",
    "gpu-max": "iGPU maxed out: looser package, capped cores, high GT minimum",
    "quiet": "quiet and cool: capped package, economy EPP",
    "burst": "sustained 8 W and quiet, short platform burst",
}
PROFILE_ORDER = ["balanced", "cpu", "cpu-max", "gpu", "gpu-max", "quiet", "burst"]
# `burst` is the current id; the template it maps to still lives under the old
# `macbook` key, and an active.txt written by an older version still says
# `macbook`, so both directions of the mapping are needed.
CORE_PROFILE_KEY = {"burst": "macbook"}
EPP_CHOICES = ["performance", "balance_performance", "balance_power", "power"]
THERMAL_CHOICES = ["performance", "balanced", "low-power", "quiet"]

# Identity: #0A0A69 deep navy, #141414 near black, #4343FF accent, #FFFFFF text.
# libadwaita reads every widget through these named colors, so redefining them here
# re-themes the whole window at once, dialogs included.
CSS = """
@define-color window_bg_color #141414;
@define-color window_fg_color #FFFFFF;
@define-color view_bg_color #1A1A1A;
@define-color view_fg_color #FFFFFF;
@define-color headerbar_bg_color #0A0A69;
@define-color headerbar_fg_color #FFFFFF;
@define-color card_bg_color #1F1F1F;
@define-color card_fg_color #FFFFFF;
@define-color popover_bg_color #1F1F1F;
@define-color popover_fg_color #FFFFFF;
@define-color accent_color #7373FF;
@define-color accent_bg_color #4343FF;
@define-color accent_fg_color #FFFFFF;
window, headerbar, popover, .hero, .pill {
    font-family: "Google Sans Flex";
}
window { font-weight: 500; }
.hero {
    background-color: alpha(@accent_bg_color, 0.10);
    border-radius: 18px;
    padding: 16px 20px;
}
.hero-num { font-size: 28px; font-weight: 700; letter-spacing: -0.6px; }
.hero-sub { opacity: 0.70; }
.pill {
    border-radius: 999px;
    padding: 2px 10px;
    background-color: alpha(@window_fg_color, 0.08);
    font-weight: 500;
    font-size: 0.85em;
}
.pill-ok { background-color: alpha(@accent_bg_color, 0.22); color: @accent_color; }
.pill-warn { background-color: alpha(@warning_bg_color, 0.25); color: @warning_color; }
.chart { background-color: alpha(@window_fg_color, 0.05); border-radius: 12px; }
.mono { font-family: monospace; font-size: 0.92em; }
.sec { font-weight: 700; }

/* Surfaces the theme paints itself. Redefining the named colors is not enough:
   libadwaita resolves its own rules against its own definitions, so the palette
   has to be set on the widgets, not on the names. */
window, .background { background-color: #141414; }
headerbar { background-color: #0A0A69; color: #FFFFFF; }
.card, .boxed-list, list.boxed-list, listview, list { background-color: #1F1F1F; }
.card > row, .boxed-list > row { background-color: #1F1F1F; }
row:selected, listview > row:selected { background-color: alpha(#4343FF, 0.28); }
.hero { background-color: alpha(#4343FF, 0.10); }
.pill-ok { background-color: alpha(#4343FF, 0.22); color: #7373FF; }
.chart { background-color: alpha(#FFFFFF, 0.05); }
"""


def core_key(name: str) -> str:
    """Id used by this UI -> key used by the throttlefed.py templates."""
    return CORE_PROFILE_KEY.get(name, name)


def display_id(name: str) -> str:
    """Key stored in a state file -> id shown and matched by this UI."""
    for new, old in CORE_PROFILE_KEY.items():
        if name == old:
            return new
    return name


# ---- unprivileged reads (sysfs only)
def read_text(path, default=None):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def read_int(path, default=None):
    v = read_text(path)
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def read_active_profile():
    v = read_text(ACTIVE_FILE)
    if v:
        return display_id(v)
    # fallback: the CLI writes the active profile into its own state dir
    for cand in (Path("/var/lib/throttlefed/active.json"),
                 core.STATE_DIR / "active.json"):
        try:
            data = json.loads(cand.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            for key in ("profile", "name", "ativo"):
                if data.get(key):
                    return display_id(str(data[key]))
        if isinstance(data, str):
            return display_id(data)
    return None


def cpu_freqs():
    vals = []
    base = Path("/sys/devices/system/cpu/cpufreq")
    if base.is_dir():
        for p in sorted(base.glob("policy*/scaling_cur_freq")):
            v = read_int(p)
            if v:
                vals.append(v / 1000.0)
    if not vals:
        return None, None
    return sum(vals) / len(vals), max(vals)


def temps():
    out = {}
    for z in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        name = read_text(z / "type")
        val = read_int(z / "temp")
        if name and val:
            out[name] = val / 1000.0
    return out


def gpu_now():
    gt = core.GT
    if not gt:
        return {}
    return {
        "act_mhz": read_int(gt / "rps_act_freq_mhz"),
        "min_mhz": read_int(gt / "rps_min_freq_mhz"),
        "max_mhz": read_int(gt / "rps_max_freq_mhz"),
        "pl1": read_int(gt / "throttle_reason_pl1"),
        "pl2": read_int(gt / "throttle_reason_pl2"),
        "pl4": read_int(gt / "throttle_reason_pl4"),
        "thermal": read_int(gt / "throttle_reason_thermal"),
    }


def find_helper():
    for cand in HELPER_CANDIDATES:
        if os.access(cand, os.X_OK):
            return cand
    return None


def run_helper(args, stdin_text=None, timeout=240):
    helper = find_helper()
    if not helper:
        return False, {"erro": "C helper not found - run ./setup.sh"}
    if not os.path.exists("/usr/bin/pkexec"):
        return False, {"erro": "pkexec missing"}
    need_root = args and args[0] not in ("check", "paths", "show")
    cmd = (["pkexec", helper] if need_root else [helper]) + list(args)
    try:
        proc = subprocess.run(
            cmd,
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, {"erro": "pkexec timed out (dialog not answered?)"}
    except OSError as exc:
        return False, {"erro": str(exc)}
    if proc.returncode != 0 and not proc.stdout.strip():
        msg = proc.stderr.strip() or f"exit {proc.returncode}"
        if "Not authorized" in msg or proc.returncode in (126, 127):
            msg = "polkit denied the authorization (or the helper is not root-owned)"
        return False, {"erro": msg}
    try:
        return proc.returncode == 0, json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return False, {"erro": f"unexpected helper output: {proc.stdout[:200]!r}"}


def plan_to_lines(plan):
    lines = []
    for _label, path, value, _before, _after in plan:
        if path is None:                      # EPP applies to every cpufreq policy
            lines.append(f"@epp\t{value}")
            continue
        p = str(path)
        if p.endswith("intel-rapl:0:0/constraint_0_power_limit_uw"):
            lines.append(f"@pp0\t{value}")     # the helper writes limit + enabled in order
            continue
        if p.endswith("intel-rapl:0:0/enabled"):
            continue                           # already covered by @pp0
        lines.append(f"{p}\t{value}")
    return "\n".join(lines) + ("\n" if lines else "")


def fmt_uw(uw):
    return "-" if not uw else f"{uw / 1_000_000:.1f} W".replace(".0 ", " ")


# ---- measured power chart
class PowerChart(Gtk.DrawingArea):
    def __init__(self):
        super().__init__()
        self.samples = []
        self.set_content_height(120)
        self.set_size_request(420, 120)
        self.set_draw_func(self._draw)
        self.add_css_class("chart")

    def set_samples(self, samples):
        self.samples = list(samples or [])
        self.queue_draw()

    def _draw(self, _area, cr, width, height):
        cr.set_source_rgba(0.5, 0.5, 0.5, 0.25)
        for i in range(1, 5):
            y = height * i / 5
            cr.move_to(0, y)
            cr.line_to(width, y)
        cr.stroke()

        if len(self.samples) < 2:
            cr.set_source_rgba(0.5, 0.5, 0.5, 0.85)
            cr.select_font_face("monospace")
            cr.set_font_size(12)
            cr.move_to(14, height / 2 + 4)
            cr.show_text("no samples - click Measure now (needs root)")
            return

        top = max(self.samples) * 1.15 or 1
        step = width / (len(self.samples) - 1)
        accent = self.get_style_context().lookup_color("accent_color")[1]

        cr.move_to(0, height)
        for i, w in enumerate(self.samples):
            cr.line_to(i * step, height - (w / top) * height)
        cr.line_to(width, height)
        cr.close_path()
        cr.set_source_rgba(accent.red, accent.green, accent.blue, 0.18)
        cr.fill_preserve()

        cr.move_to(0, height - (self.samples[0] / top) * height)
        for i, w in enumerate(self.samples):
            cr.line_to(i * step, height - (w / top) * height)
        cr.set_source_rgba(accent.red, accent.green, accent.blue, 0.95)
        cr.set_line_width(2)
        cr.stroke()

        cr.set_source_rgba(0.5, 0.5, 0.5, 0.9)
        cr.select_font_face("monospace")
        cr.set_font_size(11)
        cr.move_to(6, 14)
        cr.show_text(f"peak {max(self.samples):.1f} W")
        cr.move_to(6, height - 6)
        cr.show_text(f"avg {sum(self.samples)/len(self.samples):.1f} W")


# ---- window
def fit_size(w, h):
    """Keep the requested size inside the monitor work area."""
    try:
        disp = Gdk.Display.get_default()
        mon = disp.get_monitors().get_item(0) if disp is not None else None
        if mon is not None:
            geo = mon.get_geometry()
            gw, gh = geo.width, geo.height
            if callable(getattr(mon, "get_workarea", None)):
                r = mon.get_workarea()
                if r.width > 0 and r.height > 0:
                    gw, gh = r.width, r.height
            else:
                gh -= 90          # GDK without workarea: discount top bar + dock
            w = min(w, max(640, gw - 40))
            h = min(h, max(480, gh - 30))
    except Exception:  # no display: keep the request
        pass
    return w, h


# The entry at the top of a plugin picker that means "leave this channel alone".
# It has to be a real entry, because Adw.ComboRow has no empty state: it falls back
# to the first item, so without this a row would show a value nobody ever read and
# Apply would write it back to the firmware.
UNSET = ""


def esc(text):
    """Text that came from a plugin, from the store catalog or from the firmware.

    A row title and subtitle are parsed as Pango markup, so a plugin that prints
    "<root-only>" loses the whole line to a parse error and the user sees an empty
    row. Everything that is not written in this file goes through here first.
    """
    return GLib.markup_escape_text(str(text))


class ThrottleFedWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_title("ThrottleFed")
        self.set_default_size(*fit_size(1000, 760))

        self.active_profile = read_active_profile()
        self._tick_id = None
        self._busy = False

        self.toasts = Adw.ToastOverlay()
        self.stack = Adw.ViewStack()
        self.switcher = Adw.ViewSwitcher()
        self.switcher.set_stack(self.stack)
        self.switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)

        header = Adw.HeaderBar()
        header.set_title_widget(self.switcher)
        header.pack_start(self._menu_button())
        self.restore_btn = Gtk.Button(label="Restore stock")
        self.restore_btn.add_css_class("flat")
        self.restore_btn.connect("clicked", lambda *_: self.on_restore())
        header.pack_end(self.restore_btn)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)

        # The update notice is not a page's business: it belongs to the window, so
        # it shows whichever page is open, right under the header.
        self.update_banner = Adw.Banner()
        self.update_banner.set_revealed(False)
        try:
            self.update_banner.set_button_label("Release notes")
            self.update_banner.connect("button-clicked", lambda *_: self._open_releases())
        except (AttributeError, TypeError):
            pass  # older libadwaita: the banner stays informational without a button
        toolbar.add_top_bar(self.update_banner)

        toolbar.set_content(self.stack)
        self.toasts.set_child(toolbar)
        self.set_content(self.toasts)

        self.stack.add_titled_with_icon(
            self._clamp(self._page_profiles()), "perfis", "Profiles", "power-profile-balanced-symbolic")
        self.stack.add_titled_with_icon(
            self._clamp(self._page_tuning()), "ajuste", "Fine tuning", "preferences-system-symbolic")
        self.stack.add_titled_with_icon(
            self._clamp(self._page_monitor()), "monitor", "Monitor", "utilities-system-monitor-symbolic")

        # The store is the last tab, and an installed plugin gets a tab in front of
        # it: what a plugin drives is the plugin's own page, not a corner of somebody
        # else's. Nothing plugin-shaped is imported until it is installed, so the
        # store being empty is the same app as before there was a store.
        self.plugin_pages = {}
        self.plugin_pickers = {}
        self.store_items = []
        self.store_page = self._clamp(self._page_store())
        self._sync_plugin_tabs()
        self.stack.connect("notify::visible-child-name", lambda *_: self.refresh())

        self.refresh()
        self._start_tick()
        self._start_update_check()

    # ---- UI helpers
    @staticmethod
    def _clamp(child):
        clamp = Adw.Clamp()
        clamp.set_maximum_size(760)
        clamp.set_margin_top(18)
        clamp.set_margin_bottom(28)
        clamp.set_margin_start(18)
        clamp.set_margin_end(18)
        clamp.set_child(child)
        # without the scroller the window grows to the NATURAL height of the
        # content and overflows a small panel: the scroller absorbs the excess.
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_child(clamp)
        return scroller

    @staticmethod
    def _label(text, css=None, xalign=0.0, wrap=False):
        lbl = Gtk.Label(label=text)
        lbl.set_xalign(xalign)
        if wrap:
            lbl.set_wrap(True)
        if css:
            lbl.add_css_class(css)
        return lbl

    def _menu_button(self):
        menu = Gio.Menu()
        menu.append("Save stock now", "win.snapshot")
        menu.append("Open the CLI in a terminal", "win.cli")
        menu.append("Check for updates", "win.updates")
        menu.append("About ThrottleFed", "win.about")
        button = Gtk.MenuButton()
        button.set_icon_name("open-menu-symbolic")
        button.set_menu_model(menu)
        button.add_css_class("flat")

        for name, cb in (("snapshot", self.on_snapshot), ("cli", self.on_cli),
                         ("updates", self.on_check_updates), ("about", self.on_about)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, f=cb: f())
            self.add_action(action)
        return button

    def toast(self, text, timeout=4):
        t = Adw.Toast.new(text)
        try:
            t.set_timeout(timeout)
        except (AttributeError, TypeError):
            pass  # older libadwaita: no custom timeout
        self.toasts.add_toast(t)

    def _busy_do(self, work, done):
        """Run `work` in a thread (pkexec blocks) and deliver the result on the UI."""
        if self._busy:
            self.toast("An operation is already running")
            return
        self._busy = True
        self.restore_btn.set_sensitive(False)

        def runner():
            try:
                result = work()
            except Exception as exc:
                result = (False, {"erro": f"{type(exc).__name__}: {exc}"})
            GLib.idle_add(finish, result)

        def finish(result):
            self._busy = False
            self.restore_btn.set_sensitive(True)
            try:
                done(result)
            finally:
                self.refresh()
            return GLib.SOURCE_REMOVE

        threading.Thread(target=runner, daemon=True).start()

    # ---- page: profiles
    def _page_profiles(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)

        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        hero.add_css_class("hero")
        self.hero_line = self._label("reading state...", "hero-num")
        self.hero_sub = self._label("", "hero-sub", wrap=True)
        self.hero_badges = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        hero.append(self.hero_line)
        hero.append(self.hero_sub)
        hero.append(self.hero_badges)
        box.append(hero)

        group = Adw.PreferencesGroup(title="Profiles")
        self.profile_rows = {}
        for name in PROFILE_ORDER:
            if core.PROFILE_TEMPLATES.get(core_key(name)) is None:
                continue
            row = Adw.ActionRow()
            row.set_title(PROFILE_TITLES.get(name, name))
            row.set_subtitle(PROFILE_DOCS.get(name, ""))
            check = Gtk.Image.new_from_icon_name("object-select-symbolic")
            check.set_visible(False)
            row.add_prefix(check)
            btn = Gtk.Button(label="Apply")
            btn.add_css_class("pill")
            btn.connect("clicked", lambda _b, n=name: self.on_apply_profile(n))
            row.add_suffix(btn)
            row.set_activatable_widget(btn)
            group.add(row)
            self.profile_rows[name] = (row, check, btn)
        box.append(group)

        note = self._label(
            "Where the firmware leaves the PL1 MSR unlocked (lock bit 0), PL1 can be "
            "raised above the value the firmware declares.",
            "dim-label", wrap=True)
        box.append(note)
        return box

    # ---- page: fine tuning
    def _scale_row(self, title, subtitle, lo, hi, step, unit, value):
        row = Adw.ActionRow()
        row.set_title(title)
        row.set_subtitle(subtitle)
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, step)
        scale.set_draw_value(True)
        scale.set_value_pos(Gtk.PositionType.RIGHT)
        scale.set_size_request(230, -1)
        scale.set_value(value)
        scale.set_format_value_func(lambda _s, v, u=unit: f"{v:g} {u}")
        row.add_suffix(scale)
        row.set_activatable_widget(scale)
        return row, scale

    def _page_tuning(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        cur = core.current_targets()

        # banner at the TOP: the plan text (delta_label) sits below the fold on a
        # small panel, and a warning nobody sees warns nobody.
        self.alerta = Adw.Banner()
        self.alerta.set_revealed(False)
        box.append(self.alerta)

        g1 = Adw.PreferencesGroup(
            title="Package budget",
            description="PL1/PL2 is a single budget: it covers cores, ring, memory and iGPU.",
        )
        pl1 = min(max(round((cur["pl1_uw"] or 15_000_000) / 1e6), 5), 45)
        pl2 = min(max(round((cur["pl2_uw"] or 60_000_000) / 1e6), 5), 75)
        row, self.pl1_scale = self._scale_row(
            "PL1 - sustained power", f"current {fmt_uw(cur['pl1_uw'])} · long_term",
            5, 45, 1, "W", pl1)
        g1.add(row)
        row, self.pl2_scale = self._scale_row(
            "PL2 - short peak", f"current {fmt_uw(cur['pl2_uw'])} · short_term",
            5, 75, 1, "W", pl2)
        g1.add(row)
        box.append(g1)

        g2 = Adw.PreferencesGroup(title="iGPU")
        row, self.gtmin_scale = self._scale_row(
            "iGPU minimum frequency", f"current {cur.get('gt_min_mhz') or '-'} MHz · rps_min",
            100, 1300, 50, "MHz", min(max(cur.get("gt_min_mhz") or 100, 100), 1300))
        g2.add(row)
        box.append(g2)

        g3 = Adw.PreferencesGroup(title="Hardware power profile")
        self.epp_row = Adw.ComboRow()
        self.epp_row.set_title("EPP - Speed Shift")
        self.epp_row.set_subtitle("who picks the frequency for a lighter load")
        self.epp_row.set_model(Gtk.StringList.new(EPP_CHOICES))
        if cur.get("epp") in EPP_CHOICES:
            self.epp_row.set_selected(EPP_CHOICES.index(cur["epp"]))
        g3.add(self.epp_row)

        self.thermal_row = Adw.ComboRow()
        self.thermal_row.set_title("Firmware thermal mode")
        self.thermal_row.set_subtitle("values written to platform_profile")
        self.thermal_row.set_model(Gtk.StringList.new(THERMAL_CHOICES))
        if cur.get("platform_profile") in THERMAL_CHOICES:
            self.thermal_row.set_selected(THERMAL_CHOICES.index(cur["platform_profile"]))
        g3.add(self.thermal_row)
        box.append(g3)

        for widget in (self.pl1_scale, self.pl2_scale, self.gtmin_scale):
            widget.connect("value-changed", lambda *_: self._update_delta())
        self.epp_row.connect("notify::selected", lambda *_: self._update_delta())
        self.thermal_row.connect("notify::selected", lambda *_: self._update_delta())

        self.delta_label = self._label("", "dim-label", wrap=True)
        box.append(self.delta_label)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        preview = Gtk.Button(label="Preview plan (no root)")
        preview.connect("clicked", lambda *_: self.on_preview_custom())
        apply_btn = Gtk.Button(label="Apply fine tuning")
        apply_btn.add_css_class("suggested-action")
        apply_btn.connect("clicked", lambda *_: self.on_apply_custom())
        actions.append(preview)
        actions.append(apply_btn)
        box.append(actions)
        self._update_delta()
        return box

    def custom_profile(self):
        return {
            "pl1_w": float(self.pl1_scale.get_value()),
            "pl2_w": float(self.pl2_scale.get_value()),
            "epp": EPP_CHOICES[self.epp_row.get_selected()],
            "platform_profile": THERMAL_CHOICES[self.thermal_row.get_selected()],
            "gt_min_mhz": int(self.gtmin_scale.get_value()),
        }

    def _update_delta(self):
        try:
            plan = core.build_plan(self.custom_profile(), {})
        except Exception:
            self.delta_label.set_text("could not build the plan")
            return
        parts = []
        for label, _path, _value, before, after in plan:
            short = label.split(" (")[0]
            if before != after:
                parts.append(f"{short}: {before} → {after}")
        resumo = " · ".join(parts)
        pl1, pl2 = self.pl1_scale.get_value(), self.pl2_scale.get_value()
        aviso = pl2 < pl1
        self.delta_label.set_text(f"Will change → {resumo}" if resumo
                                  else "No change against what is applied")
        alerta = getattr(self, "alerta", None)
        if alerta is not None:
            alerta.set_title(
                f"PL2 ({pl2:g} W) below PL1 ({pl1:g} W): the effective limit becomes the "
                f"lower of the two. PL2 is the short peak ceiling, PL1 the sustained "
                f"target: use PL2 >= PL1.")
            alerta.set_revealed(bool(aviso))

    # ---- page: monitor
    def _mon_row(self, group, title, subtitle=None):
        row = Adw.ActionRow()
        row.set_title(title)
        if subtitle:
            row.set_subtitle(subtitle)
        value = Gtk.Label(label="-")
        value.add_css_class("mono")
        value.add_css_class("dim-label")
        row.add_suffix(value)
        group.add(row)
        return value

    def _page_monitor(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.mon = {}

        g = Adw.PreferencesGroup(title="Package (RAPL)",
                                 description="Updates every second. Values read from sysfs.")
        self.mon["pl1"] = self._mon_row(g, "PL1 applied")
        self.mon["pl2"] = self._mon_row(g, "PL2 applied")
        self.mon["pp0"] = self._mon_row(
            g, "Core cap (PP0)", "read-only: the platform may accept the cap and not obey it")
        self.mon["window"] = self._mon_row(
            g, "PL1 window (firmware)", "the burst window PL1 alone does not explain")
        box.append(g)

        g = Adw.PreferencesGroup(title="CPU")
        self.mon["freq"] = self._mon_row(g, "Frequency now", "average · max across cpufreq policies")
        self.mon["temp"] = self._mon_row(g, "Package temperature", "x86_pkg_temp")
        box.append(g)

        g = Adw.PreferencesGroup(title="iGPU")
        self.mon["gt"] = self._mon_row(g, "iGPU frequency", "current · min · max")
        self.mon["throttle"] = self._mon_row(g, "Throttle reason",
                                            "pl1 · pl2 · pl4 · thermal")
        box.append(g)

        g = Adw.PreferencesGroup(title="Measured package power",
                                 description="energy_uj is root-only in the kernel, so every "
                                             "measurement goes up through pkexec (once per click).")
        self.mon["power"] = self._mon_row(g, "Last measurement")
        chart_row = Adw.PreferencesRow()
        chart_holder = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        chart_holder.set_margin_top(6)
        chart_holder.set_margin_bottom(12)
        chart_holder.set_margin_start(12)
        chart_holder.set_margin_end(12)
        self.chart = PowerChart()
        self.chart.set_hexpand(True)
        chart_holder.append(self.chart)
        chart_row.set_child(chart_holder)
        g.add(chart_row)
        measure = Gtk.Button(label="Measure now")
        measure.add_css_class("pill")
        measure.connect("clicked", lambda *_: self.on_measure())
        mrow = Adw.ActionRow()
        mrow.set_title("Sample package power")
        mrow.set_subtitle("5 s window")
        mrow.add_suffix(measure)
        mrow.set_activatable_widget(measure)
        g.add(mrow)
        box.append(g)

        g = Adw.PreferencesGroup(
            title="Who can override",
            description="System daemons that rewrite PL1/EPP on their own.",
        )
        self.mon["svc"] = self._mon_row(g, "Services")
        self.mon["tuned"] = self._mon_row(g, "tuned profile")
        box.append(g)

        box.append(self._diag_block())
        return box

    # ---- store and plugin pages
    def _page_store(self):
        """What is on offer, and the only two things the store can be asked to do.

        Reading the catalog is a file read: this page costs nothing, needs no
        privilege, and works with no plugin installed at all. Installing is a
        directory copy into a plugin search path, not a system package, and the
        thing that touches firmware is still only the helper.
        """
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.store_group = Adw.PreferencesGroup(
            title="Plugins on offer",
            description="Nothing here comes installed, and nothing here is imported "
                        "until it is installed: a plugin that is only on offer cannot "
                        "be the reason the app fails to start.")
        box.append(self.store_group)
        self.store_hint = self._label("", "dim-label", wrap=True)
        box.append(self.store_hint)
        self._fill_store()
        return box

    def _fill_store(self):
        for child in self.store_items:
            self.store_group.remove(child)
        self.store_items = []
        self.store_rows = {}
        store = getattr(core, "STORE", None)
        if store is None:
            self.store_hint.set_text(
                "throttlefed_store.py is not next to throttlefed.py, so there is no "
                "catalog to read and no install to do.")
            return
        try:
            rows = store.status()
        except Exception as exc:
            self.store_hint.set_text(f"The catalog could not be read: {exc}")
            return
        target = store.target_dir()
        self.store_hint.set_text(
            f"An installed plugin is copied into {target}." if target
            else "No writable plugin directory was found on this machine.")
        for row in rows:
            self.store_rows[row["id"]] = row
            # Clicking the row expands it. What is inside is the reason to expand:
            # what the plugin changes, and who made the original tool it stands on.
            item = Adw.ExpanderRow(title=esc(row["name"]),
                                   subtitle=esc(self._store_subtitle(row)))
            try:
                item.set_subtitle_lines(4)
            except (AttributeError, TypeError):
                pass  # older libadwaita: the subtitle is clipped instead
            button = Gtk.Button(label="Remove" if row["installed"] else "Install")
            button.add_css_class("flat" if row["installed"] else "suggested-action")
            button.set_valign(Gtk.Align.CENTER)
            button.set_sensitive(bool(row["packaged"]))
            button.connect("clicked", lambda _b, pid=row["id"]: self.on_plugin_toggle(pid))
            item.add_suffix(button)
            if row["detail"]:
                item.add_row(self._row("What it changes", row["detail"], lines=4))
            for credit in row["credits"]:
                self._credit_rows(item, credit)
            self.store_group.add(item)
            self.store_items.append(item)

    @staticmethod
    def _row(title, subtitle, lines=2):
        """A read-only row. The cap on subtitle lines is tried, not required: an
        older libadwaita clips the text instead of honouring it."""
        row = Adw.ActionRow(title=esc(title), subtitle=esc(subtitle))
        try:
            row.set_subtitle_lines(lines)
        except (AttributeError, TypeError):
            pass
        return row

    def _credit_rows(self, container, credit):
        """One credit, as rows: who made the tool, under what licence, what this
        plugin reuses from it, and what it deliberately leaves alone.

        The repository is a link, because the point of a credit is that a person can
        go and look at the thing being credited.
        """
        container.add_row(self._row(
            credit.get("name", "original tool"),
            f"{credit.get('creator', 'unknown')}  ·  "
            f"{credit.get('license', 'licence not stated')}"))
        for label, value in (("What this plugin reuses", credit.get("reuse", "")),
                             ("What it does not do", credit.get("note", ""))):
            if value:
                container.add_row(self._row(label, value, lines=4))
        repo = credit.get("repo", "")
        if repo:
            row = self._row("Repository", repo)
            link = Gtk.LinkButton(uri=repo, label="Open")
            link.set_valign(Gtk.Align.CENTER)
            row.add_suffix(link)
            row.set_activatable_widget(link)
            container.add_row(row)

    def _credits_group(self, credits, title="Credits and sources"):
        """One expander per original tool, closed by default: the credit is on the
        page where it belongs without taking the page over."""
        if not credits:
            return None
        group = Adw.PreferencesGroup(
            title=esc(title),
            description=esc("The work this plugin stands on. None of it is bundled here "
                            "and none of its code is inside the plugin."))
        for credit in credits:
            exp = Adw.ExpanderRow(title=esc(credit.get("name", "original tool")),
                                  subtitle=esc(credit.get("creator", "")))
            self._credit_rows(exp, credit)
            group.add(exp)
        return group

    @staticmethod
    def _store_subtitle(row):
        """What a person needs to decide, in the order they need it: what it is for,
        whether their machine has the channel at all, and what the code is."""
        state = ("installed, its tab is open" if row["installed"]
                 else "on offer" if row["packaged"] else "no package in the store")
        hardware = (f"this machine has the channel: {row['hardware_note']}" if row["hardware"]
                    else f"not for this machine: {row['hardware_note']}")
        line = f"{row['summary']}\n{state}  ·  {hardware}"
        if row["license"]:
            line += f"\n{row['license']}"
        return line

    def _sync_plugin_tabs(self):
        """Give every installed plugin a tab of its own, and keep the store last.

        A view stack only appends, so the order is rebuilt on every change: plugin
        tabs first, the store after them.
        """
        for page in self.plugin_pages.values():
            self.stack.remove(page)
        self.plugin_pages = {}
        self.plugin_pickers = {}
        # The store page is not in the stack on the first call, while the tabs are
        # still being built, and removing a child that is not there is a GTK
        # critical, not a no-op.
        if self.store_page.get_parent() is self.stack:
            self.stack.remove(self.store_page)
        for plug in core.plugins(reload=True):
            try:
                rep = plug.report()
                page = self._clamp(self._page_plugin(plug, rep))
            except Exception as exc:
                # A plugin that cannot describe itself, or whose page cannot be
                # built, gets no tab: it must never be the reason the window itself
                # fails to open. Same contract as the loader, one level up.
                print(f"throttlefed: plugin tab skipped ({type(exc).__name__}: {exc})",
                      file=sys.stderr)
                continue
            self.stack.add_titled_with_icon(page, f"plugin-{rep['id']}", rep["tab"],
                                            "application-x-addon-symbolic")
            self.plugin_pages[rep["id"]] = page
        self.stack.add_titled_with_icon(self.store_page, "store", "Plugin Store",
                                        "system-software-install-symbolic")

    def _page_plugin(self, plug, rep):
        """A plugin's page: its channels as pickers, one Apply, and what it reports.

        The pickers are built from the plugin's own description of itself, so a
        plugin added later needs no change here, and a plugin that reports nothing
        shows that instead of an empty page pretending to be broken.
        """
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.append(self._label(rep["name"], "title-3"))
        if rep["why"]:
            box.append(self._label(rep["why"], "dim-label", wrap=True))

        pickers = {}
        groups = {}
        for ch in rep["channels"]:
            group = groups.get(ch["group"])
            if group is None:
                group = Adw.PreferencesGroup(title=esc(ch["group"]))
                groups[ch["group"]] = group
                box.append(group)
            values = list(ch["values"])
            now = self._plugin_now(rep, ch)
            known = now in values
            why = ch.get("why", "")
            if not known:
                why += ("  |  the firmware value here could not be read by this process"
                        " (it is root-only), so nothing is preselected. Choose a value"
                        " to apply it.")
            combo = Adw.ComboRow(title=esc(ch["label"]), subtitle=esc(why))
            combo.set_model(Gtk.StringList.new([UNSET, *values]))
            combo.set_selected(values.index(now) + 1 if known else 0)
            group.add(combo)
            pickers[ch["key"]] = combo
        if groups:
            first = next(iter(groups.values()))
            apply_btn = Gtk.Button(label="Apply")
            apply_btn.add_css_class("suggested-action")
            apply_btn.connect("clicked", lambda _b, p=plug: self.on_apply_plugin(p))
            first.set_header_suffix(apply_btn)
        self.plugin_pickers[rep["id"]] = pickers

        for sec in rep["sections"]:
            if not sec["rows"]:
                continue
            group = Adw.PreferencesGroup(title=esc(sec["title"]))
            if sec.get("note"):
                group.set_description(esc(sec["note"]))
            for row in sec["rows"]:
                name = row[0] if isinstance(row, (tuple, list)) else row
                text = row[1] if isinstance(row, (tuple, list)) and len(row) > 1 else ""
                item = Adw.ActionRow(title=esc(name), subtitle=esc(text))
                try:
                    item.set_subtitle_lines(2)
                except (AttributeError, TypeError):
                    pass
                group.add(item)
            box.append(group)
        if not rep["channels"] and not rep["sections"]:
            box.append(self._label("This plugin reports nothing to change here.",
                                   "dim-label", wrap=True))
        credits = self._credits_group(rep.get("credits") or [])
        if credits is not None:
            box.append(credits)
        return box

    @staticmethod
    def _plugin_now(rep, ch):
        """The firmware value right now, borrowed from the plugin's own report.

        It is the first token of the read-only row named after the attribute the
        channel targets. When the value is root-only and this process cannot read
        it, the row says so and nothing is preselected, which is the honest thing
        to show: the picker starts where the firmware is only if the firmware
        answered.
        """
        name = str(ch["target"]).rsplit(":", 1)[-1]
        for sec in rep["sections"]:
            for row in sec["rows"]:
                if (isinstance(row, (tuple, list)) and len(row) > 1
                        and str(row[0]) == name):
                    first = str(row[1]).split()
                    return first[0].strip("()") if first else ""
        return ""

    def on_plugin_toggle(self, pid):
        """Install or remove, in a thread: it is a file copy, and the UI stays live."""
        store = getattr(core, "STORE", None)
        if store is None:
            return
        row = self.store_rows.get(pid) or {}
        remove = bool(row.get("installed"))

        def work():
            ok, message = (store.remove if remove else store.install)(pid)
            return ok, {"message": message}

        def done(result):
            ok, payload = result
            if not ok:
                self.toast(payload.get("message") or "the store refused", 6)
                return
            self.toast(payload.get("message", ""), 5)
            self._sync_plugin_tabs()
            self._fill_store()

        self._busy_do(work, done)

    def plugin_plan(self, rep):
        """The plan a plugin page would send: one line per channel, in the plugin's
        own words. Kept apart from the click handler so the plan can be checked
        without a root prompt in the way."""
        pickers = self.plugin_pickers.get(rep["id"], {})
        plan = []
        for ch in rep["channels"]:
            combo = pickers.get(ch["key"])
            if combo is None:
                continue
            values = list(ch["values"])
            idx = combo.get_selected()
            if idx < 1 or idx - 1 >= len(values):
                continue      # entry zero: the user left this channel alone
            value = values[idx - 1]
            if value == self._plugin_now(rep, ch):
                continue      # the firmware already holds it; writing it back is noise
            plan.append((ch["label"], ch["target"], value, "", value))
        return plan_to_lines(plan)

    def on_apply_plugin(self, plug):
        """Write the chosen channel values through the same helper path as everything
        else: check first, apply after, and believe the readback."""
        try:
            rep = plug.report()
        except Exception as exc:
            self.toast(f"This plugin failed to report: {exc}", 6)
            return
        tsv = self.plugin_plan(rep)
        if not tsv.strip():
            self.toast("Nothing to apply.")
            return

        def work():
            run_helper(["capture"])
            ok, res = run_helper(["check"], tsv)
            if not ok:
                return False, {"apply": res}
            ok, res = run_helper(["apply", "--profile", "custom"], tsv)
            return ok and not res.get("bad"), {"apply": res}

        def done(result):
            ok, payload = result
            if not ok:
                self.toast(f"Failed: {(payload.get('apply') or {}).get('erro') or 'see diagnostics'}", 6)
                return
            self.toast(self._summary_toast(f"{rep['tab']} applied", payload), 6)

        self._busy_do(work, done)

    def _diag_block(self):
        """Diagnostics, collapsed inside the monitor page."""
        exp = Gtk.Expander(label="Diagnostics")
        exp.set_expanded(False)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        refresh = Gtk.Button(label="Gather data")
        refresh.connect("clicked", lambda *_: self.fill_diag())
        full = Gtk.Button(label="Full probe (root)")
        full.add_css_class("pill")
        full.connect("clicked", lambda *_: self.on_full_probe())
        bar.append(refresh)
        bar.append(full)
        body.append(bar)

        self.diag_view = Gtk.TextView()
        self.diag_view.set_editable(False)
        self.diag_view.set_monospace(True)
        self.diag_view.add_css_class("mono")
        self.diag_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.diag_view.set_top_margin(12)
        self.diag_view.set_bottom_margin(12)
        self.diag_view.set_left_margin(12)
        self.diag_view.set_right_margin(12)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(360)
        scroller.set_child(self.diag_view)
        scroller.add_css_class("card")
        body.append(scroller)
        exp.set_child(body)
        self.fill_diag()
        return exp

    def diag_text(self):
        rapl = core.rapl_snapshot()
        pkg = rapl.get("package", {})
        core_zone = rapl.get("core", {})
        gpu = gpu_now()
        t = temps()
        helper = find_helper() or "NOT FOUND (run ./setup.sh)"
        stock = "no stock saved"
        if STOCK_FILE.exists():
            try:
                stock = STOCK_FILE.read_text().replace("\n", "  ")
            except OSError:
                stock = "could not read it"
        comp = {}
        try:
            comp = core.competitors()
        except Exception:
            pass
        lines = [
            "ThrottleFed - diagnostics",
            "=" * 60,
            f"kernel      : {read_text('/proc/sys/kernel/osrelease', '?')}",
            f"machine     : {read_text('/sys/class/dmi/id/product_name', '?')} "
            f"(BIOS {read_text('/sys/class/dmi/id/bios_version', '?')})",
            f"python      : {sys.version.split()[0]}  ·  helper C: {helper}",
            f"version     : throttlefed {core.VERSION}  ·  update check: {self.update_note()}",
            "",
            "PACKAGE BUDGET (sysfs, unprivileged read)",
            f"  PL1 long_term   : {fmt_uw(pkg.get('pl1_uw'))}   (window {pkg.get('pl1_win_us', 0) / 1e6:.1f} s)",
            f"  PL2 short_term  : {fmt_uw(pkg.get('pl2_uw'))}",
            f"  firmware peak   : {fmt_uw(pkg.get('peak_uw'))}",
            f"  PP0 cores       : {fmt_uw(core_zone.get('limit_uw'))}  "
            f"({'on' if core_zone.get('enabled') else 'off'})",
            f"  EPP             : {core.get_epp()}",
            f"  thermal mode    : {read_text('/sys/firmware/acpi/platform_profile', '?')}",
            "",
            "iGPU",
            f"  freq            : {gpu.get('act_mhz')} MHz now · "
            f"{gpu.get('min_mhz')}-{gpu.get('max_mhz')} MHz",
            f"  throttle reason : pl1={gpu.get('pl1')} pl2={gpu.get('pl2')} "
            f"pl4={gpu.get('pl4')} thermal={gpu.get('thermal')}",
            "",
            "TEMPERATURES",
        ]
        for name, val in sorted(t.items(), key=lambda kv: -kv[1])[:6]:
            lines.append(f"  {name:<16}: {val:.1f} °C")
        lines += [
            "",
            "WHO CAN OVERRIDE",
            f"  {'  '.join(f'{k}={v}' for k, v in comp.items()) if comp else 'could not query systemd'}",
            f"  tuned profile    : {core.tuned_profile()}",
            "",
            "SAVED STOCK (the target of Restore)",
            f"  {stock}",
        ]
        return "\n".join(lines)

    def fill_diag(self):
        self.diag_view.get_buffer().set_text(self.diag_text())

    # ---- actions
    def on_apply_profile(self, name):
        prof = core.resolve_profile(core_key(name))[0]
        plan = core.build_plan(prof, {})
        tsv = plan_to_lines(plan)

        def work():
            ok_cap, _ = run_helper(["capture"])
            ok_chk, chk = run_helper(["check"], tsv)
            if not ok_chk:
                return False, chk
            ok, res = run_helper(["apply", "--profile", name], tsv)
            return ok and not chk.get("bad"), {"check": chk, "apply": res, "profile": name}

        def done(result):
            ok, payload = result
            if not ok:
                self.toast(f"Failed: {payload.get('erro') or 'see diagnostics'}", 6)
                return
            self.active_profile = name
            self.toast(self._summary_toast(f"{PROFILE_TITLES.get(name, name)} applied", payload), 6)

        self._busy_do(work, done)

    def on_preview_custom(self):
        tsv = plan_to_lines(core.build_plan(self.custom_profile(), {}))
        ok, res = run_helper(["check"], tsv)
        if not ok:
            self.toast(f"Check failed: {res.get('erro')}", 6)
            return
        partes = [f"{i['target'].split('/')[-1]}: {i['readback'] or '?'} → {i['wrote']}"
                  for i in res.get("items", []) if i["ok"]]
        self.toast("Valid plan · " + " | ".join(partes) if partes else "Empty plan", 8)

    def on_apply_custom(self):
        prof = self.custom_profile()
        plan = core.build_plan(prof, {})
        tsv = plan_to_lines(plan)
        if not tsv.strip():
            self.toast("Nothing to apply.")
            return

        def work():
            run_helper(["capture"])
            ok, res = run_helper(["apply", "--profile", "custom"], tsv)
            return ok, {"apply": res, "profile": "custom"}

        def done(result):
            ok, payload = result
            if not ok:
                self.toast(f"Failed: {payload.get('erro') or 'see diagnostics'}", 6)
                return
            self.active_profile = "custom"
            self.toast(self._summary_toast("Fine tuning applied", payload), 6)

        self._busy_do(work, done)

    @staticmethod
    def _summary_toast(title, payload):
        items = (payload.get("apply") or {}).get("items", [])
        if not items:
            return title
        partes = []
        for it in items[:4]:
            nome = it["target"].split("/")[-1].replace("constraint_0_power_limit_uw", "limit")
            partes.append(f"{nome}={it['readback'] or it['wrote']}")
        return f"{title} · read back: " + ", ".join(partes)

    def on_restore(self):
        def work():
            return run_helper(["restore"])

        def done(result):
            ok, res = result
            if not ok:
                self.toast(f"Restore failed: {res.get('erro')}", 6)
                return
            self.active_profile = "balanced"
            stock = res.get("stock", {})
            self.toast("Stock restored · PL1 "
                       f"{fmt_uw(stock.get('pl1_uw'))} / PL2 {fmt_uw(stock.get('pl2_uw'))}", 6)

        self._busy_do(work, done)

    def on_snapshot(self):
        def work():
            return run_helper(["capture", "--force"])

        def done(result):
            ok, res = result
            self.toast("Stock saved." if ok else f"Failed: {res.get('erro')}")

        self._busy_do(work, done)

    def on_measure(self):
        def work():
            return run_helper(["sample", "--seconds", "5", "--interval", "500"])

        def done(result):
            ok, res = result
            if not ok or not res.get("ok"):
                self.toast(f"Measurement failed: {res.get('erro')}", 5)
                return
            self.chart.set_samples(res.get("samples"))
            self.mon["power"].set_text(
                f"average {res['avg_w']:.1f} W · peak {res['max_w']:.1f} W "
                f"({res['n']} samples in {res['seconds']} s)")

        self._busy_do(work, done)

    def on_full_probe(self):
        cli = HERE / "throttlefed.py"

        def work():
            try:
                proc = subprocess.run(
                    ["pkexec", "/usr/bin/python3", str(cli), "probe"],
                    capture_output=True, text=True, timeout=300)
            except (OSError, subprocess.TimeoutExpired) as exc:
                return False, {"erro": str(exc)}
            out = proc.stdout.strip() or proc.stderr.strip()
            if proc.returncode != 0 and not out:
                return False, {"erro": f"exit {proc.returncode}"}
            return True, {"texto": out}

        def done(result):
            ok, res = result
            if not ok:
                self.toast(f"Probe failed: {res.get('erro')}", 6)
                return
            self.diag_view.get_buffer().set_text(res["texto"])
            self.toast("Root probe finished.")

        self._busy_do(work, done)

    def on_cli(self):
        cli = HERE / "throttlefed.py"
        for term, args in (("kgx", ["-x"]), ("gnome-terminal", ["--"]),
                           ("ptyxis", ["--"]), ("xterm", ["-e"])):
            if subprocess.run(["which", term], capture_output=True).returncode == 0:
                try:
                    subprocess.Popen([term] + args + ["/usr/bin/python3", str(cli), "watch"])
                    return
                except OSError:
                    continue
        self.toast("No terminal found.")

    def on_about(self):
        about = Adw.AboutDialog()
        about.set_application_name("ThrottleFed")
        about.set_application_icon("io.github.framet.ThrottleFed")
        about.set_version(core.VERSION + " (gui 0.1)")
        about.set_comments(
            "Power budget control on Linux: PL1/PL2, iGPU floor, EPP and thermal mode.\n\n"
            "Python/GTK4 interface; every write goes through a C helper via pkexec, which "
            "returns the value re-read from sysfs. Profile regeneration: throttlefed.py."
        )
        about.add_link("Releases and update check", core.RELEASES_PAGE)
        about.present(self)

    # ---- updates
    def update_note(self):
        """The last known check result, from the cache only: this feeds the
        diagnostics view, which is instant and never waits on the network."""
        try:
            res = core.update_check(offline=True)
        except Exception as exc:
            return f"unknown ({type(exc).__name__})"
        if res.get("error"):
            last = res.get("last_known") or {}
            if last.get("latest"):
                return f"check failed, last seen {last['latest']}"
            return f"check failed ({res['error'][:40]})"
        if res.get("disabled"):
            return "disabled (THROTTLEFED_NO_UPDATE_CHECK is set)"
        if res.get("source") == "none":
            return "nothing published upstream yet"
        if res.get("latest") and res.get("has_update"):
            return f"{res['latest']} is published, this build is {res['local']}"
        if res.get("latest"):
            return f"{res['latest']} is the newest published ({res['source']})"
        return "no answer from GitHub yet"

    def _start_update_check(self):
        """One check a day, off the main loop, from the cache when it is fresh.
        Failures stay silent: a launch must never nag or wait on the network."""
        def runner():
            try:
                res = core.update_check()
            except Exception:
                return
            GLib.idle_add(self._update_result, res, True)

        threading.Thread(target=runner, daemon=True).start()

    def on_check_updates(self):
        self.toast("Checking for updates...")

        def runner():
            try:
                res = core.update_check(force=True)
            except Exception as exc:
                res = {"local": core.VERSION, "latest": None,
                       "error": f"{type(exc).__name__}: {exc}"}
            GLib.idle_add(self._update_result, res, False)

        threading.Thread(target=runner, daemon=True).start()

    def _update_result(self, res, quiet):
        if res.get("has_update"):
            self.update_banner.set_title(
                f"Update available: {res['latest']} (this build is {res['local']}).")
            self.update_banner.set_revealed(True)
            if not quiet:
                self.toast(f"Update available: {res['latest']}", 6)
        elif res.get("error"):
            if not quiet:
                self.toast(f"Could not check for updates: {res['error']}", 8)
        elif res.get("latest"):
            # A fresh answer with nothing newer retires the banner: it is a claim
            # about the last check, not a decoration that outlives its truth.
            self.update_banner.set_revealed(False)
            if not quiet:
                self.toast(f"No newer version, this build is {res['local']}")
        return GLib.SOURCE_REMOVE

    def _open_releases(self):
        try:
            Gtk.UriLauncher.new(core.RELEASES_PAGE).launch(self, None, None)
        except Exception:
            self.toast(core.RELEASES_PAGE, 8)

    # ---- periodic refresh
    def _start_tick(self):
        if self._tick_id is None:
            self._tick_id = GLib.timeout_add(1000, self._tick)

    def _stop_tick(self):
        if self._tick_id is not None:
            try:
                GLib.source_remove(self._tick_id)
            except Exception:
                pass
            self._tick_id = None

    def _tick(self):
        try:
            if self.stack.get_visible_child_name() == "monitor":
                self.update_monitor()
        except Exception:  # an error here would kill the loop silently
            pass
        return GLib.SOURCE_CONTINUE

    def update_monitor(self):
        rapl = core.rapl_snapshot()
        pkg = rapl.get("package", {})
        zone = rapl.get("core", {})
        avg, top = cpu_freqs()
        t = temps()
        gpu = gpu_now()

        self.mon["pl1"].set_text(fmt_uw(pkg.get("pl1_uw")))
        self.mon["pl2"].set_text(fmt_uw(pkg.get("pl2_uw")))
        self.mon["pp0"].set_text(
            f"{'on' if zone.get('enabled') else 'off'} · {fmt_uw(zone.get('limit_uw'))}")
        self.mon["window"].set_text(f"{pkg.get('pl1_win_us', 0) / 1e6:.1f} s")
        self.mon["freq"].set_text("-" if not avg else f"{avg:.0f} MHz · {top:.0f} MHz")
        self.mon["temp"].set_text(f"{t.get('x86_pkg_temp', 0):.1f} °C")
        self.mon["gt"].set_text(
            f"{gpu.get('act_mhz')} · {gpu.get('min_mhz')} · {gpu.get('max_mhz')} MHz")
        thr = [f"pl1={gpu.get('pl1')}", f"pl2={gpu.get('pl2')}",
               f"pl4={gpu.get('pl4')}", f"thermal={gpu.get('thermal')}"]
        self.mon["throttle"].set_text(" ".join(thr))
        svc = {}
        try:
            svc = core.competitors()
        except Exception:
            pass
        self.mon["svc"].set_text(" ".join(f"{k}={v}" for k, v in svc.items()) or "-")
        self.mon["tuned"].set_text(str(core.tuned_profile()))

    def refresh(self):
        rail = core.rapl_snapshot()
        pkg = rail.get("package", {})
        zone = rail.get("core", {})
        core_msr = pkg.get("pl1_uw")
        pl2 = pkg.get("pl2_uw")
        self.hero_line.set_text(f"{fmt_uw(core_msr)} sustained  ·  {fmt_uw(pl2)} peak")
        self.hero_sub.set_text(
            f"EPP {core.get_epp()}  ·  thermal "
            f"{read_text('/sys/firmware/acpi/platform_profile', '?')}  ·  cores (PP0) "
            f"{'capped at ' + fmt_uw(zone.get('limit_uw')) if zone.get('enabled') else 'no cap'}")

        for child in list(self.hero_badges):
            self.hero_badges.remove(child)
        try:
            comp = core.competitors()
        except Exception:
            comp = {}
        ativos = [k for k, v in comp.items() if v == "active" and k in ("tuned", "thermald")]
        if ativos:
            self.hero_badges.append(
                self._label(f"can override: {', '.join(ativos)}", "pill pill-warn"))

        for name, (row, check, _btn) in self.profile_rows.items():
            check.set_visible(name == self.active_profile)

        if self.stack.get_visible_child_name() == "monitor":
            self.update_monitor()


# ---- application
class ThrottleFedApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = ThrottleFedWindow(application=self)
        win.present()

    def do_startup(self):
        Adw.Application.do_startup(self)
        # the palette is dark by definition, so the app does not follow the system scheme
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        # priority USER: the user theme lives at USER(800) and would beat APPLICATION(600)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_USER)

        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<primary>q"])
        self.set_accels_for_action("win.snapshot", ["<primary>s"])


def main():
    if "--version" in sys.argv:
        print("throttlefed gui 0.1 · core", core.VERSION)
        return 0
    return ThrottleFedApp().run(sys.argv[:1])


if __name__ == "__main__":
    sys.exit(main())
