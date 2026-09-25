# ThrottleFed

![ThrottleFed](assets/banner.png)

Power budget control for Linux laptops. The CPU and the iGPU share one thermal
envelope, and this sets the split through the kernel interfaces that already exist,
then goes back to check whether the hardware obeyed.

## Why

On Windows this job belongs to ThrottleStop: power limits, Speed Shift, turbo caps,
TCC offset, all in one window. Every one of those knobs exists on Linux too, but
scattered across four subsystems with three naming conventions. A short map:

| ThrottleStop | Where it lives on Linux |
|---|---|
| PL1 / PL2 / tau (TPL) | `intel-rapl:0/constraint_{0,1}_power_limit_uw`, `constraint_1_time_window_us` |
| PP0 (cores) and PP1 (uncore) share | RAPL subzones `intel-rapl:0:0`, `intel-rapl:0:1` |
| Speed Shift EPP | `energy_performance_preference`, or bits 31:24 of MSR `0x774` |
| Turbo ratio limits | `scaling_max_freq`, or MSR `0x771` |
| TjMax and TCC offset | `proc_thermal/tcc_offset_degree_celsius` |
| DPTF power limits | `proc_thermal/power_limits/*` and `platform_profile` |
| Undervolt (FIVR) | nothing: MSR `0x150` is locked since Plundervolt |
| CPU and GPU share one budget | no engine in mainline Linux |
| Fan control | `thermal/cooling_device*/cur_state` |

Finding the files is the easy half. The hard half is that writing them does not mean
they take effect. On the machine this was written on, a load that actually saturates
the package showed the limit obeyed on the way down (10 W requested, 9.94 W drawn,
against 15.68 W with the limit at 15 W), while the envelope really in force was the
firmware policy: `power_limits/power_limit_0_max_uw`. The silicon advertises a 28 W
cTDP-up tier that no Linux path ever asks for. RAPL there is a brake, not an
accelerator.

Windows has a runtime controller that writes those limits on its own. On Linux you
ask, and the firmware decides. This tool makes both halves visible: what was asked,
and what changed.

## What it does

- Applies named profiles or ad-hoc values across RAPL (PL1, PL2, burst window, PP0
  core cap), HWP energy preference, turbo caps, thermal mode and the iGPU RPS range.
- Captures the factory state once, before the first write, and can always go back to
  it. That reference is never overwritten with values the tool wrote itself.
- Keeps the profile in place across reboots and against `thermald` and `tuned`, which
  rewrite the same registers: a boot unit plus a 60 second timer.
- Reports what is happening: watts, MHz, throttle reason, and who else is competing
  for the same knobs.
- Runs a self-test that writes, reads back through both sysfs and the MSR, and
  restores, even if a step fails halfway.
- Ships an optional GTK4 app for the same thing with sliders and a live chart.

## Requirements

- An Intel platform with RAPL and HWP (`intel_pstate`), i915 RPS, and ACPI
  `platform_profile`. Detection is by capability, so a missing knob is reported as
  missing instead of crashing. There is no model list.
- Python 3.11 or newer. The CLI uses the standard library only.
- The `msr` module for the paths that go through `/dev/cpu/*/msr`:
  `sudo modprobe msr`.
- For the GUI: GTK4, libadwaita and PyGObject on the system Python, plus `gcc` to
  build the privileged helper.

## Install

```bash
git clone https://github.com/FrameT-bit/throttlefed
cd throttlefed
sudo python3 throttlefed.py probe   # what this machine exposes, and what competes
sudo python3 throttlefed.py demo    # write, verify through sysfs and MSR, restore
```

The GUI helper is C, compiled and installed at that time:

```bash
./setup.sh --dry-run     # prints every step, writes nothing
./setup.sh               # GTK4 wizard: compiled C helper, polkit policy,
                         # launchers in /usr/local/bin and the menu entry
./setup.sh --no-gui      # install from the terminal, no wizard
./setup.sh --status      # what is installed right now, changes nothing
```

The wizard installs, besides the helper and the launchers, the two fonts the
interface asks for (`Google Sans Flex`, SIL Open Font License, in `assets/fonts`)
into `/usr/local/share/fonts/throttlefed`, and refreshes the font cache with
`fc-cache`. Without them the toolkit falls back to the distribution default face,
so the interface would look different on every machine. `uninstall` removes the
directory again; deleting it by hand only changes the look, nothing else.

For the systemd unit and the drift timer:

```bash
sudo python3 throttlefed.py install   # service + timer, persists across reboots
sudo python3 throttlefed.py uninstall
```

## Usage

```bash
sudo python3 throttlefed.py probe                    # full inventory, works partly without root
sudo python3 throttlefed.py status                   # short summary
sudo python3 throttlefed.py apply gpu                # move budget to the iGPU
sudo python3 throttlefed.py apply cpu-max            # move budget to the CPU
sudo python3 throttlefed.py set --pl1 22 --pl2 38 --core 10 --epp balance_power
sudo python3 throttlefed.py watch --interval 1       # live: watts, MHz, throttle reason
sudo python3 throttlefed.py restore                  # back to the captured stock
sudo python3 throttlefed.py auto                     # reassert the active profile
sudo python3 throttlefed.py demo                     # self-test, always restores
sudo python3 throttlefed.py experimental --force-bit 0 off
python3 throttlefed.py update-check                  # is there a newer release?
```

`--dry-run` works on every command that writes. `--if-drift` writes only what
changed, which is how the timer uses it. `--takeover` stops `thermald` and `tuned`
instead of racing them. `--reset-stock` recaptures the factory reference on purpose,
and `--force` allows values the tool considers extreme.

Built-in profiles: `balanced` (stock), `cpu`, `cpu-max`, `gpu`, `gpu-max`, `quiet`,
`burst`. They can be edited, and new ones added, in `/etc/throttlefed/profiles.json`,
which `install` creates. `macbook` is still accepted as an alias of `burst`.

## The GUI

`throttlefed_gui.py` runs under `/usr/bin/python3` with GTK4 and libadwaita. The app
never runs as root. Only the C helper is elevated, through pkexec, and the polkit
policy authorizes exactly one path: `/usr/local/libexec/throttlefed-helper`. That
helper validates each target against a closed whitelist, writes it, and reads it
back before reporting.

## Development status

CLI is version 1.0.0 and stable for what it covers: probe, apply, restore, watch,
demo and the systemd timer are all exercised on real hardware, and `demo` covers the
round trip through sysfs and MSR.

Not done yet:

- CPU and GPU power sharing stays emulated, by pinning the PP0 cap and PL1, because
  mainline Linux has no DPTF engine. The `workload_type` hint from DPTF was measured
  here and does nothing: five types, variation under 0.2 W.
- `experimental --force-bit` touches MSR `0x1FC` (widely reported as BD PROCHOT). The
  bit is not confirmed on this generation, so it sits behind its own command and
  `--force`.
- TCC offset is read-only on this platform (`EPERM`), and undervolt is gone for good.

## Compatibility

Compatibility is a property of the platform, not the model: if RAPL, HWP, i915 RPS
and `platform_profile` are there, the tool will drive them. The capability probe is
what the contract is written against.

If you run it on another machine, `probe` prints everything a report needs. The
useful reports are the ones where a knob reads back exactly what was written and the
watts do not move: that is a platform that takes the request and ignores it, and it
is worth knowing about.

## This tool is not for

- **Undervolting.** The voltage offset interface is locked since Plundervolt. There
  is nothing here to unlock.
- **Raising the platform ceiling.** Where the firmware fixes a wattage tier, asking
  for more is accepted by the register and ignored by the platform. `probe` prints
  the ceiling the platform declares, so the answer is visible instead of implied.
- **Replacing a runtime power controller.** There is no DPTF equivalent in mainline
  Linux. This sets values and holds them; it does not decide anything for you.
- **Fan control**, beyond writing the thermal cooling device state as root.
- **Guessing.** A field that fails to decode is printed raw, and a value that could
  not be confirmed is reported as unconfirmed.

## How the measurements work

Readback proves the kernel accepted a write. It does not prove the hardware obeyed.
Those are two different claims, and they are always reported separately.

- **Watts** from the delta of the RAPL `energy_uj` counter under a workload that
  really saturates the package, with an idle sample for the baseline, so the number
  is draw and not a limit read off a register.
- **Clock** as the kernel reports it: `scaling_cur_freq` for the CPU,
  `rps_act_freq_mhz` for the iGPU. Reported raw, without smoothing or inference.
- **Clock, when a number has to stand up in an argument**, from APERF and MPERF, with
  the reference taken from `MSR_PLATFORM_INFO[15:8]` and cross-checked against
  `turbostat`. That is not the CLI. It lives in `tools/clock_and_clamp.py`, next to the
  load it drives (`tools/flops.c`), so the claims in this README can be re-run instead of
  trusted.
- **Obedience** by writing a value, reading it back, and then measuring the envelope
  again under load. A register that reads back correctly while the wattage stays put
  is reported as unchanged. That is a result, not a failure.
- **Ceiling** from the firmware's own declaration, `power_limit_0_max_uw` and the
  cTDP table, which is what tells the difference between a limit that was set and a
  limit the platform was ever going to allow.

`demo` closes the loop end to end: apply, read back through sysfs and through the
MSR, restore, 12 checks. It proves nothing broke on the way back. It is not proof of
effect.

## Updates

```bash
python3 throttlefed.py update-check            # is there a newer release?
python3 throttlefed.py update-check --json     # the whole answer, for scripts
```

```text
throttlefed 1.0.0
  published      : 1.0.0  (branch)
  updates        : none, this is the newest published version
  no token and no query string: nothing about this machine is sent
```

Exit codes: `0` this build is the newest, `1` a newer version is published, `2` the
check could not run.

What it does, exactly:

- Four questions, in order: the newest release, the newest tag, `VERSION` on `main`
  through `api.github.com`, then the same file straight from `raw.githubusercontent`.
  The first one that answers with a version wins; the last one exists because the
  unauthenticated API allows 60 requests an hour per IP and the raw host has no such
  limit.
- No token, no query string, no machine identifier. The whole walk is capped at 15
  seconds, and a channel that errors out does not end it, because a home link drops
  the odd connection and the next question may still answer.
- The answer is cached in `$XDG_CACHE_HOME/throttlefed/update.json` for 24 hours, so
  a check that finds a fresh entry does not open a socket at all. A failure is cached
  for 15 minutes only, and it keeps the previous answer, printed as `last answer`:
  one dropped connection should not become the answer for a day, and it should not
  erase what a good check already knew either.
- `--offline` reads the cache and never opens a socket. `--force` ignores a fresh one.
  `THROTTLEFED_NO_UPDATE_CHECK=1` turns the whole thing off.
- Nothing is downloaded and nothing is installed. The check reports, you decide.

The GUI runs the same check once a day, in a background thread, and only when the
cache is stale. A newer version reveals a banner whose button opens the releases
page; a failure stays silent. The diagnostics view shows the local version and the
result of the last check.

For the maintainer: the channel is release, then tag, then the `VERSION` file on
`main`. To publish 1.1.0, bump `VERSION` and push, or tag `v1.1.0`.

## Notes

- `thermald` and `tuned` are active by default on Fedora and rewrite PL1, PL2 and
  EPP. The timer reasserts the profile every 60 seconds, with the first pass 25
  seconds after boot. `--takeover` is the other option: stop them and own the
  registers, which also means owning what they were doing for you.
- The stock reference is captured once, at the first write, into
  `~/.local/state/throttlefed/stock.json` (`/var/lib/throttlefed/stock.json` as
  root). The active profile lives next to it, in `active.json`.
- Commands that write need root and say so. `probe` and `status` run partly without
  it, and mark the values that were blocked.

## Contributing

Run `probe` and `status`, then the command that misbehaved, and include all three
outputs in the report. The interesting case is a value that reads back correct while
the measurement does not move, because that tells us where the platform stops
listening.

## Credits

Design, architecture and measurement method: FrameT
Code implementation: DeepSeek

## License

GPL-3.0. You can use, study, share and modify it; if you distribute a modified
version, it has to stay under the same license and carry the source. No warranty.
