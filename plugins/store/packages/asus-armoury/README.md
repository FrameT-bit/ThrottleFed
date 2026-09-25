# ASUS ROG power tunables (asus-armoury)

What this plugin drives, and what it deliberately leaves alone.

## The channel

ASUS publishes the package power limits, the GPU power and the panel state as **BIOS
firmware attributes** through the kernel driver `asus-armoury`, not as CPU registers:

```
/sys/class/firmware-attributes/asus-armoury/attributes/<name>/
    current_value      what the firmware holds now
    possible_values    only for the switches that offer a list
    type               enumeration / integer / bool
```

The driver is in the mainline kernel. Without it the attributes do not exist, and this
plugin reports nothing rather than guessing.

## How asusctl does it

[asusctl](https://github.com/OpenGamingCollective/asusctl), by Luke Jones and the
asus-linux project (now the Open Gaming Collective), is a daemon (`asusd`) with a D-Bus
client (`asusctl`) and a graphical front end (`rog-control-center`). It exposes the same
firmware knobs safely over D-Bus and keeps state that outlives a session.

None of it is required here. The kernel already publishes the values, so this plugin
writes them through the same privileged helper everything else in ThrottleFed uses, and
no daemon has to be installed or running.

## What is here

| asusctl | This plugin |
| --- | --- |
| Battery charge thresholds | reported only: this kernel marks `charge_mode` read only |
| Power profile management | the `platform_profile` channel the core already writes |
| PPT sliders | channels `fwa:ppt_pl1_spl`, `fwa:ppt_pl2_sppt`, `fwa:ppt_pl3_fppt`, `fwa:ppt_apu_sppt`, `fwa:ppt_platform_sppt` |
| GPU MUX toggling and the eGPU switch | reported only: applied on the next boot, or it disables the dGPU |
| GPU power detail (dynamic boost, TGP, thermal target) | channels `fwa:nv_dynamic_boost`, `fwa:nv_tgp`, `fwa:nv_temp_target` |
| Custom fan curves | not here: the core drives the fan policy through `platform_profile` |
| Keyboard lighting, per key RGB, AniMe Matrix, POST audio | the lighting is not a power surface; `boot_sound` is offered as a switch |

## What is not here

- **`apu_mem`** (how much system RAM the APU may take). It sits in the same directory and
  it is a memory split, not a power setting.
- **`gpu_mux_mode` and `egpu_enable`.** Both are writable, but the driver marks the first
  as applying on the next boot and the second as also disabling the dGPU, so they are
  reported instead of written.
- **A range for the power limits.** The driver carries the limits for this model and
  refuses what is outside them. Publishing a range here would be inventing numbers.

## Notes that matter in practice

- The power limits are kept **per power source**: the driver holds one set for AC and one
  for battery, answers with the set in use, and a write changes only that set. The capture
  records which source it read from, and the restore says so when the machine is on the
  other one.
- The kernel publishes **no `min_value` or `max_value`** for the limits, so those fields
  carry no range. The driver refuses an out of range value with `-EINVAL` before the
  firmware sees it, which means a wrong number is a refusal and not a damaged machine.
- On some models a power limit write is refused with `-EBUSY` until a custom fan curve is
  active. If a write fails here, that is the first thing to check.
- `gpu_mux_mode` and `panel_hd_mode` are marked by the driver as applying on the next
  boot, and a write can set `pending_reboot`.

## License

The plugin is original code, GPL-3.0, part of ThrottleFed. asusctl, the `asus-armoury`
kernel driver and Armoury Crate are credited as the references for which attributes
matter and what they mean; no code was taken from any of them.
