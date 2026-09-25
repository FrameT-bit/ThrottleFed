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
plugins/base.py - the contract every platform plugin implements.

The core of throttlefed drives what Intel and ACPI give every machine: RAPL power
limits, HWP energy preference, turbo ratios, i915 RPS, the generic ACPI
platform_profile. Anything that belongs to one vendor instead of the platform goes
in a plugin, so the core stays honest on hardware it has never seen and a vendor
channel can be added without touching it.

A plugin never imports the core. It receives a Context with the readers the core
already has, so a plugin cannot invent a privileged path on its own: writes are
declared as a Channel and go through the same helper and the same whitelist as
everything else.
"""

import importlib.util
import sys
from pathlib import Path

###############################################
# DEFINITIONS
###############################################


class Context:
    """What the core hands to a plugin: readers, writer, and where things live."""

    def __init__(self, read, read_int, write_sysfs, plan_line, root, verbose=False):
        self.read = read
        self.read_int = read_int
        self.write_sysfs = write_sysfs
        self.plan_line = plan_line
        self.root = Path(root)
        self.verbose = verbose


class Channel:
    """One knob a plugin adds to the plan.

    `target` is what the privileged helper receives: a whitelisted path or a
    pseudo-target the helper knows how to resolve and validate.

    A channel is one of two kinds, and the firmware decides which: a list of values
    (enum, drawn as a picker) or a number. A range drawn as a picker would mean
    inventing the values in between it; a list drawn as a number field would mean
    accepting values the firmware never offered. `lo` and `hi` are filled in only
    when the machine publishes them, and firmware that publishes neither still gets
    a number field, because the driver is the one that checks it.
    """

    def __init__(self, key, label, target, values=None, why="", group="Firmware", role="",
                 number=False, lo=None, hi=None, unit=""):
        self.key = key
        self.label = label
        self.target = target
        self.values = list(values or [])
        self.why = why
        self.group = group
        self.role = role
        self.number = bool(number)
        self.lo = lo
        self.hi = hi
        self.unit = unit

    @property
    def kind(self):
        return "number" if self.number else "enum"


class Section:
    """A read-only block of rows a plugin contributes to probe and status."""

    def __init__(self, title, rows, note=""):
        self.title = title
        self.rows = list(rows)
        self.note = note


class Plugin:
    """Base class. Every method is optional except detect()."""

    ID = ""
    NAME = ""
    # What a front end calls the tab it gives this plugin. Empty means "use NAME".
    TAB = ""
    VENDOR = ""
    WHY = ""
    # The original tools this plugin is a compatibility layer for, one dict each:
    # name, creator, repo, license, reuse, note. A front end shows them without
    # reading this plugin's code, and they are shown, not buried: code that stands
    # on someone else's work says whose work it is.
    CREDITS: tuple = ()

    def __init__(self, ctx):
        self.ctx = ctx

    def detect(self) -> bool:
        """True when this plugin applies to the machine it is running on."""
        return False

    def sections(self):
        return []

    def channels(self):
        return []

    def thermal_map(self):
        """Named profile to vendor value, for the modes this vendor owns."""
        return {}

    def capture(self):
        """Extra stock fields, namespaced by the plugin."""
        return {}

    def restore(self, stock, dry=False):
        """Put captured vendor state back. Returns [(ok, message)]."""
        return []

    def warnings(self):
        return []

    def report(self) -> dict:
        """Everything a front end needs to draw this plugin, without importing it."""
        return {
            "id": self.ID,
            "name": self.NAME,
            "tab": self.TAB or self.NAME,
            "vendor": self.VENDOR,
            "why": self.WHY,
            "channels": [{"key": c.key, "label": c.label, "target": c.target,
                          "values": c.values, "group": c.group, "role": c.role,
                          "kind": c.kind, "lo": c.lo, "hi": c.hi, "unit": c.unit,
                          "why": c.why}
                         for c in self.channels()],
            "sections": [{"title": s.title, "rows": s.rows, "note": s.note}
                         for s in self.sections()],
            "credits": [dict(c) for c in self.CREDITS],
            "warnings": self.warnings(),
        }

###############################################
# ENGINE
###############################################


def load(ctx, directories):
    """Import every plugin found, isolate failures, keep only what detects.

    A broken plugin must not take the tool down: the error is kept and reported,
    because a plugin that fails to import is a fact the user needs to see.
    """
    found, errors, seen = [], [], set()
    # one contract instance only: a plugin that imports this module by name must
    # get the same class objects the loader checks against, or issubclass() lies
    here = sys.modules[__name__]
    for alias in ("throttlefed_plugin_api", "base"):
        sys.modules.setdefault(alias, here)
    for directory in directories:
        directory = Path(directory)
        if not directory.is_dir():
            continue
        # the contract module is imported by name from inside a plugin
        if str(directory) not in sys.path:
            sys.path.insert(0, str(directory))
        # Two layouts are read here. A loose .py next to the contract is the layout of
        # a plugin being written; a directory with a plugin.py in it is the layout of
        # an installed one, and the directory name is the plugin id. The store's own
        # packages sit two levels down and are therefore never imported directly: an
        # offered plugin is not an installed one.
        for path in sorted(directory.glob("*.py")) + sorted(directory.glob("*/plugin.py")):
            if path.parent.name == "store":
                continue
            stem = path.parent.name if path.name == "plugin.py" else path.stem
            if path.name in ("base.py", "__init__.py") or stem in seen:
                continue
            seen.add(stem)
            try:
                spec = importlib.util.spec_from_file_location(f"throttlefed_plugin_{stem}", path)
                if spec is None or spec.loader is None:
                    errors.append((path.name, "cannot build an import spec"))
                    continue
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)
            except Exception as exc:
                errors.append((path.name, f"{type(exc).__name__}: {exc}"))
                continue
            for obj in vars(module).values():
                if (isinstance(obj, type) and issubclass(obj, Plugin) and obj is not Plugin
                        and getattr(obj, "ID", "")):
                    try:
                        inst = obj(ctx)
                        if inst.detect():
                            found.append(inst)
                    except Exception as exc:
                        errors.append((path.name, f"detect(): {type(exc).__name__}: {exc}"))
    return found, errors
