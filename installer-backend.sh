#!/usr/bin/env bash
# Privileged side of the ThrottleFed installer. installer.py only draws the
# wizard: every write to the system happens here, and the wizard calls this
# script through pkexec, so the graphical part never runs as root.
#
# Usage:
#   installer-backend.sh check       read-only report of this machine
#   installer-backend.sh plan        print every action, write nothing
#   installer-backend.sh install     install (needs root: pkexec or sudo)
#   installer-backend.sh uninstall   remove what was installed (needs root)
#   installer-backend.sh status      what is installed right now
#
# Flags:
#   --dry-run      keep every mutation as a printed line, never execute it
#   --source DIR   checkout to install from (default: the directory of this script)
# --no-gui       skip the desktop application (GUI, launcher, entry, icon, fonts)
#   --no-service   skip the boot service and the 60 s drift timer
#   --no-stock     skip capturing the current state as the restore target
#
# One record per line, so callers parse state instead of prose:
#   STEP|<percent>|<phase>|<detail>
#   CHECK|ok|fail|<name>|<detail>
#   STATUS|present|missing|<path>|<mode owner:group>
#   LOG|<text>
#   RESULT|ok|fail|<detail>          (always the last line)

set -Eeuo pipefail

# ---------------------------------------------------------------------------
# definitions
# ---------------------------------------------------------------------------
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

SOURCE_DIR=$SCRIPT_DIR
MODE=
DRY_RUN=0
WITH_GUI=1
WITH_SERVICE=1
WITH_STOCK=1
WORK_DIR=${TMPDIR:-/tmp}/throttlefed-install.XXXXXX
WORK_DIR_MADE=0

SYSTEM_PYTHON=/usr/bin/python3

INSTALL_DIR=/usr/local/share/throttlefed
CLI_FILE=$INSTALL_DIR/throttlefed.py
GUI_FILE=$INSTALL_DIR/throttlefed_gui.py
LAUNCHER=/usr/local/bin/throttlefed
# The menu entry runs the GUI, the shell runs the command line tool: two names,
# two launchers. An Exec pointing at the command line tool opens nothing.
GUI_LAUNCHER=/usr/local/bin/throttlefed-gui
HELPER=/usr/local/libexec/throttlefed-helper
# /usr/local, not /usr/share: that tree belongs to the distribution packages and
# is read-only on an image-based system (rpm-ostree / Silverblue). polkitd
# searches /usr/local/share/polkit-1/actions as well.
POLICY=/usr/local/share/polkit-1/actions/io.github.framet.throttlefed.policy
APPS_DIR=/usr/local/share/applications
DESKTOP_FILE=$APPS_DIR/io.github.framet.ThrottleFed.desktop
ICON_THEME_DIR=/usr/local/share/icons/hicolor
ICON_FILE=$ICON_THEME_DIR/scalable/apps/io.github.framet.ThrottleFed.svg
# The interface asks for "Google Sans Flex" by family name (assets/fonts is OFL):
# OFL): without these two static instances every fresh install silently falls
# back to the distribution default face.
FONT_DIR=/usr/local/share/fonts/throttlefed
STATE_DIR=/var/lib/throttlefed
STOCK_FILE=$STATE_DIR/stock.conf
UNIT_FILE=/etc/systemd/system/throttlefed.service
TIMER_FILE=/etc/systemd/system/throttlefed.timer
PROFILES_FILE=/etc/throttlefed/profiles.json

HELPER_SRC=
CLI_SRC=
GUI_SRC=
POLICY_SRC=
DESKTOP_SRC=
ICON_SRC=
FONT_SRC_BOLD=
FONT_SRC_MEDIUM=

# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------
step() { printf 'STEP|%s|%s|%s\n' "$1" "$2" "$3"; }
log() { printf 'LOG|%s\n' "$*"; }
check_ok() { printf 'CHECK|ok|%s|%s\n' "$1" "$2"; }
check_fail() { printf 'CHECK|fail|%s|%s\n' "$1" "$2"; }
status_line() { printf 'STATUS|%s|%s|%s\n' "$1" "$2" "$3"; }
finish() { printf 'RESULT|%s|%s\n' "$1" "$2"; }
die() {
    printf 'RESULT|fail|%s\n' "$*"
    exit 1
}

usage() {
    sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# ---------------------------------------------------------------------------
# engine: small helpers
# ---------------------------------------------------------------------------
run() {
    # Single funnel for mutations, so --dry-run cannot miss one.
    if [[ $DRY_RUN == 1 ]]; then
        log "would run: $*"
        return 0
    fi
    "$@"
}

write_file_text() {  # path mode, content on stdin
    local path=$1 mode=$2 tmp
    if [[ $DRY_RUN == 1 ]]; then
        log "would write $path (mode $mode):"
        while IFS= read -r line; do log "  $line"; done
        return 0
    fi
    tmp=$(mktemp)
    cat >"$tmp"
    install -D -m"$mode" "$tmp" "$path"
    rm -f "$tmp"
}

expect_mode() {  # path mode owner
    local path=$1 want_mode=$2 want_owner=$3 got
    [[ $DRY_RUN == 1 ]] && return 0
    got=$(stat -c '%a %U:%G' "$path" 2>/dev/null || printf 'missing')
    if [[ $got != "$want_mode $want_owner" ]]; then
        die "expected $want_mode $want_owner on $path, got $got"
    fi
    log "mode ok: $path ($got)"
}

distro_id() {
    local id=unknown
    if [[ -r /etc/os-release ]]; then
        id=$(. /etc/os-release >/dev/null 2>&1; printf '%s' "${ID:-unknown}")
    fi
    printf '%s' "$id"
}

distro_name() {
    local name=unknown
    if [[ -r /etc/os-release ]]; then
        name=$(. /etc/os-release >/dev/null 2>&1; printf '%s' "${PRETTY_NAME:-${ID:-unknown}}")
    fi
    printf '%s' "$name"
}

install_hint() {  # package names: fedora debian [arch]
    local fedora=$1 debian=$2 arch=${3:-$2}
    case $(distro_id) in
    fedora | rhel | centos | rocky | almalinux) printf 'sudo dnf install %s' "$fedora" ;;
    debian | ubuntu | linuxmint | pop | zorin | kali) printf 'sudo apt install %s' "$debian" ;;
    arch | manjaro | endeavouros) printf 'sudo pacman -S %s' "$arch" ;;
    *) printf 'install the %s package' "$fedora" ;;
    esac
}

set_source_paths() {
    HELPER_SRC=$SOURCE_DIR/src/throttlefed-helper.c
    CLI_SRC=$SOURCE_DIR/throttlefed.py
    GUI_SRC=$SOURCE_DIR/throttlefed_gui.py
    POLICY_SRC=$SOURCE_DIR/data/io.github.framet.throttlefed.policy
    DESKTOP_SRC=$SOURCE_DIR/data/io.github.framet.ThrottleFed.desktop
    ICON_SRC=$SOURCE_DIR/data/io.github.framet.ThrottleFed.svg
    FONT_SRC_BOLD=$SOURCE_DIR/assets/fonts/GoogleSansFlex-Bold.ttf
    FONT_SRC_MEDIUM=$SOURCE_DIR/assets/fonts/GoogleSansFlex-Medium.ttf
}

missing_sources() {
    local path
    for path in "$CLI_SRC" "$HELPER_SRC" "$POLICY_SRC" "$DESKTOP_SRC" "$ICON_SRC"; do
        [[ -r $path ]] || printf '%s ' "$path"
    done
    if [[ $WITH_GUI == 1 ]]; then
        for path in "$GUI_SRC" "$FONT_SRC_BOLD" "$FONT_SRC_MEDIUM"; do
            [[ -r $path ]] || printf '%s ' "$path"
        done
    fi
}

refresh_caches() {
    if [[ $DRY_RUN == 1 ]]; then
        log "would refresh: gtk-update-icon-cache $ICON_THEME_DIR / update-desktop-database $APPS_DIR"
        return 0
    fi
    command -v gtk-update-icon-cache >/dev/null 2>&1 &&
        gtk-update-icon-cache -f -t "$ICON_THEME_DIR" >/dev/null 2>&1 || true
    command -v update-desktop-database >/dev/null 2>&1 &&
        update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
    return 0
}

cleanup() {
    [[ $WORK_DIR_MADE == 1 && -n ${WORK_DIR:-} ]] && rm -rf "$WORK_DIR"
    return 0
}

require_root() {
    [[ $DRY_RUN == 1 ]] && return 0
    [[ $(id -u) == 0 ]] && return 0
    die "this subcommand needs root: run it through pkexec or sudo. Nothing was changed."
}

require_gcc() {
    command -v gcc >/dev/null 2>&1 && return 0
    die "gcc is required to build the privileged helper ($(install_hint gcc gcc)). Nothing was changed."
}

# ---------------------------------------------------------------------------
# engine: check
# ---------------------------------------------------------------------------
do_check() {
    local fails=0 vendor model out rapl

    vendor=""
    model=""
    if [[ -r /proc/cpuinfo ]]; then
        vendor=$(awk -F': ' '/^vendor_id/{print $2; exit}' /proc/cpuinfo)
        model=$(awk -F': ' '/^model name/{print $2; exit}' /proc/cpuinfo)
    fi
    if [[ $vendor == GenuineIntel ]]; then
        check_ok "Intel platform" "$vendor, ${model:-unknown model}"
    else
        check_fail "Intel platform" "vendor_id is ${vendor:-unknown}: RAPL, MSR and HWP need an Intel CPU"
        fails=$((fails + 1))
    fi

    # The package zone is found by its 'name' field: intel-rapl:0 is what the kernel
    # numbers it on most laptops, but that index is not guaranteed, and the MMIO copy
    # reports the same 'name' while the writable constraints live on the powercap zone.
    rapl=""
    for out in /sys/class/powercap/intel-rapl*/; do
        [[ -r ${out}name ]] || continue
        [[ ${out} == *intel-rapl-mmio* ]] && continue
        case "$(cat "${out}name")" in
            package-0 | package) rapl="${out%/}"; break ;;
        esac
    done
    if [[ -n $rapl ]]; then
        check_ok "RAPL powercap zone" "$rapl present (name '$(cat "$rapl/name")')"
    else
        check_fail "RAPL powercap zone" "no zone named 'package-0' under /sys/class/powercap/intel-rapl*: PL1 and PL2 cannot be read or written without the powercap driver"
    fi

    if [[ -r /sys/devices/system/cpu/intel_pstate/status ]]; then
        check_ok "HWP / intel_pstate" "intel_pstate status: $(cat /sys/devices/system/cpu/intel_pstate/status)"
    else
        check_fail "HWP / intel_pstate" "intel_pstate is not the active cpufreq driver: EPP and the Speed Shift hints need it (intel_pstate=active on the kernel command line)"
    fi

    out=""
    for out in /sys/class/drm/card*/gt_min_freq_mhz; do
        [[ -e $out ]] && break
        out=""
    done
    if [[ -n $out ]]; then
        check_ok "i915 RPS" "${out%/gt_min_freq_mhz} present, driver $(basename "$(readlink -f "${out%/gt_min_freq_mhz}/device/driver" 2>/dev/null || printf unknown)")"
    else
        check_fail "i915 RPS" "no gt_min_freq_mhz under /sys/class/drm/card*: the iGPU budget needs the i915 driver with RPS enabled"
    fi

    if [[ -r /sys/firmware/acpi/platform_profile ]]; then
        check_ok "ACPI platform profile" "$(cat /sys/firmware/acpi/platform_profile) (available: $(cat /sys/firmware/acpi/platform_profile_choices 2>/dev/null || printf '?'))"
    else
        check_fail "ACPI platform profile" "/sys/firmware/acpi/platform_profile is missing: the thermal modes (quiet, cool, performance) need the ACPI platform profile driver"
    fi

    if [[ -d /sys/module/msr ]]; then
        if [[ -e /dev/cpu/0/msr ]]; then
            if [[ -r /dev/cpu/0/msr ]]; then
                check_ok "MSR access" "/dev/cpu/0/msr present, msr module loaded, readable here"
            else
                check_ok "MSR access" "/dev/cpu/0/msr present, msr module loaded (mode $(stat -c '%a %U:%G' /dev/cpu/0/msr), root only, which is what the helper expects)"
            fi
        else
            check_fail "MSR access" "the msr module is loaded but /dev/cpu/0/msr is missing: the locked MSRs (turbo caps, EPP) are unreadable"
        fi
    else
        check_fail "MSR access" "the msr module is not loaded: sudo modprobe msr, or write msr into /etc/modules-load.d/throttlefed.conf to keep it across boots"
    fi

    if command -v gcc >/dev/null 2>&1; then
        check_ok "C compiler" "$(command -v gcc): $(gcc --version | head -1)"
    else
        check_fail "C compiler" "gcc is required to build the privileged helper ($(install_hint gcc gcc))"
        fails=$((fails + 1))
    fi

    if [[ -d /run/systemd/system ]] && command -v systemctl >/dev/null 2>&1; then
        check_ok "systemd" "$(systemctl --version | head -1)"
    else
        check_fail "systemd" "systemd is not running this session: the boot service and the 60 s timer cannot be installed (the app itself still works)"
    fi

    if command -v pkexec >/dev/null 2>&1; then
        check_ok "polkit / pkexec" "$(command -v pkexec): the GUI asks for the password through it"
    else
        check_fail "polkit / pkexec" "pkexec is missing ($(install_hint polkit policykit-1 polkit)): the GUI cannot reach the privileged helper"
        fails=$((fails + 1))
    fi

    if [[ -x $SYSTEM_PYTHON ]]; then
        if out=$(PYTHONDONTWRITEBYTECODE=1 "$SYSTEM_PYTHON" -c 'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); from gi.repository import Gtk, Adw; print(f"GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}, libadwaita {Adw.MAJOR_VERSION}.{Adw.MINOR_VERSION}")' 2>&1); then
            check_ok "System Python stack" "$SYSTEM_PYTHON $("$SYSTEM_PYTHON" -V 2>&1 | awk '{print $2}') with $out"
        else
            check_fail "System Python stack" "$SYSTEM_PYTHON has no GTK4/libadwaita ($(install_hint 'python3-gobject gtk4 libadwaita' 'python3-gi gir1.2-gtk-4.0 gir1.2-adw-1')), so the GUI and the launcher cannot run: $(printf '%s' "$out" | tail -1)"
            fails=$((fails + 1))
        fi
    else
        check_fail "System Python stack" "$SYSTEM_PYTHON is missing: the launcher and the GUI run on the distribution Python"
        fails=$((fails + 1))
    fi

    out=$(missing_sources)
    if [[ -n $out ]]; then
        check_fail "Installer files" "missing next to this script: $out"
        fails=$((fails + 1))
    else
        check_ok "Installer files" "complete checkout at $SOURCE_DIR"
    fi

    check_ok "Distribution" "$(distro_name) [$(distro_id)]"

    if ((fails > 0)); then
        finish fail "$fails requirement(s) missing"
    else
        finish ok "machine ready"
    fi
}

# ---------------------------------------------------------------------------
# engine: install
# ---------------------------------------------------------------------------
preflight() {
    local missing
    step 5 install "checking what the install needs"
    require_gcc
    if [[ $WITH_GUI == 1 ]] && ! command -v pkexec >/dev/null 2>&1; then
        die "pkexec is required for the graphical application to reach the helper ($(install_hint polkit policykit-1 polkit)). Nothing was changed."
    fi
    missing=$(missing_sources)
    [[ -z $missing ]] || die "incomplete checkout: missing $missing. Nothing was changed."
    if [[ $WITH_SERVICE == 1 ]] && ! command -v systemctl >/dev/null 2>&1; then
        WITH_SERVICE=0
        log "systemctl is missing: the boot service is skipped"
    fi
    if [[ $WITH_GUI == 1 && ! -r $GUI_SRC ]]; then
        WITH_GUI=0
        log "$GUI_SRC is not readable: the desktop application is skipped"
    fi
}

compile_helper() {
    step 15 install "compiling the privileged helper (gcc -O2 -Wall -Wextra)"
    if [[ $DRY_RUN == 1 ]]; then
        log "would run: gcc -O2 -Wall -Wextra -o $WORK_DIR/throttlefed-helper $HELPER_SRC"
        return 0
    fi
    if [[ $WORK_DIR_MADE == 0 ]]; then
        # Built in a private directory: nothing is written into the checkout, and
        # the result is installed with the exact mode instead of chmod-ed later.
        WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/throttlefed-install.XXXXXX")
        WORK_DIR_MADE=1
        trap cleanup EXIT
    fi
    if ! gcc -O2 -Wall -Wextra -o "$WORK_DIR/throttlefed-helper" "$HELPER_SRC" 2>"$WORK_DIR/gcc.log"; then
        sed 's/^/LOG|gcc: /' "$WORK_DIR/gcc.log" || true
        die "gcc failed to build the helper (compiler output above). Nothing was changed."
    fi
    sed 's/^/LOG|gcc: /' "$WORK_DIR/gcc.log" 2>/dev/null || true
    log "compiled helper: $(stat -c%s "$WORK_DIR/throttlefed-helper") bytes"
}

install_helper() {
    step 35 install "helper -> $HELPER (0755 root:root)"
    run install -Dm755 "$WORK_DIR/throttlefed-helper" "$HELPER"
    expect_mode "$HELPER" 755 root:root
}

install_policy() {
    step 45 install "polkit policy -> $POLICY (0644), authorizes $HELPER only"
    # Single source for the helper path: the annotation is rewritten from $HELPER
    # while installing, so the policy cannot drift from the binary that is really
    # installed. A stale path makes pkexec fall back to the generic action: the
    # password is still asked, and the "this binary only" restriction is lost
    # without a word.
    sed -e "s|<annotate key=\"org.freedesktop.policykit.exec.path\">[^<]*</annotate>|<annotate key=\"org.freedesktop.policykit.exec.path\">$HELPER</annotate>|" \
        "$POLICY_SRC" | write_file_text "$POLICY" 0644
    if [[ $DRY_RUN == 0 ]] && ! grep -qF "policykit.exec.path\">$HELPER</annotate>" "$POLICY"; then
        die "the installed policy does not authorize $HELPER: pkexec would fall back to the generic action"
    fi
    expect_mode "$POLICY" 644 root:root
}

install_app_files() {
    step 55 install "command line tool and GUI -> $INSTALL_DIR (0644)"
    run install -d -m755 "$INSTALL_DIR"
    run install -m0644 "$CLI_SRC" "$CLI_FILE"
    if [[ $WITH_GUI == 1 ]]; then
        run install -m0644 "$GUI_SRC" "$GUI_FILE"
        expect_mode "$GUI_FILE" 644 root:root
    else
        log "skipped: desktop application not requested"
    fi
    expect_mode "$CLI_FILE" 644 root:root
}

install_launcher() {
    step 65 install "launchers -> $LAUNCHER and $GUI_LAUNCHER (0755)"
    write_file_text "$LAUNCHER" 0755 <<EOF
#!/bin/sh
# Installed by installer-backend.sh: always runs the copy under $INSTALL_DIR,
# never a checkout that may move or disappear.
exec $SYSTEM_PYTHON $CLI_FILE "\$@"
EOF
    expect_mode "$LAUNCHER" 755 root:root
    # The desktop entry calls this one: the command line tool needs a subcommand,
    # so a menu entry pointing at it would open nothing at all.
    write_file_text "$GUI_LAUNCHER" 0755 <<EOF
#!/bin/sh
# Installed by installer-backend.sh: opens the GTK4 interface from the installed
# copy, so the menu entry never runs a checkout that may have moved.
exec $SYSTEM_PYTHON $GUI_FILE "\$@"
EOF
    expect_mode "$GUI_LAUNCHER" 755 root:root
}

install_desktop_entry() {
    step 75 install "desktop entry -> $DESKTOP_FILE and icon -> $ICON_FILE (0644)"
    # The installed entry calls the launcher by NAME (no absolute path from any
    # checkout) and it must be the GUI launcher: ${GUI_LAUNCHER##*/} is the only
    # name that opens a window. TryExec has to name an installed binary too,
    # otherwise the entry disappears from the menu.
    sed -e "s|^Exec=.*|Exec=${GUI_LAUNCHER##*/}|" \
        -e "s|^TryExec=.*|TryExec=${GUI_LAUNCHER##*/}|" \
        "$DESKTOP_SRC" | write_file_text "$DESKTOP_FILE" 0644
    run install -Dm644 "$ICON_SRC" "$ICON_FILE"
    if [[ $DRY_RUN == 0 ]]; then
        grep -qF "Exec=${GUI_LAUNCHER##*/}" "$DESKTOP_FILE" ||
            die "the installed desktop entry does not run ${GUI_LAUNCHER##*/}: it would open nothing"
        grep -qF "TryExec=${GUI_LAUNCHER##*/}" "$DESKTOP_FILE" ||
            die "the installed desktop entry has no TryExec=${GUI_LAUNCHER##*/}: a failing TryExec hides the entry"
    fi
    expect_mode "$DESKTOP_FILE" 644 root:root
    expect_mode "$ICON_FILE" 644 root:root
    refresh_caches
}

install_fonts() {
    step 80 install "bundled fonts -> $FONT_DIR (0644)"
    # The interface asks for the family by name: with no such font installed the
    # toolkit silently falls back to the distribution default face. Both files
    # are OFL and ship with the source, so the look travels with the package.
    run install -d -m755 "$FONT_DIR"
    run install -m0644 "$FONT_SRC_BOLD" "$FONT_DIR/GoogleSansFlex-Bold.ttf"
    run install -m0644 "$FONT_SRC_MEDIUM" "$FONT_DIR/GoogleSansFlex-Medium.ttf"
    if command -v fc-cache >/dev/null 2>&1; then
        run fc-cache -f "$FONT_DIR" >/dev/null
    else
        log "fc-cache is not installed: the font server picks the files up on its own"
    fi
    if [[ $DRY_RUN == 0 ]] && command -v fc-list >/dev/null 2>&1; then
        if fc-list : family 2>/dev/null | grep -qi "google sans flex"; then
            check_ok "Bundled fonts" "fontconfig lists Google Sans Flex"
        else
            check_fail "Bundled fonts" "fontconfig does not list Google Sans Flex yet: the interface will use another face"
        fi
    fi
}

install_state_dir() {
    step 82 install "state directory -> $STATE_DIR (0755)"
    run install -d -m755 "$STATE_DIR"
    expect_mode "$STATE_DIR" 755 root:root
}

capture_stock() {
    if [[ $WITH_STOCK == 0 ]]; then
        log "skipped: stock capture not requested"
        return 0
    fi
    if [[ $DRY_RUN == 0 && -s $STOCK_FILE ]]; then
        # Rewriting it would destroy the factory reference the app restores to.
        log "a factory snapshot already exists in $STOCK_FILE: kept as the restore target"
        return 0
    fi
    step 88 install "capturing the current state as the restore target (helper capture)"
    run "$HELPER" capture
    if [[ $DRY_RUN == 0 && ! -s $STOCK_FILE ]]; then
        die "the helper did not write $STOCK_FILE"
    fi
}

install_service() {
    if [[ $WITH_SERVICE == 0 ]]; then
        log "skipped: boot service not requested"
        return 0
    fi
    step 92 install "boot service and 60 s drift timer (units written by the installed command line tool)"
    # Delegated to the installed copy on purpose: the unit then runs the copy in
    # $INSTALL_DIR, never the checkout this installer was started from.
    run "$SYSTEM_PYTHON" "$CLI_FILE" install
    if [[ $DRY_RUN == 0 ]]; then
        # Matched without pinning the whole line: the interpreter and the quoting
        # style of the unit are the command line tool's business, not this script's.
        grep -qF "$CLI_FILE" "$UNIT_FILE" || die "$UNIT_FILE does not run $CLI_FILE"
        log "unit ok: $(grep -F 'ExecStart=' "$UNIT_FILE")"
        if command -v systemctl >/dev/null 2>&1; then
            log "timer: $(systemctl is-enabled throttlefed.timer 2>&1 || true) / $(systemctl is-active throttlefed.timer 2>&1 || true)"
        fi
    fi
}

verify_install() {
    step 98 install "verifying the installed files"
    expect_mode "$HELPER" 755 root:root
    expect_mode "$POLICY" 644 root:root
    expect_mode "$CLI_FILE" 644 root:root
    expect_mode "$STATE_DIR" 755 root:root
    if [[ $WITH_GUI == 1 ]]; then
        expect_mode "$GUI_FILE" 644 root:root
        expect_mode "$LAUNCHER" 755 root:root
        expect_mode "$GUI_LAUNCHER" 755 root:root
        expect_mode "$DESKTOP_FILE" 644 root:root
        expect_mode "$ICON_FILE" 644 root:root
        expect_mode "$FONT_DIR/GoogleSansFlex-Bold.ttf" 644 root:root
        expect_mode "$FONT_DIR/GoogleSansFlex-Medium.ttf" 644 root:root
    fi
    [[ $DRY_RUN == 1 ]] && return 0
    if ! "$SYSTEM_PYTHON" "$CLI_FILE" --version >/dev/null 2>&1; then
        die "the installed command line tool does not run: $SYSTEM_PYTHON $CLI_FILE --version failed"
    fi
    log "command line tool: $("$SYSTEM_PYTHON" "$CLI_FILE" --version)"
    log "helper self report: $("$HELPER" paths 2>&1 | head -c 200)"
}

do_install() {
    require_root
    preflight
    compile_helper
    install_helper
    install_policy
    install_app_files
    if [[ $WITH_GUI == 1 ]]; then
        install_launcher
        install_desktop_entry
        install_fonts
    fi
    install_state_dir
    capture_stock
    install_service
    verify_install
    step 100 install "done"
    if [[ $DRY_RUN == 1 ]]; then
        finish ok "plan only: nothing was written"
    elif [[ $WITH_GUI == 1 ]]; then
        finish ok "installed. Open it from the application menu or run $GUI_LAUNCHER"
    else
        finish ok "installed without the desktop application: run $LAUNCHER"
    fi
}

# ---------------------------------------------------------------------------
# engine: uninstall and status
# ---------------------------------------------------------------------------
do_uninstall() {
    require_root
    step 10 uninstall "stopping the boot service and removing its units"
    if [[ -r $CLI_FILE ]]; then
        if [[ $DRY_RUN == 1 ]]; then
            log "would run: $SYSTEM_PYTHON $CLI_FILE uninstall"
        else
            "$SYSTEM_PYTHON" "$CLI_FILE" uninstall || log "the command line tool reported a problem while removing the units"
        fi
    else
        run systemctl disable --now throttlefed.timer
        run systemctl stop throttlefed.service
        run rm -f "$UNIT_FILE" "$TIMER_FILE"
        run systemctl daemon-reload
    fi

    step 40 uninstall "removing the installed files"
    run rm -f "$LAUNCHER" "$GUI_LAUNCHER" "$DESKTOP_FILE" "$ICON_FILE" "$POLICY" "$HELPER"
    run rm -rf "$INSTALL_DIR"
    run rm -rf "$FONT_DIR"
    if command -v fc-cache >/dev/null 2>&1; then run fc-cache -f >/dev/null; fi
    refresh_caches

    step 90 uninstall "keeping the saved state"
    log "kept: $STOCK_FILE and $PROFILES_FILE (delete them by hand for a clean slate)"
    finish ok "removed. The factory snapshot in $STATE_DIR was kept"
}

do_status() {
    local path mode
    step 10 status "installed files"
    for path in "$HELPER" "$POLICY" "$CLI_FILE" "$STATE_DIR" "$LAUNCHER" "$GUI_LAUNCHER" "$DESKTOP_FILE" "$ICON_FILE" "$FONT_DIR" "$UNIT_FILE" "$TIMER_FILE"; do
        if [[ -e $path ]]; then
            mode=$(stat -c '%a %U:%G' "$path" 2>/dev/null || printf '?')
            status_line present "$path" "$mode"
        else
            status_line missing "$path" ""
        fi
    done
    step 60 status "service"
    if command -v systemctl >/dev/null 2>&1; then
        log "throttlefed.timer: $(systemctl is-enabled throttlefed.timer 2>&1 || true) / $(systemctl is-active throttlefed.timer 2>&1 || true)"
    else
        log "systemctl is not available on this system"
    fi
    step 80 status "restore target and profiles"
    if [[ -s $STOCK_FILE ]]; then
        log "$STOCK_FILE: present, $(grep -c '=' "$STOCK_FILE" 2>/dev/null || printf '?') keys"
    else
        log "$STOCK_FILE: not captured yet"
    fi
    [[ -s $PROFILES_FILE ]] && log "$PROFILES_FILE: present" || log "$PROFILES_FILE: missing"
    finish ok "status report complete"
}

# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case $1 in
    check | plan | install | uninstall | status) MODE=$1 ;;
    --dry-run) DRY_RUN=1 ;;
    --source)
        [[ -n ${2:-} ]] || die "--source needs a directory"
        SOURCE_DIR=$(cd -- "$2" && pwd) || die "no such directory: $2"
        shift
        ;;
    --no-gui) WITH_GUI=0 ;;
    --no-service) WITH_SERVICE=0 ;;
    --no-stock) WITH_STOCK=0 ;;
    -h | --help)
        usage
        exit 0
        ;;
    *) die "unknown argument: $1" ;;
    esac
    shift
done

set_source_paths

case $MODE in
check) do_check ;;
plan)
    DRY_RUN=1
    do_install
    ;;
install) do_install ;;
uninstall) do_uninstall ;;
status) do_status ;;
*)
    usage >&2
    exit 2
    ;;
esac
