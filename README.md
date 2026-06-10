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
  "chip": "auto",
  "url": "socket://127.0.0.1:9876",
  "detected_chip": "esp32"
}
```

`detected_chip` is filled after a client identifies the SoC (in `auto` mode).

### Chip selection

| Mode | When to use |
|------|-------------|
| `--chip auto` | Client passes its own `--chip`; mock learns from registers (recommended for CI matrices) |
| `--chip esp32`, `esp32c6`, … | Fixed profile for every session |

In `auto` mode the mock:

1. Returns a ROM-style error on the first `GET_SECURITY_INFO` so clients do not lock onto ESP32 via `chip_id` 0.
2. Returns `0` for the legacy probe at `0x40001000` until chip-specific registers identify the SoC.
3. Sets `detected_chip` from unique detect registers or efuse windows (addresses from esptool ROM classes).

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
  --com-port COM18 --com-peer COM19 --chip auto
esptool --chip esp32 --port COM19 write-flash 0x10000 firmware.bin
```

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

## How it works

The mock speaks SLIP-framed ROM commands. Implemented handlers include:

`SYNC`, `FLASH_BEGIN` / `DATA` / `END`, `FLASH_DEFL_*`, `MEM_*` (+ OHAI after `MEM_END`), `READ_REG`, `GET_SECURITY_INFO`, `WRITE_REG`, `SPI_SET_PARAMS`, `SPI_ATTACH`, `CHANGE_BAUDRATE`, `SPI_FLASH_MD5`.

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
- **Alpha API** — expect changes before `1.0.0`.

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
├── src/esp32_mock_bootloader/   # Python package (CLI, daemon, SLIP server)
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
