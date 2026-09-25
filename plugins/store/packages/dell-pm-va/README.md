# Dell Power Manager compatibility

What this plugin drives, and what it deliberately leaves alone.

## The channel

Dell publishes part of the thermal and power policy as **BIOS firmware attributes**
through `dell-wmi-sysman`, not as CPU registers:

```
/sys/class/firmware-attributes/dell-wmi-sysman/attributes/<name>/
    current_value      root only
    possible_values    what the firmware accepts
    type               int / enum
```

This is the same class of interface the platform_profile does not cover: the value
is firmware state, it survives a reboot, and on some machines it takes effect before
the kernel starts.

## How Dell Power Manager does it

The cross-platform re-implementation
([alexVinarskis/dell-powermanager](https://github.com/alexVinarskis/dell-powermanager))
writes the same values by shelling out to **Dell Command | Configure** (`cctk`), which
requires root and, being a full BIOS configuration tool, can also change Secure Boot,
TPM and boot order through the very same attribute directory.

This plugin goes to the attributes directly. Same firmware state, same values, and no
root binary in the path that could be pointed at something else.

## What is here

| Dell Power Manager | This plugin |
| --- | --- |
| Thermal Management: Optimized / Cool / Quiet / UltraPerformance | channel `fwa:ThermalManagement`, and `--thermal` for the profile side |
| Battery Charge Configuration: Adaptive / Standard / Express / Primarily AC / Custom | channel `fwa:PrimaryBattChargeCfg` |
| Custom charge window | channels `fwa:CustomChargeStart`, `fwa:CustomChargeStop` |
| Peak Shift on battery | channels `fwa:PeakShiftCfg`, `fwa:PeakShiftBatteryThreshold` |
| BIOS values shown for information | reported in the plugin section, never written from here |

## What is not here

- **Secure Boot, TPM, virtualization, asset and password attributes.** They live in
  the same directory and `cctk` can change them. This plugin does not touch them.
- **`cctk` itself.** The attributes carry the values, so no root tool is needed.
- **Windows-only features** (ExpressCharge behaviour on AC, battery health
  reporting). They are not published through firmware attributes on this class of
  machine, so there is nothing to read.

## Notes that matter in practice

- `current_value` is root only, and this kernel publishes no `is_readonly`. Whether an
  attribute accepts a write is only proved by writing it and reading it back.
- A write can set `pending_reboot`: the firmware stores the value and applies it on the
  next boot. The plugin reports that flag when it is set.
- The firmware thermal mode and the ACPI `platform_profile` are two channels to the same
  policy. The ACPI one is runtime, the BIOS attribute is firmware state and can win
  before the kernel loads. If a limit reads back and does nothing, check for a BIOS
  setting that contradicts it.

## License

The plugin is original code, GPL-3.0, part of ThrottleFed. The upstream application is
credited as the reference for which attributes matter; no code was taken from it.
