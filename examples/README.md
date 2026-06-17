# Usage examples

Self-contained scripts that show how to use **esp32-mock-bootloader** as an upload target. Each file imports only `esp32_mock_bootloader` (and the Python/stdlib modules it needs).

## Setup

```bash
pip install -e .
```

## Run

```bash
# Basic — mock endpoint + esptool upload
python examples/basic/mock_endpoint.py
python examples/basic/esptool_upload.py
python examples/basic/context_manager.py
python examples/basic/two_mocks.py

# Advanced — protocol, transport, raw SLIP
python examples/advanced/daemon_vs_foreground.py
python examples/advanced/protocol_chip_detect.py
python examples/advanced/protocol_flash_write.py
python examples/advanced/transport_tcp_connect.py
python examples/advanced/handle_advanced_raw_slip.py
python examples/advanced/verify_upload_with_protocol.py
```

## What the mock does

The mock does **not** upload firmware for you. It exposes a `socket://…` URL. You pass that URL to your upload client (`esptool`, `arduino-cli`, …) or talk to it over the ROM protocol.

```
mock_bootloader(chip="esp32")  →  mock.url()  →  esptool --port <url> write-flash …
                                              →  advanced.protocol.connect(mock)
```

## Basic examples

| File | What it shows |
|------|----------------|
| [`basic/mock_endpoint.py`](basic/mock_endpoint.py) | Start a mock, read `url` / `port` |
| [`basic/esptool_upload.py`](basic/esptool_upload.py) | Point esptool at `mock.url()` |
| [`basic/context_manager.py`](basic/context_manager.py) | Same upload inside `with mock_bootloader(...)` |
| [`basic/two_mocks.py`](basic/two_mocks.py) | Two chips → two URLs → two esptool uploads |

## Advanced examples

Import from `esp32_mock_bootloader.advanced` when you need protocol or transport building blocks.

| File | What it shows |
|------|----------------|
| [`advanced/daemon_vs_foreground.py`](advanced/daemon_vs_foreground.py) | Foreground (default) vs `mode="daemon"` |
| [`advanced/protocol_chip_detect.py`](advanced/protocol_chip_detect.py) | `protocol.connect()` + `read_reg()` — chip detect without esptool |
| [`advanced/protocol_flash_write.py`](advanced/protocol_flash_write.py) | Custom upload: `FLASH_BEGIN` / `DATA` / `END` + stub readback |
| [`advanced/transport_tcp_connect.py`](advanced/transport_tcp_connect.py) | `transport.connect(port)` + `protocol_client` helpers |
| [`advanced/handle_advanced_raw_slip.py`](advanced/handle_advanced_raw_slip.py) | `mock.advanced.send_raw()` for raw SLIP frames |
| [`advanced/verify_upload_with_protocol.py`](advanced/verify_upload_with_protocol.py) | esptool upload, then `protocol.connect()` readback |

Foreground mocks stop automatically when the handle is destroyed. Use `with mock_bootloader(...)` for deterministic teardown in tests.

## Tests

```bash
pytest tests/test_examples.py -n0 -q
```

Upload-related examples are marked `@pytest.mark.esptool`.
