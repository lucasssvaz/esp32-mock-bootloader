# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""CLI for esp32-mock-bootloader."""

from __future__ import annotations

import argparse
import json
import sys

from esp32_mock_bootloader import chips, daemon
from esp32_mock_bootloader.server import run_pty_server, run_server

CHIP_CHOICES = sorted(chips.PROFILES.keys()) + ['auto']


def _add_chip_port_bind_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        '--chip', default='auto', choices=CHIP_CHOICES,
        help='Chip profile for READ_REG / GET_SECURITY_INFO (default: auto)',
    )
    parser.add_argument('--port', type=int, default=daemon.DEFAULT_PORT,
                        help=f'TCP port (default: {daemon.DEFAULT_PORT})')
    parser.add_argument('--bind', default=daemon.DEFAULT_BIND,
                        help=f'Bind address (default: {daemon.DEFAULT_BIND})')
    parser.add_argument('--state-dir', default=None,
                        help='Daemon state directory (default: ~/.cache/esp32-mock-bootloader)')


def _cmd_run(args: argparse.Namespace) -> int:
    if args.pty:
        run_pty_server(
            args.timeout,
            args.pty_path_file,
            args.chip,
            args.state_file,
            com_port=args.com_port,
            com_peer=args.com_peer,
        )
    else:
        run_server(
            args.port, args.timeout, args.chip, args.bind, args.state_file,
        )
    return 0


def _cmd_start(args: argparse.Namespace) -> int:
    try:
        data = daemon.start_daemon(
            port=args.port,
            chip_mode=args.chip,
            startup_timeout=args.startup_timeout,
            bind=args.bind,
            base=args.state_dir,
            force=args.force,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Started mock bootloader pid={data['pid']} url={data['url']}")
    return 0


def _cmd_stop(args: argparse.Namespace) -> int:
    daemon.stop_daemon(args.port, args.state_dir)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    info = daemon.daemon_status(args.port, args.state_dir)
    if args.json:
        print(json.dumps(info, indent=2))
    else:
        status = 'running' if info['running'] else 'stopped'
        print(f"status: {status}")
        if info['running']:
            print(f"pid: {info['pid']}")
            print(f"port: {info['port']}")
            print(f"chip: {info['chip']}")
            print(f"detected_chip: {info['detected_chip']}")
            print(f"url: {info['url']}")
            if info.get('log_file'):
                print(f"log_file: {info['log_file']}")
    return 1 if not info['running'] else 0


def _cmd_chips(args: argparse.Namespace) -> int:
    if args.json:
        payload = {
            name: {
                'detect_reg': f'0x{profile.detect_reg:08x}',
                'detect_magic': f'0x{profile.detect_magic:08x}',
                'efuse_base': f'0x{profile.efuse_base:08x}',
                'image_chip_id': profile.image_chip_id,
            }
            for name, profile in chips.PROFILES.items()
        }
        print(json.dumps(payload, indent=2))
        return 0

    for name in chips.SUPPORTED:
        profile = chips.PROFILES[name]
        chip_id = (
            'n/a'
            if profile.image_chip_id is None
            else str(profile.image_chip_id)
        )
        print(f'{name}\tchip_id={chip_id}')
    return 0


def _cmd_url(args: argparse.Namespace) -> int:
    state = daemon.read_state(args.port, args.state_dir)
    if state and state.get('url'):
        print(state['url'])
    else:
        print(daemon.socket_url(args.port, args.bind))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='esp32-mock-bootloader',
        description='Mock ESP32 ROM bootloader for CI upload testing',
    )
    sub = parser.add_subparsers(dest='command', required=True)

    run_p = sub.add_parser(
        'run',
        help='Run server in the foreground (used internally by start)',
    )
    _add_chip_port_bind_args(run_p)
    run_p.add_argument(
        '--timeout', type=float, default=None,
        help='Exit after N seconds (default: run until interrupted or stopped)',
    )
    mode = run_p.add_mutually_exclusive_group()
    mode.add_argument('--pty', action='store_true',
                      help='Serial path mode: PTY (Unix), com0com COM pair, or socket URL fallback')
    run_p.add_argument('--pty-path-file',
                       help='Output file for the client port (PTY path, COM name, or socket:// URL)')
    run_p.add_argument(
        '--com-port',
        help='Server-side COM port (com0com pair); also ESP32_MOCK_COM_PORT',
    )
    run_p.add_argument(
        '--com-peer',
        help='Client-side COM port written to --pty-path-file; also ESP32_MOCK_COM_PEER',
    )
    run_p.add_argument('--state-file', default=None,
                       help='Update detected_chip in this JSON state file')
    run_p.set_defaults(func=_cmd_run)

    start_p = sub.add_parser(
        'start',
        help='Start background daemon (runs until stop)',
    )
    _add_chip_port_bind_args(start_p)
    start_p.add_argument(
        '--startup-timeout', '--timeout',
        type=float,
        default=daemon.DEFAULT_STARTUP_TIMEOUT,
        dest='startup_timeout',
        help=(
            'Seconds to wait for the TCP port during startup '
            f'(default: {daemon.DEFAULT_STARTUP_TIMEOUT}); --timeout is an alias'
        ),
    )
    start_p.add_argument('--force', action='store_true',
                         help='Stop existing daemon on the same port first')
    start_p.set_defaults(func=_cmd_start)

    stop_p = sub.add_parser('stop', help='Stop background daemon')
    stop_p.add_argument('--port', type=int, default=daemon.DEFAULT_PORT)
    stop_p.add_argument('--state-dir', default=None)
    stop_p.set_defaults(func=_cmd_stop)

    status_p = sub.add_parser('status', help='Show daemon status')
    status_p.add_argument('--port', type=int, default=daemon.DEFAULT_PORT)
    status_p.add_argument('--state-dir', default=None)
    status_p.add_argument('--bind', default=daemon.DEFAULT_BIND)
    status_p.add_argument('--json', action='store_true', help='JSON output')
    status_p.set_defaults(func=_cmd_status)

    chips_p = sub.add_parser(
        'chips',
        help='List supported SoCs (from installed esptool)',
    )
    chips_p.add_argument('--json', action='store_true', help='JSON output')
    chips_p.set_defaults(func=_cmd_chips)

    url_p = sub.add_parser('url', help='Print socket:// URL')
    url_p.add_argument('--port', type=int, default=daemon.DEFAULT_PORT)
    url_p.add_argument('--state-dir', default=None)
    url_p.add_argument('--bind', default=daemon.DEFAULT_BIND)
    url_p.set_defaults(func=_cmd_url)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    raise SystemExit(main())
