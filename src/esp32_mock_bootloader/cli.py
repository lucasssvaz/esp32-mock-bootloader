# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""CLI for esp32-mock-bootloader."""

from __future__ import annotations

import argparse
import json
import sys

from esp32_mock_bootloader import chips, daemon, instances

CHIP_CHOICES = sorted(chips.PROFILES.keys()) + ['auto']


def _parse_target_port(value: str) -> int | str:
    if value == 'all':
        return 'all'
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f'invalid port {value!r} (use a TCP port number or "all")',
        ) from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f'port out of range: {port}')
    return port


def _add_target_port_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        '--port', default=None, type=_parse_target_port, metavar='PORT',
        help=(
            'TCP port; omit to auto-pick the only running instance '
            f'(or {daemon.DEFAULT_PORT} if none); "all" lists or affects every instance'
        ),
    )


def _add_chip_port_bind_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        '--chip', default='auto', choices=CHIP_CHOICES,
        help='Chip profile for READ_REG / GET_SECURITY_INFO (default: auto)',
    )
    parser.add_argument('--port', type=int, default=daemon.DEFAULT_PORT,
                        help=f'TCP port (default: {daemon.DEFAULT_PORT})')
    parser.add_argument('--bind', default=daemon.DEFAULT_BIND,
                        help=f'Bind address (default: {daemon.DEFAULT_BIND})')


def _cmd_run(args: argparse.Namespace) -> int:
    from esp32_mock_bootloader.server import is_serial_port_name, run_pty_server, run_server

    port_file = getattr(args, 'port_file', None)
    if args.pty:
        client_port = args.port if is_serial_port_name(args.port) else None
        run_pty_server(
            args.timeout,
            port_file,
            args.chip,
            client_port=client_port,
            serial_bind=args.serial_bind,
            exit_on_disconnect=args.exit_on_disconnect,
        )
    else:
        run_server(
            int(args.port),
            args.timeout,
            args.chip,
            args.bind,
            exit_on_disconnect=args.exit_on_disconnect,
            track_registry=not args.daemon_child,
            port_file=port_file,
        )
    return 0


def _cmd_start(args: argparse.Namespace) -> int:
    try:
        data = daemon.start_daemon(
            port=args.port,
            chip_mode=args.chip,
            startup_timeout=args.startup_timeout,
            bind=args.bind,
            force=args.force,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Started mock bootloader pid={data['pid']} url={data['url']}")
    return 0


def _cmd_stop(args: argparse.Namespace) -> int:
    instances.stop(args.port)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    fmt = 'json' if args.json else 'text'
    data = instances.status(args.port, format='data')
    if fmt == 'json':
        instances.status(args.port, format='json', file=sys.stdout)
        if isinstance(data, dict) and not data.get('running'):
            return 1
        if isinstance(data, list) and not data:
            return 1
        return 0

    if isinstance(data, dict):
        if not data.get('running'):
            status_text = 'stopped'
            print(f'status: {status_text}')
            return 1
        print(f"status: running")
        print(f"pid: {data['pid']}")
        print(f"port: {data['port']}")
        print(f"chip: {data['chip']}")
        print(f"detected_chip: {data['detected_chip']}")
        print(f"url: {data['url']}")
        if data.get('log_file'):
            print(f"log_file: {data['log_file']}")
        return 0

    if not data:
        print('no running mock bootloader instances', file=sys.stderr)
        return 1
    instances.status(args.port, format='text', file=sys.stdout)
    return 0


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
    result = instances.url(args.port, bind=args.bind)
    if isinstance(result, str):
        print(result)
        return 0
    if not result:
        print('no running mock bootloader instances', file=sys.stderr)
        return 1
    for port, url in result:
        print(f'{port}\t{url}')
    return 0


def _cmd_port(args: argparse.Namespace) -> int:
    result = instances.port(args.port)
    if isinstance(result, int):
        print(result)
        return 0
    if not result:
        print('no running mock bootloader instances', file=sys.stderr)
        return 1
    for port in result:
        print(port)
    return 0


def _cmd_erase_flash(args: argparse.Namespace) -> int:
    try:
        ports = instances.erase_flash(args.port)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if len(ports) == 1:
        print(f'Erased mock flash (port {ports[0]})')
    else:
        joined = ', '.join(str(p) for p in ports)
        print(f'Erased mock flash (ports {joined})')
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
    run_p.add_argument(
        '--chip', default='auto', choices=CHIP_CHOICES,
        help='Chip profile for READ_REG / GET_SECURITY_INFO (default: auto)',
    )
    run_p.add_argument(
        '--port', default=str(daemon.DEFAULT_PORT), metavar='PORT',
        help=(
            'TCP listen port (default: %(default)s); '
            'with --pty in null-modem mode, upload client serial port (e.g. COM19)'
        ),
    )
    run_p.add_argument('--bind', default=daemon.DEFAULT_BIND,
                       help=f'Bind address (default: {daemon.DEFAULT_BIND})')
    run_p.add_argument(
        '--timeout', type=float, default=None,
        help='Exit after N seconds (default: run until interrupted or stopped)',
    )
    run_p.add_argument(
        '--exit-on-disconnect', action='store_true',
        help='Exit after the first client disconnects (TCP, PTY, and COM)',
    )
    run_p.add_argument(
        '--daemon-child', action='store_true',
        help=argparse.SUPPRESS,
    )
    run_p.add_argument(
        '--port-file',
        help=argparse.SUPPRESS,
    )
    mode = run_p.add_mutually_exclusive_group()
    mode.add_argument('--pty', action='store_true',
                      help='Serial path mode: PTY (Unix), com0com COM pair, or socket URL fallback')
    run_p.add_argument(
        '--serial-bind',
        help=(
            'Mock-side serial port in a null-modem pair (--pty); '
            'auto-detected from com0com when only --port is set'
        ),
    )
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

    stop_p = sub.add_parser('stop', help='Stop background daemon(s)')
    _add_target_port_arg(stop_p)
    stop_p.set_defaults(func=_cmd_stop)

    status_p = sub.add_parser('status', help='Show daemon status')
    _add_target_port_arg(status_p)
    status_p.add_argument('--bind', default=daemon.DEFAULT_BIND)
    status_p.add_argument('--json', action='store_true', help='JSON output')
    status_p.set_defaults(func=_cmd_status)

    chips_p = sub.add_parser(
        'chips',
        help='List supported SoCs (from installed esptool)',
    )
    chips_p.add_argument('--json', action='store_true', help='JSON output')
    chips_p.set_defaults(func=_cmd_chips)

    url_p = sub.add_parser('url', help='Print socket:// URL(s)')
    _add_target_port_arg(url_p)
    url_p.add_argument('--bind', default=daemon.DEFAULT_BIND,
                       help='Bind address when resolving a single stopped port')
    url_p.set_defaults(func=_cmd_url)

    port_p = sub.add_parser('port', help='Print TCP listen port number(s)')
    _add_target_port_arg(port_p)
    port_p.set_defaults(func=_cmd_port)

    erase_p = sub.add_parser(
        'erase-flash',
        help='Erase mock SPI flash on running daemon(s) (resets to 0xFF)',
    )
    _add_target_port_arg(erase_p)
    erase_p.set_defaults(func=_cmd_erase_flash)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    raise SystemExit(main())
