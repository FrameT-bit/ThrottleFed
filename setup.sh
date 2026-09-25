#!/usr/bin/env bash
# Friendly entry point for the ThrottleFed installer.
#
#   ./setup.sh                run the graphical wizard
#   ./setup.sh --demo         walk through the wizard without touching the system
#   ./setup.sh --dry-run      wizard that only shows what the install would do
#   ./setup.sh --no-gui       skip the wizard and install from the terminal
#   ./setup.sh --uninstall    remove everything the installer added
#   ./setup.sh --status       report what is installed right now
#
# Only installer-backend.sh is ever given root, through pkexec when it is
# available and sudo otherwise. The wizard itself runs as your normal user.

set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
BACKEND=$SCRIPT_DIR/installer-backend.sh
WIZARD=$SCRIPT_DIR/installer.py
SYSTEM_PYTHON=/usr/bin/python3

DEMO=0
DRY_RUN=0
NO_GUI=0
ACTION=wizard
EXTRA=()

usage() {
    sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

wizard_available() {
    [[ -x $SYSTEM_PYTHON ]] || return 1
    PYTHONDONTWRITEBYTECODE=1 "$SYSTEM_PYTHON" -c \
        'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); from gi.repository import Gtk, Adw' \
        2>/dev/null
}

gui_packages() {
    case "$(. /etc/os-release 2>/dev/null; printf '%s' "${ID:-unknown}")" in
    fedora | rhel | centos | rocky | almalinux) printf 'sudo dnf install python3-gobject gtk4 libadwaita' ;;
    debian | ubuntu | linuxmint | pop | zorin | kali) printf 'sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1' ;;
    arch | manjaro | endeavouros) printf 'sudo pacman -S python-gobject gtk4 libadwaita' ;;
    *) printf 'install PyGObject, GTK4 and libadwaita for /usr/bin/python3' ;;
    esac
}

privileged_run() {
    if [[ $(id -u) == 0 ]]; then
        "$BACKEND" "$@"
        return
    fi
    local rc=0
    if command -v pkexec >/dev/null 2>&1; then
        pkexec "$BACKEND" "$@" || rc=$?
    elif command -v sudo >/dev/null 2>&1; then
        printf 'pkexec is missing: falling back to sudo, which asks for the password here.\n' >&2
        sudo "$BACKEND" "$@" || rc=$?
    else
        printf 'Neither pkexec nor sudo is available: run %s as root yourself.\n' "$BACKEND" >&2
        return 1
    fi
    if ((rc == 126 || rc == 127)); then
        printf 'The administrator prompt was dismissed or the password was refused (status %d). Nothing was changed.\n' "$rc" >&2
    fi
    return "$rc"
}

while [[ $# -gt 0 ]]; do
    case $1 in
    --demo) DEMO=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --no-gui) NO_GUI=1 ;;
    --uninstall | --remove) ACTION=uninstall ;;
    --status) ACTION=status ;;
    -h | --help)
        usage
        exit 0
        ;;
    -*) EXTRA+=("$1") ;;
    *) EXTRA+=("$1") ;;
    esac
    shift
done

[[ -x $BACKEND || -f $BACKEND ]] || {
    printf 'installer-backend.sh is missing next to setup.sh (%s).\n' "$BACKEND" >&2
    exit 1
}

case $ACTION in
status)
    "$BACKEND" status "${EXTRA[@]+"${EXTRA[@]}"}"
    exit $?
    ;;
uninstall)
    privileged_run uninstall "${EXTRA[@]+"${EXTRA[@]}"}"
    exit $?
    ;;
esac

if [[ $NO_GUI == 1 ]]; then
    if [[ $DRY_RUN == 1 ]]; then
        # The same records the wizard would show, printed to the terminal.
        "$BACKEND" plan "${EXTRA[@]}"
        exit $?
    fi
    privileged_run install --no-gui "${EXTRA[@]}"
    exit $?
fi

if [[ $DRY_RUN == 0 && $DEMO == 0 ]]; then
    if wizard_available; then
        exec env PYTHONDONTWRITEBYTECODE=1 "$SYSTEM_PYTHON" "$WIZARD" "${EXTRA[@]}"
    fi
    printf 'The graphical wizard needs GTK4 and libadwaita for the system Python.\n' >&2
    printf 'Missing here, install them with:\n    %s\n' "$(gui_packages)" >&2
    printf 'Falling back to the non-interactive install (no desktop application).\n' >&2
    privileged_run install --no-gui "${EXTRA[@]}"
    exit $?
fi

if [[ $DRY_RUN == 1 ]]; then
    if wizard_available; then
        exec env PYTHONDONTWRITEBYTECODE=1 "$SYSTEM_PYTHON" "$WIZARD" --dry-run "${EXTRA[@]}"
    fi
    printf 'No wizard available, printing the plan from the backend instead.\n' >&2
    "$BACKEND" plan "${EXTRA[@]}"
    exit $?
fi

if ! wizard_available; then
    printf 'The demo needs the same GTK4 and libadwaita stack as the wizard:\n    %s\n' "$(gui_packages)" >&2
    exit 1
fi
exec env PYTHONDONTWRITEBYTECODE=1 "$SYSTEM_PYTHON" "$WIZARD" --demo "${EXTRA[@]}"
