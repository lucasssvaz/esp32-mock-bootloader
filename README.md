# esp32-mock-bootloader

A mock Espressif ROM bootloader for testing firmware uploads **without a board**.

Run it on your machine or in CI. Point **esptool**, **arduino-cli**, or any ROM-compatible flasher at a TCP port or serial path, and flash as if a chip were connected.

[![CI](https://github.com/lucasssvaz/esp32-mock-bootloader/actions/workflows/ci.yml/badge.svg)](https://github.com/lucasssvaz/esp32-mock-bootloader/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/esp32-mock-bootloader)](https://pypi.org/project/esp32-mock-bootloader/)
[![Python](https://img.shields.io/pypi/pyversions/esp32-mock-bootloader)](https://pypi.org/project/esp32-mock-bootloader/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

> **Stability:** version `0.x` is alpha. CLI flags and protocol details may change before `1.0.0`.

## Table of contents

- [Overview](#overview)
- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Usage](#usage)
  - [CLI commands](#cli-commands)
  - [Daemon lifecycle](#daemon-lifecycle)
  - [Chip selection](#chip-selection)
  - [Supported chips](#supported-chips)
- [Transports](#transports)
  - [TCP (default)](#tcp-default)
  - [PTY / serial path](#pty--serial-path)
  - [Windows with com0com](#windows-with-com0com)
- [GitHub Actions](#github-actions)
  - [Client examples](#client-examples)
  - [Python API](#python-api)
  - [How it works](#how-it-works)
- [Protocol references](#protocol-references)
- [Limitations](#limitations)
- [Development](#development)
- [Project layout](#project-layout)
- [AI disclosure](#ai-disclosure)
- [License](#license)

## Overview

Real Espressif chips expose a **ROM bootloader** over serial. Upload tools speak a SLIP-framed binary protocol: sync, detect the SoC, write flash blocks, verify MD5, and so on.

**esp32-mock-bootloader** implements enough of that protocol for upload clients to complete a full flash cycle. It does **not** run your firmware or emulate peripherals — it only answers the bootloader conversation.

```
  esptool / arduino-cli / …
           │
           ▼
  socket://127.0.0.1:PORT   or   /dev/tty* / COM*
           │
           ▼
  esp32-mock-bootloader  (SLIP server)
           │
           ▼
  ACK + chip metadata + in-memory flash image
```

Chip metadata comes from the installed **[esptool](https://github.com/espressif/esptool)** package. When Espressif adds a new SoC to esptool, this mock can support it without a code change here.

For protocol details and authoritative behavior, see [Protocol references](#protocol-references).

## Features

- **No hardware** — run upload tests locally and in CI.
- **All esptool SoCs** — profiles are built from `esptool.targets.CHIP_DEFS`.
- **Auto chip detection** — `--chip auto` learns the SoC from client traffic.
- **TCP daemon** — background `start` / `stop` with stable `socket://` URLs.
- **PTY and COM paths** — Unix PTY, Windows com0com pairs, or socket fallback.
- **GitHub Action** — one step to start the mock; teardown runs automatically.
- **CI-ready** — tested on Ubuntu, Windows, and macOS with esptool integration tests.

## Requirements

| Component | Version |
|-----------|---------|
| Python | 3.9 or newer |
| esptool | Installed automatically with this package (runtime dependency) |
| Upload client | e.g. pip `esptool`, or arduino-cli with a client that supports your transport |

Optional:

- **com0com** on Windows — for real `COMx` ports in local testing ([setup guide](#windows-with-com0com)).

## Installation

From PyPI:

```bash
pip install esp32-mock-bootloader
```

Pin a release:

```bash
pip install esp32-mock-bootloader==0.1.0
```

From source (development):

```bash
git clone https://github.com/lucasssvaz/esp32-mock-bootloader.git
cd esp32-mock-bootloader
pip install -e ".[dev]"
```

## Quick start

```bash
# 1. Start the mock (background daemon on port 9876)
esp32-mock-bootloader start

# 2. Flash through it
esptool --chip esp32 \
  --port "$(esp32-mock-bootloader url)" \
  write-flash 0x10000 firmware.bin

# 3. Stop when done
esp32-mock-bootloader stop
```

The daemon keeps running between steps 1 and 3. Use `status` to inspect it, or `url` to print the `socket://` address again.

## Usage

### CLI commands

| Command | Description |
|---------|-------------|
| `start` | Start the background daemon and exit once the port is ready |
| `stop` | Stop the daemon (safe to run if already stopped) |
| `status` | Show pid, chip mode, detected SoC, and URL (exit 1 if stopped) |
| `url` | Print `socket://127.0.0.1:PORT` |
| `chips` | List SoCs supported by the installed esptool |
| `run` | Run the server in the foreground (used internally; prefer `start`) |

Common flags for `start` and `run`:

| Flag | Default | Description |
|------|---------|-------------|
| `--chip` | `auto` | Chip profile, or `auto` to detect from client traffic |
| `--port` | `9876` | TCP port |
| `--bind` | `127.0.0.1` | Bind address |
| `--state-dir` | `~/.cache/esp32-mock-bootloader` | Daemon state and logs |
| `--startup-timeout` | `30` | Seconds `start` waits for the port (`start` only) |
| `--force` | off | Stop an existing daemon on the same port first (`start` only) |

`status --json` adds machine-readable output. Set `ESP32_MOCK_BOOTLOADER_STATE_DIR` to override the default state directory.

### Daemon lifecycle

`start` writes state to `{state-dir}/port-{port}.json` and logs to `port-{port}.log`:

```json
{
  "pid": 12345,
  "port": 9876,
  "chip": "esp32",
  "url": "socket://127.0.0.1:9876",
  "detected_chip": "esp32"
}
```

`detected_chip` is filled after a client identifies the SoC (in `auto` mode).

### Chip selection

esptool’s `--chip` flag is **client-side only** — it is never sent over the serial link. The mock cannot read it. Choose the mock profile to match how your upload client selects the SoC:

| Mock `--chip` | Client | Connect fidelity |
|---------------|--------|------------------|
| `esp32`, `esp32c3`, … | `esptool --chip <same>` | Full ROM profile (MAC, crystal, security-info) — **recommended for CI** |
| `esp32`, `esp32c3`, … | `esptool --chip auto` | esptool autodetects from ROM probes against the fixed mock profile |
| `auto` | `esptool --chip auto` | Mock learns the SoC from esptool’s standard detection probes |
| `auto` | `esptool --chip <explicit>` | Upload usually works; connect may warn until chip-specific registers are read |

**Recommended CI pattern** — one virtual board per job, matching chips:

```bash
esp32-mock-bootloader start --chip esp32c3
esptool --chip esp32c3 --port "$(esp32-mock-bootloader url)" flash-id
```

Or let esptool autodetect against a fixed mock profile:

```bash
esp32-mock-bootloader start --chip esp32c3
esptool --chip auto --port "$(esp32-mock-bootloader url)" flash-id
```

Use mock `--chip auto` when the client also uses `--chip auto`, or when you need the mock to learn the SoC from chip-specific register traffic (for example multi-SoC protocol tests).

In `auto` mode the mock:

1. Returns a ROM-style error on `GET_SECURITY_INFO` until a SoC is known.
2. Returns `0` for the legacy probe at `0x40001000` until chip-specific registers identify the SoC.
3. Sets `detected_chip` from unique detect registers or efuse windows (addresses from esptool ROM classes).

**ROM profile:** For explicit chip modes (and after auto detection), `READ_REG` returns a sparse set of register values derived from esptool ROM classes — synthetic MAC, crystal calibration (`UART_CLKDIV`, ESP32 `RTCCALICFG1`), and security-off defaults. This is not a full efuse block emulator; see [espefuse](#espefuse).

#### Synthetic MAC

esptool's `flash-id` prints a MAC decoded from efuse/OTP registers. The mock fills those registers with a **synthetic BASE_MAC** so the line is non-zero and passes each chip family's `read_mac()` logic. No particular address is required for protocol correctness.

| Property | Value |
|----------|-------|
| OUI | `24:0A:C4` (Espressif; not a real burned address on the mock) |
| Host suffix | First 3 bytes of `SHA256("<chip>")`, e.g. `esp32` → `e2:95:26` |
| Stability | Same `--chip` always yields the same MAC across runs |
| Uniqueness | Different SoC names get different suffixes (helps multi-chip CI logs) |

Examples: `esp32` → `24:0a:c4:e2:95:26`, `esp32c3` → `24:0a:c4:1f:67:7d`, `esp8266` → `24:0a:c4:ce:2b:c1`.

To compute the expected MAC in a test: `registers.mac_bytes_for_chip("esp32c3")` or the formula above. A single fixed MAC for all chips would also work with esptool; the per-chip suffix is a readability choice, not a hardware requirement.

### Supported chips

Every target in the installed esptool `CHIP_DEFS` is available:

```bash
esp32-mock-bootloader chips
esp32-mock-bootloader chips --json   # detect registers and chip_id metadata
```

New esptool releases can add SoCs without updating this package.

## Transports

### TCP (default)

The daemon listens on `127.0.0.1:PORT`. Pip-installed esptool accepts `socket://` URLs via pyserial.

```bash
esp32-mock-bootloader start --port 9876
esptool --chip esp32c6 --port socket://127.0.0.1:9876 write-flash 0x10000 app.bin
```

### VID/PID messages

esptool reads USB vendor/product IDs only from real USB serial devices (Espressif VID `0x303A`). Virtual transports cannot provide those descriptors:

| Transport | Typical esptool message | Fixable by mock? |
|-----------|-------------------------|------------------|
| `socket://` | `Device VID/PID identification is only supported on COM and /dev/ serial ports.` | No — document only |
| Unix PTY (`/dev/ttys…`) | `Failed to get VID/PID of a device on /dev/ttys…` | No — PTY is not a USB device |
| Windows com0com (`COMx`) | Same `Failed to get VID/PID` — virtual pairs have no USB descriptors | No |

These messages are harmless for upload tests. Only a physical Espressif USB-Serial/JTAG adapter silences them.

### PTY / serial path

Use `--pty` with `run` when a tool expects a **device path** instead of a URL (common with arduino-cli):

```bash
esp32-mock-bootloader run --pty --pty-path-file /tmp/mock-pty --chip esp32
esptool --chip esp32 --port "$(cat /tmp/mock-pty)" write-flash 0x10000 firmware.bin
```

| Platform | `--pty` provides |
|----------|------------------|
| Linux / macOS | Real PTY device (e.g. `/dev/ttys003`) |
| Windows (local) | com0com virtual COM pair |
| Windows (CI) | `socket://127.0.0.1:PORT` fallback when no COM pair is configured |

### Windows with com0com

Install [com0com](https://sourceforge.net/projects/com0com/). Run the mock on the **server** port; the path file receives the **client** port:

```bat
esp32-mock-bootloader run --pty --pty-path-file mock.port ^
  --com-port COM18 --com-peer COM19 --chip esp32
esptool --chip esp32 --port COM19 write-flash 0x10000 firmware.bin
```

Expect `Failed to get VID/PID` on com0com ports (see [VID/PID messages](#vidpid-messages)).

Environment variables `ESP32_MOCK_COM_PORT` and `ESP32_MOCK_COM_PEER` work the same as `--com-port` / `--com-peer`.

**Helper script** (elevated prompt; creates a pair, runs esptool, removes the pair):

```bat
python scripts\test_windows_com.py
```

Set `ESP32_MOCK_KEEP_COM_PAIR=1` to leave the pair installed after the script exits.


## GitHub Actions

The action starts the daemon in the main step and **stops it in a post step** when the job ends — including on failure. No manual `stop` required.

```yaml
jobs:
  upload-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Mock bootloader
        uses: lucasssvaz/esp32-mock-bootloader@v0.1.0
        id: mock

      - name: Flash test firmware
        run: |
          pip install esptool
          esptool --chip esp32 \
            --port "${{ steps.mock.outputs.url }}" \
            --no-stub --before no-reset --after no-reset \
            write-flash 0x10000 firmware.bin
```

**Outputs:** `url` (`socket://…`), `port`.

**Inputs** (all optional):

| Input | Default | Description |
|-------|---------|-------------|
| `chip` | `auto` | Chip profile |
| `port` | `9876` | TCP port |
| `startup-timeout` | `30` | Startup wait in seconds |
| `python-version` | `3.x` | Python for `setup-python` |
| `version` | *(empty)* | PyPI pin (e.g. `0.1.0`); omit to install from the action checkout |

**Manual CLI in a workflow** (without the action):

```yaml
- run: pip install esp32-mock-bootloader esptool
- run: esp32-mock-bootloader start
- run: |
    esptool --chip esp32 --port "$(esp32-mock-bootloader url)" \
      write-flash 0x10000 firmware.bin
- run: esp32-mock-bootloader stop
  if: always()
```

## Client examples

**esptool** (TCP, no stub — typical for CI):

```bash
esptool --chip esp32c6 \
  --port socket://127.0.0.1:9876 \
  --no-stub --before no-reset --after no-reset \
  write-flash 0x10000 app.bin
```

**arduino-cli:**

```bash
arduino-cli compile -b esp32:esp32:esp32 \
  -p socket://127.0.0.1:9876 --upload sketch.ino
```

**Shell CI script:**

```bash
esp32-mock-bootloader start
PORT="$(esp32-mock-bootloader url)"
# run your upload tests against "$PORT"
esp32-mock-bootloader stop
```

## Python API

Downstream projects can integrate the mock without shelling out to the CLI. Import **submodules by name** — avoid long symbol lists from the package root.

| Tier | Import | Use case |
|------|--------|----------|
| High-level | `from esp32_mock_bootloader import MockBootloader` | CI scripts, pytest fixtures |
| Testing helpers | `import esp32_mock_bootloader.testing as mock` | Protocol clients, esptool upload tests |
| Chip metadata | `from esp32_mock_bootloader import chips` | `chips.PROFILES`, `chips.SUPPORTED` |
| Protocol constants | `from esp32_mock_bootloader import protocol` | `protocol.CMD_SYNC`, checksum helpers |
| Daemon control | `from esp32_mock_bootloader import daemon` | Background mock (`start_daemon` / `stop_daemon`) |
| Advanced | `from esp32_mock_bootloader import server` | In-process SLIP handlers (unstable in 0.x) |

**Context manager** (recommended for tests):

```python
from esp32_mock_bootloader import MockBootloader
import esp32_mock_bootloader.testing as mock

with MockBootloader(chip="auto") as bootloader:
    sock = bootloader.connect()
    mock.protocol.send_sync(sock)
    # bootloader.url → "socket://127.0.0.1:PORT"
```

**esptool integration test** in another project:

```python
import esp32_mock_bootloader.testing as mock

with mock.server.running_server(chip="esp32") as (_proc, port):
    ok, detail = mock.esptool.write_flash_no_stub("esp32", "app.bin", port=port)
    assert ok, detail
```

Reference constants (`FLASH_APP_OFFSET`, `SYNC_PAYLOAD`, …) live in `esp32_mock_bootloader.testing.constants`.

## How it works

The mock speaks SLIP-framed ROM commands. Implemented handlers include:

`SYNC`, `FLASH_BEGIN` / `DATA` / `END`, `FLASH_DEFL_*`, `MEM_*` (+ OHAI after `MEM_END`), `READ_REG`, `WRITE_REG` (with mask, readable via `READ_REG`), `GET_SECURITY_INFO`, `SPI_SET_PARAMS`, `SPI_ATTACH`, `CHANGE_BAUDRATE`, `SPI_FLASH_MD5`, `READ_FLASH_SLOW` (ROM), and stub-only `ERASE_FLASH`, `ERASE_REGION`, `READ_FLASH` (streaming + MD5), `RUN_USER_CODE`. `FLASH_DATA`, `MEM_DATA`, and `FLASH_DEFL_DATA` validate the 0xEF XOR checksum (ROM error `0x07`, stub error `0xC1`). Unknown commands return ROM error `0x05` or stub error `0xFF`.

Flash data is stored in an in-memory image (erased bytes default to `0xFF`). After a stub upload (`MEM_END` with entrypoint + `OHAI`), `SPI_FLASH_MD5` returns a **16-byte binary** digest; in ROM mode it returns **32-byte lowercase hex ASCII** — matching esptool’s `flash_md5sum()` expectations. Unknown commands receive a generic ACK.

The mock validates **protocol behavior**, not silicon accuracy. It does not model Wi-Fi, sleep, brownout, or real flash timing.

## Protocol references

Implementation follows Espressif’s published bootloader protocol and the **esptool** reference client. Primary sources:

| Topic | Reference |
|-------|-----------|
| Serial protocol overview | [esptool serial protocol (ESP32)](https://docs.espressif.com/projects/esptool/en/latest/esp32/advanced-topics/serial-protocol.html) — same command set is documented per chip under `esptool/en/latest/<chip>/advanced-topics/serial-protocol.html` |
| Flash upload & MD5 verify | [Verifying uploaded data](https://docs.espressif.com/projects/esptool/en/latest/esp32/advanced-topics/serial-protocol.html#verifying-uploaded-data) — ROM returns 32 hex ASCII bytes; stub returns 16 raw MD5 bytes before the 2 status bytes |
| `SPI_FLASH_MD5` (`0x13`) | [Commands table](https://docs.espressif.com/projects/esptool/en/latest/esp32/advanced-topics/serial-protocol.html#supported-by-stub-loader-and-rom-loader) |
| Stub upload & `OHAI` | [Functional description — initialization](https://docs.espressif.com/projects/esptool/en/latest/esp32/advanced-topics/serial-protocol.html#functional-description) — `MEM_END` entrypoint, unsolicited `OHAI` SLIP packet |
| Client MD5 handling | [esptool `loader.py` — `flash_md5sum`](https://github.com/espressif/esptool/blob/master/esptool/loader.py) — `RESP_DATA_LEN` 32 (ROM) vs `RESP_DATA_LEN_STUB` 16 (stub); status bytes follow the digest |
| Stub lifecycle | [esptool `loader.py` — `run_stub`](https://github.com/espressif/esptool/blob/master/esptool/loader.py) — RAM download via `MEM_*`, then `mem_finish(entry)` |
| Chip profiles & detection | [esptool `targets` / `CHIP_DEFS`](https://github.com/espressif/esptool/tree/master/esptool/targets) — register addresses, magic values, security info |

**Integration tests** in downstream projects (e.g. [arduino-esp32 upload tests](https://github.com/espressif/arduino-esp32/blob/master/.github/workflows/upload-tests.yml)) exercise the default **stub** path (`flasher.py` / esptool without `--no-stub`). CI examples in this repo that pass `--no-stub` target the ROM MD5 format explicitly.

## Limitations

- **Protocol emulator only** — no application code runs on the mock.
- **Client packaging matters** — some bundled esptool builds lack `socket://`; use PTY/COM or pip esptool.
- **com0com is local** — GitHub-hosted Windows runners use the socket fallback automatically.
- **VID/PID noise on virtual ports** — socket, PTY, and com0com cannot expose Espressif USB descriptors; esptool prints informational VID/PID messages (see [Transports](#transports)).
- **Alpha API** — expect changes before `1.0.0`.

### espefuse

This mock targets **esptool** ROM upload clients (`write-flash`, `flash-id`, stub upload). It is **not** an efuse programmer.

| Tool / mode | Use with mock? |
|-------------|----------------|
| `espefuse --virt` | **Yes** — in-process efuse emulation for host-side tests (no serial port) |
| `espefuse --port …` read/burn commands | **No** — requires on-chip efuse controller `WRITE_REG` sequences and persistent burned state |

After connect, the mock exposes a **sparse ROM profile** (MAC, crystal registers, security-off defaults) so esptool connect paths behave plausibly. That is not espefuse field parity:

| espefuse command | Mock support |
|------------------|--------------|
| `summary` | Partial / best-effort only — most named fields stay at defaults |
| `dump`, `adc-info`, `check-error` | No — unmapped addresses read as `0` |
| `burn-*`, `read-protect-efuse`, `write-protect-efuse` | No — permanently out of scope unless a full efuse controller emulator is added |

Supported chips follow installed esptool `CHIP_DEFS`. [espefuse supported chips](https://github.com/espressif/esptool/tree/master/espefuse) are a subset (no ESP8266).

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for pull request guidelines and AI disclosure expectations.

```bash
git clone https://github.com/lucasssvaz/esp32-mock-bootloader.git
cd esp32-mock-bootloader
pip install -e ".[dev]"
```

**Run tests:**

```bash
pytest                                      # parallel by default (pytest-xdist)
pytest -n0                                  # single process (debugging)
pytest -m "not esptool"                     # protocol unit tests only
pytest -m "not transport"                     # skip TCP/PTY integration
pytest tests/test_transports.py             # transport matrix only
```

**Coverage** (see [`reports/README.md`](reports/README.md)):

```bash
pytest --cov=esp32_mock_bootloader \
  --cov-report=term-missing \
  --cov-report=html:reports/htmlcov \
  --cov-report=xml:reports/coverage.xml
python scripts/check_coverage.py
```

CI runs the full suite on Ubuntu, Windows, and macOS, enforces coverage baselines on Ubuntu, and uploads an HTML report artifact. Windows com0com tests run locally via `scripts/test_windows_com.py`.

**Build a release wheel:**

```bash
hatch build
```

## Project layout

```
esp32-mock-bootloader/
├── CONTRIBUTING.md              # PR guidelines and AI policy
├── src/esp32_mock_bootloader/   # Python package (api, testing, CLI, daemon, SLIP server)
│   ├── api.py                   # MockBootloader context manager
│   ├── testing/                 # Public helpers for downstream CI/tests
│   ├── chips.py                 # Chip profiles from esptool
│   ├── registers.py             # Sparse ROM register profile for esptool fidelity
│   ├── protocol.py              # Protocol constants
│   └── server.py                # SLIP server (advanced)
├── action/                      # Node.js steps for the GitHub Action
├── action.yml                   # Composite action entry point
├── tests/                       # pytest suite (protocol, esptool, transports)
├── scripts/                     # Coverage checker, Windows COM helper
├── reports/                     # Coverage config and baselines
└── .github/workflows/           # CI and release pipelines
```

## AI disclosure

This repository was developed with help from AI coding assistants. Every change is reviewed and tested by a human maintainer before merge or release.

Contributor expectations (disclosure, review, commit trailers) are in [CONTRIBUTING.md](CONTRIBUTING.md#ai-assisted-contributions).

## License

Copyright 2026 Lucas Saavedra Vaz. Released under the [Apache-2.0](LICENSE) license.
