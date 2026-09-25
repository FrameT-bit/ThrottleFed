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
throttlefed_store.py - what is on offer, what is installed, and the only write the
store is allowed to do: put a package in place, take it back out.

This is not a package manager and does not want to be one. A plugin is a directory
with a plugin.py in it. It is installed by copying that directory into one of the
plugin search paths, and removed by deleting it again. No dependency resolution, no
version solving, no network unless an entry asks for it.

Why the store exists: the core drives what the platform gives every machine (RAPL,
HWP, i915 RPS, the ACPI platform_profile). Anything that only exists on one vendor's
hardware is a plugin. A plugin that is not installed is not imported, adds no
channel, and cannot be the reason the tool fails to start.
"""

import json
import os
import shutil
from pathlib import Path

###############################################
# DEFINITIONS
###############################################

HERE = Path(__file__).resolve().parent
PLUGINS = HERE / "plugins"
STORE = PLUGINS / "store"
CATALOG = STORE / "catalog.json"
PACKAGES = STORE / "packages"

# Written on install, read on remove. Without it the store only deletes directories
# it can prove it created, which is the difference between a mistake and an rm -rf.
MARKER = ".throttlefed-plugin"

SYSTEM_PLUGINS = Path("/usr/local/share/throttlefed/plugins")

REQUIRED_FIELDS = ("id", "name", "summary", "package")

###############################################
# ENGINE
###############################################


class StoreError(Exception):
    """Catalog or package problem, phrased for the person who has to fix it."""


def user_plugins():
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / "throttlefed" / "plugins"


def search_dirs():
    """Everywhere a plugin can live, in the order it is looked for.

    The in-tree directory first, so a checkout installs next to its own catalog and
    the plugin shows up in git status where you can see it. A system install cannot
    write there, so it falls through to the user's data directory.
    """
    return (PLUGINS, user_plugins(), SYSTEM_PLUGINS)


def catalog(path=None):
    """The index, validated. A broken catalog is reported, never guessed around."""
    path = Path(path or CATALOG)
    if not path.is_file():
        raise StoreError(f"no catalog at {path}")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise StoreError(f"{path} is not valid JSON: {exc}") from exc
    plugins = data.get("plugins")
    if not isinstance(plugins, list):
        raise StoreError(f"{path} has no 'plugins' list")
    for entry in plugins:
        if not isinstance(entry, dict):
            raise StoreError(f"{path} has an entry that is not an object")
        missing = [f for f in REQUIRED_FIELDS if not entry.get(f)]
        if missing:
            raise StoreError(f"{path}: entry '{entry.get('id', '?')}' is missing {', '.join(missing)}")
    return data


def entries(path=None):
    return catalog(path)["plugins"]


def entry(plugin_id, path=None):
    for item in entries(path):
        if item["id"] == plugin_id:
            return item
    return None


def package_dir(item):
    """Where the package for this entry lives, or None when the catalog lies."""
    declared = Path(item.get("package", ""))
    candidate = (STORE / declared).resolve()
    if not str(candidate).startswith(str(STORE.resolve())):
        # a catalog from somewhere else may not point outside the store
        return None
    return candidate if (candidate / "plugin.py").is_file() else None


def hardware(item):
    """Does this machine look like the one the plugin is for? (bool, why)."""
    for needed in item.get("requires", []):
        if not Path(needed).exists():
            return False, f"missing {needed}"
    try:
        vendor = Path("/sys/class/dmi/id/sys_vendor").read_text().strip().lower()
    except OSError:
        vendor = ""
    wanted = [v.lower() for v in item.get("vendor_match", [])]
    if wanted and not any(w in vendor for w in wanted):
        return False, f"system vendor is {vendor or 'unknown'}"
    return True, "channel present"


def installed_at(plugin_id):
    """The directory this plugin is installed in, or None.

    Every search path is read, not only the writable ones: a plugin installed
    system-wide is installed, and the store says so rather than pretending it is
    not there because this user cannot write to it.
    """
    for directory in search_dirs():
        candidate = directory / plugin_id
        if (candidate / "plugin.py").is_file():
            return candidate
    return None


def target_dir():
    """First writable search path. None when there is nowhere to install."""
    for directory in search_dirs():
        try:
            directory.mkdir(parents=True, exist_ok=True)
            if os.access(directory, os.W_OK):
                return directory
        except OSError:
            continue
    return None


def install(plugin_id):
    """Copy the package in place. Returns (ok, message)."""
    item = entry(plugin_id)
    if item is None:
        return False, f"'{plugin_id}' is not in the catalog"
    source = package_dir(item)
    if source is None:
        return False, f"'{plugin_id}' has no package in the store ({item.get('package')})"
    target = target_dir()
    if target is None:
        return False, "nowhere to install: no writable plugin directory"
    destination = target / plugin_id
    replaced = destination.is_dir()
    if replaced:
        shutil.rmtree(destination)
    try:
        shutil.copytree(source, destination)
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    (destination / MARKER).write_text(json.dumps({"id": plugin_id, "source": str(source)}) + "\n")
    return True, f"{'reinstalled' if replaced else 'installed'} in {destination}"


def remove(plugin_id):
    """Delete an installed plugin. Refuses anything the store did not write."""
    found = installed_at(plugin_id)
    if found is None:
        return False, f"'{plugin_id}' is not installed"
    if not os.access(found.parent, os.W_OK):
        return False, f"{found} is not writable by this user: remove it as root"
    marker = found / MARKER
    if not marker.is_file():
        return False, (f"{found} has no {MARKER}: it was not installed by the store, "
                       "so it is not the store's to delete")
    try:
        marked = json.loads(marker.read_text()).get("id")
    except (json.JSONDecodeError, OSError):
        marked = None
    if marked != plugin_id:
        return False, f"{marker} belongs to '{marked}', not to '{plugin_id}'"
    shutil.rmtree(found)
    return True, f"removed {found}"


def status(path=None):
    """One row per catalog entry, for the CLI and for the store page."""
    out = []
    for item in entries(path):
        where = installed_at(item["id"])
        fits, why = hardware(item)
        out.append({
            "id": item["id"],
            "name": item["name"],
            "vendor": item.get("vendor", ""),
            "tab": item.get("tab", item["name"]),
            "summary": item["summary"],
            "detail": item.get("detail", ""),
            "upstream": item.get("upstream", ""),
            "license": item.get("license", ""),
            # Structured, so the store can show who made the original tool without
            # importing the package: importing it is what installing means.
            "credits": list(item.get("credits", [])),
            "installed": bool(where),
            "path": str(where) if where else "",
            "hardware": fits,
            "hardware_note": why,
            "packaged": package_dir(item) is not None,
        })
    return out


###############################################
# EXECUTION
###############################################

if __name__ == "__main__":
    for row in status():
        state = row["path"] or ("on offer" if row["packaged"] else "no package")
        print(f"{row['id']:16s} {row['name']:34s} {state}")
        if not row["hardware"]:
            print(f"{'':16s} not for this machine: {row['hardware_note']}")
