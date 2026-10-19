# MS43X VE auto-tuner

Automatic tuning tool for the **BMW Siemens MS43** ECU running the
[MS43X custom firmware](https://www.ms4x.net/index.php?title=MS43X_Custom_Firmware)
in speed-density (MAP) mode. It reads your XDF definition, your ROM binary and
CSV datalogs, learns how far every cell of the eight MAP-based VE tables
(VANOS overlap steps 1–8) is off, and produces a ready-to-flash `.bin`.

Created by **[sorek.uk](https://sorek.uk)** · hosted instance:
**[ms43x.sorek.uk](https://ms43x.sorek.uk)**

## The community behind it

This tool only makes sense together with the MS4x community's work:

- **[MS4x.net](https://www.ms4x.net/)** — the MS4x wiki and community; reverse-
  engineered documentation, definition files and tools for Siemens MS4x ECUs.
- **[MS43X Custom Firmware](https://www.ms4x.net/index.php?title=MS43X_Custom_Firmware)**
  ([GitHub](https://github.com/ms4x-net/MS43X-Custom-Firmware)) — the custom
  firmware that adds speed-density / MAP-based load with eight VANOS-dependent
  VE tables to the MS43. This tool tunes exactly those tables.
- **[MS4x Flasher](https://www.ms4x.net/index.php?title=MS4x_Flasher)** —
  read/write your MS43 over OBD; use it to read the baseline ROM and flash the
  tuned result back.

## What it does

- **XDF-driven**: finds the `.ve<N>.` table family + shared RPM/MAP axes in any
  MS43 definition automatically (base offset applied, byte order auto-detected).
- **3D histogram accumulation**: every log sample is attributed by the *active
  VE table* channel — fractional values (e.g. 4.3) split 70/30 between adjacent
  tables, matching the ECU's VANOS interpolation — and spread bilinearly over
  the four surrounding MAP × RPM cells.
- **Correction source** (per sample):
  - *Wideband AFR vs target* — `AFR ÷ AFR target`
  - *AFR + fuel trims* — **trims priority**: trim ≠ 0 → `1 + trims/100` (the
    ECU is already compensating and the wideband reads the corrected mixture);
    trim = 0 → `AFR ÷ target` (open loop — the wideband is the truth)
  - *Fuel trims only* — `1 + (STFT + LTFT)/100`, lambda/AFR channels disabled
- **Filters**: RPM window, plausible-AFR window, warm-engine coolant threshold,
  transient slew limiter, plus **arbitrary value rules** — any logged column
  with an operator (`< ≤ > ≥ = ≠`) and threshold. A seeded default
  `accelerator pedal (%) > 1` keeps only samples with the throttle applied.
- **Per-cell parameter previews**: measured AFR, target, lambda, sample count,
  or **any** logged column (knock, IAT, pedal …) averaged into the grid.
- **Multiple logs**: add as many CSVs as you like — they must share identical
  headers (mismatches are rejected). Channels are auto-detected and re-mappable.
- **Outputs**: the tuned binary (only VE bytes differ from your original —
  everything else is byte-identical) and a full CSV change report.

## Download & run

### Hosted

Nothing to install — open **[ms43x.sorek.uk](https://ms43x.sorek.uk)**.
Every browser session is isolated and RAM-only: your ROM and logs are never
stored on the server.

### Windows standalone

Grab `MS43XVEAutoTuner.exe` from the
[**Releases**](../../releases) page and double-click it. A dedicated app window
opens (backed by a local server); closing the window stops the server.
Nothing is installed; your recent files are remembered in `%LOCALAPPDATA%\MS4X-VE-AutoTune`.

### From source

```sh
pip install -r requirements.txt
python server.py            # app window; --browser or --no-ui also available
```

### Docker (your own instance)

```sh
# Linux / macOS
./deploy/build-container.sh

# Windows
deploy\build-container.bat
```

Either script builds the image and serves the web UI on
`http://localhost:8080` (`--port` to change). The container keeps **no data on
disk at all** — every browser session lives in RAM only, so your ROM and logs
never touch the server's filesystem. Point more browsers at the same server and
each gets its own isolated session.

## Workflow

1. Read your ROM with [MS4x Flasher](https://www.ms4x.net/index.php?title=MS4x_Flasher).
2. Load XDF → binary → one or more datalogs.
3. Check the auto-detected channel mapping, pick a correction source, tune filters.
4. **Analyze** → inspect the Δ% heat maps (red = was lean, blue = was rich).
5. Iterate: flash, log, re-analyze, until |Δ| < ~3% everywhere you drive.
6. Save/download the tuned binary, flash it, verify.

## Safety / no warranty

> **This software is provided for off-road and closed-course use only.**
> Flashing a wrong binary can brick your ECU or damage your engine.
> Always verify the produced file, keep a backup of your original ROM, and
> review every changed cell before flashing.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED. See [LICENSE](LICENSE).

## Development

See [AGENTS.md](AGENTS.md) for architecture notes and build details. The
Dockerfile lives in [`deploy/`](deploy/). Continuous builds: the release
workflow packages the Windows exe for every `v*` tag.

## License

[MIT](LICENSE) © sorek.uk
