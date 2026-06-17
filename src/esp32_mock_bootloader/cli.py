# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""CLI for esp32-mock-bootloader."""

from __future__ import annotations

import argparse
import json
import sys

from esp32_mock_bootloader import chips, daemon
from esp32_mock_bootloader.server import is_serial_port_name, run_pty_server, run_server

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


def _resolve_query_targets(
    port: int | str | None,
) -> tuple[str, int | list[dict]]:
    """Return ('single', port) or ('multi', running_instances)."""
    if port is not None and port != 'all':
        return 'single', int(port)

    running = daemon.list_running_daemons()
    if port == 'all':
        return 'multi', running

    if len(running) > 1:
        return 'multi', running
    if len(running) == 1:
        return 'single', int(running[0]['port'])
    return 'single', daemon.DEFAULT_PORT


def _print_single_status(info: dict, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(info, indent=2))
        return
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


def _print_single_url(port: int, bind: str) -> None:
    state = daemon.read_state(port)
    if state and state.get('url'):
        print(state['url'])
    else:
        print(daemon.socket_url(port, bind))


def _print_single_port_number(port: int) -> None:
    state = daemon.read_state(port)
    if state and state.get('port') is not None:
        print(state['port'])
    else:
        print(port)


def _format_status_table(instances: list[dict]) -> str:
    header = f"{'PORT':<8} {'PID':<8} {'CHIP':<10} {'DETECTED':<12} {'MODE':<12} URL"
    lines = [header, '-' * len(header)]
    for info in instances:
        detected = info.get('detected_chip') or '-'
        mode = info.get('mode') or '-'
        lines.append(
            f"{info['port']:<8} "
            f"{info['pid']!s:<8} "
            f"{info['chip']!s:<10} "
            f"{detected!s:<12} "
            f"{mode!s:<12} "
            f"{info['url']}",
        )
    return '\n'.join(lines) + '\n'


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
    if args.pty:
        client_port = args.port if is_serial_port_name(args.port) else None
        run_pty_server(
            args.timeout,
            args.pty_path_file,
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
    mode, target = _resolve_query_targets(args.port)
    if mode == 'single':
        daemon.stop_daemon(int(target))
        return 0

    instances = target
    if not instances:
        return 0
    for info in instances:
        daemon.stop_daemon(int(info['port']))
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    mode, target = _resolve_query_targets(args.port)
    if mode == 'single':
        info = daemon.daemon_status(int(target))
        _print_single_status(info, json_output=args.json)
        return 1 if not info['running'] else 0

    instances = target
    if not instances:
        if args.json:
            print(json.dumps({'instances': []}, indent=2))
        else:
            print('no running mock bootloader instances', file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({'instances': instances}, indent=2))
    else:
        sys.stdout.write(_format_status_table(instances))
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
    mode, target = _resolve_query_targets(args.port)
    if mode == 'single':
        _print_single_url(int(target), args.bind)
        return 0

    instances = target
    if not instances:
        print('no running mock bootloader instances', file=sys.stderr)
        return 1
    for info in instances:
        print(f"{info['port']}\t{info['url']}")
    return 0


def _cmd_port(args: argparse.Namespace) -> int:
    mode, target = _resolve_query_targets(args.port)
    if mode == 'single':
        _print_single_port_number(int(target))
        return 0

    instances = target
    if not instances:
        print('no running mock bootloader instances', file=sys.stderr)
        return 1
    for info in instances:
        print(info['port'])
    return 0


def _cmd_erase_flash(args: argparse.Namespace) -> int:
    mode, target = _resolve_query_targets(args.port)
    try:
        if mode == 'single':
            ports = daemon.erase_flash(port=int(target))
        else:
            ports = daemon.erase_flash(port='all')
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
    mode = run_p.add_mutually_exclusive_group()
    mode.add_argument('--pty', action='store_true',
                      help='Serial path mode: PTY (Unix), com0com COM pair, or socket URL fallback')
    run_p.add_argument('--pty-path-file',
                       help='Output file for the client port (PTY path, COM name, or socket:// URL)')
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
