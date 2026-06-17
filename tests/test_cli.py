# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""CLI integration and in-process unit tests."""

from __future__ import annotations

import argparse
import json

import pytest

from esp32_mock_bootloader import chips, cli, daemon

import esp32_mock_bootloader.testing as mock


def _start_via_cli(cli, port: int, chip: str = 'auto') -> None:
    result = cli('start', '--port', str(port), '--chip', chip)
    assert result.returncode == 0, result.stderr


def test_cli_lists_all_running_instances(cli, wait_for_daemons):
    port_a = mock.server.reserve_tcp_port()
    port_b = mock.server.reserve_tcp_port()
    try:
        _start_via_cli(cli, port_a, 'esp32')
        _start_via_cli(cli, port_b, 'esp32c3')
        wait_for_daemons(2)

        status = cli('status')
        assert status.returncode == 0, status.stderr
        assert str(port_a) in status.stdout
        assert str(port_b) in status.stdout
        assert 'socket://' in status.stdout

        status_json = cli('status', '--json')
        assert status_json.returncode == 0
        payload = json.loads(status_json.stdout)
        assert len(payload['instances']) == 2
        ports = {i['port'] for i in payload['instances']}
        assert ports == {port_a, port_b}

        url = cli('url')
        assert url.returncode == 0
        lines = url.stdout.strip().splitlines()
        assert len(lines) == 2
        for line in lines:
            p, u = line.split('\t', 1)
            assert u == f'socket://127.0.0.1:{p}'

        ports_out = cli('port')
        assert ports_out.returncode == 0
        assert set(ports_out.stdout.strip().splitlines()) == {str(port_a), str(port_b)}
    finally:
        daemon.stop_daemon(port_a)
        daemon.stop_daemon(port_b)


def test_cli_port_all_lists_single_instance(cli):
    port = mock.server.reserve_tcp_port()
    try:
        daemon.start_daemon(port=port, chip_mode='esp32')

        status = cli('status', '--port', 'all')
        assert status.returncode == 0
        assert 'PORT' in status.stdout
        assert str(port) in status.stdout
        assert 'status: running' not in status.stdout

        url = cli('url', '--port', 'all')
        assert url.returncode == 0
        assert url.stdout.strip() == f'{port}\tsocket://127.0.0.1:{port}'

        only_port = cli('port', '--port', 'all')
        assert only_port.returncode == 0
        assert only_port.stdout.strip() == str(port)
    finally:
        daemon.stop_daemon(port)


def test_cli_stop_port_all(cli, wait_for_daemons):
    port_a = mock.server.reserve_tcp_port()
    port_b = mock.server.reserve_tcp_port()
    try:
        _start_via_cli(cli, port_a)
        _start_via_cli(cli, port_b)
        wait_for_daemons(2)

        stop = cli('stop', '--port', 'all')
        assert stop.returncode == 0

        assert daemon.list_running_daemons() == []
    finally:
        daemon.stop_daemon(port_a)
        daemon.stop_daemon(port_b)


def test_cli_auto_stop_multiple_instances(cli, wait_for_daemons):
    port_a = mock.server.reserve_tcp_port()
    port_b = mock.server.reserve_tcp_port()
    try:
        _start_via_cli(cli, port_a)
        _start_via_cli(cli, port_b)
        wait_for_daemons(2)

        stop = cli('stop')
        assert stop.returncode == 0
        assert daemon.list_running_daemons() == []
    finally:
        daemon.stop_daemon(port_a)
        daemon.stop_daemon(port_b)


def test_cli_list_all_none_running(cli):
    status = cli('status', '--port', 'all')
    assert status.returncode == 1
    assert 'no running mock bootloader instances' in status.stderr

    url = cli('url', '--port', 'all')
    assert url.returncode == 1
    assert 'no running mock bootloader instances' in url.stderr

    port = cli('port', '--port', 'all')
    assert port.returncode == 1
    assert 'no running mock bootloader instances' in port.stderr


def test_cli_auto_picks_single_running_instance(cli):
    port = mock.server.reserve_tcp_port()
    try:
        daemon.start_daemon(port=port, chip_mode='esp32')

        status = cli('status')
        assert status.returncode == 0, status.stderr
        assert 'status: running' in status.stdout
        assert f'port: {port}' in status.stdout

        url = cli('url')
        assert url.returncode == 0
        assert url.stdout.strip() == f'socket://127.0.0.1:{port}'

        only_port = cli('port')
        assert only_port.returncode == 0
        assert only_port.stdout.strip() == str(port)
    finally:
        daemon.stop_daemon(port)


def test_cli_no_daemon_fallback_default_port(cli):
    url = cli('url')
    assert url.returncode == 0
    assert url.stdout.strip() == 'socket://127.0.0.1:9876'

    only_port = cli('port')
    assert only_port.returncode == 0
    assert only_port.stdout.strip() == '9876'

    status = cli('status')
    assert status.returncode == 1
    assert 'status: stopped' in status.stdout


def test_cli_url_default(cli):
    result = cli('url', '--port', '9876')
    assert result.returncode == 0
    assert result.stdout.strip() == 'socket://127.0.0.1:9876'


def test_cli_port_default(cli):
    result = cli('port', '--port', '9876')
    assert result.returncode == 0
    assert result.stdout.strip() == '9876'


def test_cli_invalid_port(cli):
    result = cli('status', '--port', 'not-a-port')
    assert result.returncode != 0
    assert 'invalid port' in result.stderr


def test_cli_lists_supported_chips(cli):
    result = cli('chips')
    assert result.returncode == 0
    for chip in chips.SUPPORTED:
        assert chip in result.stdout


def test_cli_chips_json(cli):
    result = cli('chips', '--json')
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert set(payload.keys()) == set(chips.SUPPORTED)
    for chip in chips.SUPPORTED:
        assert 'detect_reg' in payload[chip]


def test_cli_status_human_readable(cli):
    port = mock.server.reserve_tcp_port()
    try:
        start = cli('start', '--port', str(port), '--chip', 'auto')
        assert start.returncode == 0
        status = cli('status', '--port', str(port))
        assert status.returncode == 0
        assert 'status: running' in status.stdout
        assert f'port: {port}' in status.stdout
        assert 'url: socket://' in status.stdout
    finally:
        daemon.stop_daemon(port)


def test_cli_status_stopped_exit_code(cli):
    result = cli('status', '--port', '39703')
    assert result.returncode == 1
    assert 'status: stopped' in result.stdout


def test_start_status_stop_round_trip(cli):
    port = mock.server.reserve_tcp_port()
    chip = chips.SUPPORTED[0]

    start = cli('start', '--port', str(port), '--chip', chip)
    assert start.returncode == 0, start.stderr

    status = cli('status', '--port', str(port), '--json')
    assert status.returncode == 0, status.stderr
    info = json.loads(status.stdout)
    assert info['running'] is True
    assert info['chip'] == chip
    assert info['url'] == f'socket://127.0.0.1:{port}'

    url = cli('url', '--port', str(port))
    assert url.returncode == 0
    assert url.stdout.strip() == f'socket://127.0.0.1:{port}'

    only_port = cli('port', '--port', str(port))
    assert only_port.returncode == 0
    assert only_port.stdout.strip() == str(port)

    stop = cli('stop', '--port', str(port))
    assert stop.returncode == 0

    after = cli('status', '--port', str(port))
    assert after.returncode == 1
    assert daemon.read_state(port) is None


def test_cli_erase_flash(cli):
    import hashlib
    import struct

    from esp32_mock_bootloader import protocol

    port = mock.server.reserve_tcp_port()
    offset = mock.constants.FLASH_APP_OFFSET
    length = 0x200
    try:
        _start_via_cli(cli, port, 'esp32')

        sock = mock.server.connect(port)
        mock.protocol.send_sync(sock)
        mock.protocol.send_and_receive(
            sock,
            mock.protocol.make_command(
                protocol.CMD_FLASH_BEGIN,
                struct.pack('<IIII', length, 1, length, offset),
            ),
        )
        block = b'\x5A' * length
        mock.protocol.send_and_receive(
            sock,
            mock.protocol.make_command(
                protocol.CMD_FLASH_DATA,
                struct.pack('<IIII', length, 0, 0, 0) + block,
            ),
        )
        sock.close()

        erase = cli('erase-flash', '--port', str(port))
        assert erase.returncode == 0, erase.stderr
        assert 'Erased mock flash' in erase.stdout

        sock = mock.server.connect(port)
        mock.protocol.activate_stub(sock)
        data, digest = mock.protocol.stub_read_flash(sock, offset, length)
        sock.close()
        assert data == b'\xff' * length
        assert digest == hashlib.md5(data).digest()
    finally:
        daemon.stop_daemon(port)


def test_cli_erase_flash_port_all(cli, wait_for_daemons):
    port_a = mock.server.reserve_tcp_port()
    port_b = mock.server.reserve_tcp_port()
    try:
        _start_via_cli(cli, port_a, 'esp32')
        _start_via_cli(cli, port_b, 'esp32')
        wait_for_daemons(2)

        erase = cli('erase-flash', '--port', 'all')
        assert erase.returncode == 0, erase.stderr
        assert str(port_a) in erase.stdout and str(port_b) in erase.stdout
    finally:
        daemon.stop_daemon(port_a)
        daemon.stop_daemon(port_b)


def test_cli_erase_flash_requires_running_daemon(cli):
    result = cli('erase-flash')
    assert result.returncode == 1
    assert 'no mock bootloader running' in result.stderr


def test_cli_erase_flash_all_none_running(cli):
    result = cli('erase-flash', '--port', 'all')
    assert result.returncode == 1
    assert 'no mock bootloader daemon is running' in result.stderr


def test_cli_stop_none_running_is_noop(cli):
    assert cli('stop', '--port', 'all').returncode == 0
    assert cli('stop').returncode == 0


def test_erase_flash_all_erases_multiple_daemons():
    port_a = mock.server.reserve_tcp_port()
    port_b = mock.server.reserve_tcp_port()
    try:
        daemon.start_daemon(port=port_a, chip_mode='auto')
        daemon.start_daemon(port=port_b, chip_mode='auto')
        erased = daemon.erase_flash()
        assert sorted(erased) == sorted([port_a, port_b])
    finally:
        daemon.stop_daemon(port_a)
        daemon.stop_daemon(port_b)


def test_erase_flash_single_port_with_multiple_running():
    import struct

    from esp32_mock_bootloader import protocol

    port_a = mock.server.reserve_tcp_port()
    port_b = mock.server.reserve_tcp_port()
    offset = mock.constants.FLASH_APP_OFFSET
    length = 0x100
    try:
        daemon.start_daemon(port=port_a, chip_mode='esp32')
        daemon.start_daemon(port=port_b, chip_mode='esp32')

        sock_b = mock.server.connect(port_b)
        mock.protocol.send_sync(sock_b)
        mock.protocol.send_and_receive(
            sock_b,
            mock.protocol.make_command(
                protocol.CMD_FLASH_BEGIN,
                struct.pack('<IIII', length, 1, length, offset),
            ),
        )
        mock.protocol.send_and_receive(
            sock_b,
            mock.protocol.make_command(
                protocol.CMD_FLASH_DATA,
                struct.pack('<IIII', length, 0, 0, 0) + (b'\x5A' * length),
            ),
        )
        sock_b.close()

        assert daemon.erase_flash(port=port_a) == [port_a]

        sock_b = mock.server.connect(port_b)
        mock.protocol.activate_stub(sock_b)
        data, _digest = mock.protocol.stub_read_flash(sock_b, offset, length)
        sock_b.close()
        assert data == b'\x5A' * length
    finally:
        daemon.stop_daemon(port_a)
        daemon.stop_daemon(port_b)


def test_cli_run_serial_port_flags():
    from esp32_mock_bootloader.cli import build_parser

    parser = build_parser()
    args = parser.parse_args([
        'run', '--pty', '--port', 'COM19', '--serial-bind', 'COM18', '--chip', 'esp32',
    ])
    assert args.port == 'COM19'
    assert args.serial_bind == 'COM18'
    assert args.pty is True


def test_start_refuses_double_start(cli):
    port = mock.server.reserve_tcp_port()
    try:
        first = cli('start', '--port', str(port), '--chip', 'auto')
        assert first.returncode == 0
        second = cli('start', '--port', str(port), '--chip', 'auto')
        assert second.returncode != 0
        assert 'already running' in second.stderr
    finally:
        daemon.stop_daemon(port)


def test_registry_tracks_single_file(cli, registry_root):
    port = mock.server.reserve_tcp_port()
    try:
        cli('start', '--port', str(port), '--chip', 'auto')
        registry = daemon.load_registry()
        assert str(port) in registry['instances']
        assert len(registry['instances']) == 1
        assert registry_root.joinpath('registry.json').is_file()
        assert any(registry_root.glob('port-*.log'))
    finally:
        daemon.stop_daemon(port)

def _running(port: int, **extra: object) -> dict:
    return {
        'running': True,
        'pid': 4242,
        'port': port,
        'chip': 'esp32',
        'detected_chip': None,
        'url': f'socket://127.0.0.1:{port}',
        'log_file': None,
        'mode': 'daemon',
        **extra,
    }


def test_parse_target_port_values():
    assert cli._parse_target_port('all') == 'all'
    assert cli._parse_target_port('9876') == 9876
    assert cli._parse_target_port('65535') == 65535


def test_parse_target_port_invalid():
    with pytest.raises(argparse.ArgumentTypeError, match='invalid port'):
        cli._parse_target_port('abc')
    with pytest.raises(argparse.ArgumentTypeError, match='out of range'):
        cli._parse_target_port('0')
    with pytest.raises(argparse.ArgumentTypeError, match='out of range'):
        cli._parse_target_port('70000')


def test_resolve_query_targets_explicit_port():
    mode, target = cli._resolve_query_targets(43210)
    assert mode == 'single'
    assert target == 43210


def test_resolve_query_targets_all_forces_multi(monkeypatch):
    monkeypatch.setattr(
        daemon,
        'list_running_daemons',
        lambda base=None: [_running(1111)],
    )
    mode, target = cli._resolve_query_targets('all')
    assert mode == 'multi'
    assert len(target) == 1


def test_resolve_query_targets_auto_single(monkeypatch):
    monkeypatch.setattr(
        daemon,
        'list_running_daemons',
        lambda base=None: [_running(2222)],
    )
    mode, target = cli._resolve_query_targets(None)
    assert mode == 'single'
    assert target == 2222


def test_resolve_query_targets_auto_multiple(monkeypatch):
    monkeypatch.setattr(
        daemon,
        'list_running_daemons',
        lambda base=None: [_running(3333), _running(4444)],
    )
    mode, target = cli._resolve_query_targets(None)
    assert mode == 'multi'
    assert {i['port'] for i in target} == {3333, 4444}


def test_resolve_query_targets_auto_none_running(monkeypatch):
    monkeypatch.setattr(daemon, 'list_running_daemons', lambda base=None: [])
    mode, target = cli._resolve_query_targets(None)
    assert mode == 'single'
    assert target == daemon.DEFAULT_PORT


def test_format_status_table():
    table = cli._format_status_table([_running(5555, detected_chip='esp32c3')])
    assert '5555' in table
    assert 'esp32c3' in table
    assert 'socket://127.0.0.1:5555' in table


def test_cmd_status_single_running(capsys, monkeypatch):
    monkeypatch.setattr(
        daemon,
        'daemon_status',
        lambda port, base=None: _running(port),
    )
    monkeypatch.setattr(cli, '_resolve_query_targets', lambda _p: ('single', 6001))

    rc = cli._cmd_status(argparse.Namespace(port=6001, json=False, bind='127.0.0.1'))
    out = capsys.readouterr().out
    assert rc == 0
    assert 'status: running' in out
    assert 'port: 6001' in out


def test_cmd_status_single_stopped(capsys, monkeypatch):
    monkeypatch.setattr(
        daemon,
        'daemon_status',
        lambda port, base=None: {'running': False, 'port': port},
    )
    monkeypatch.setattr(cli, '_resolve_query_targets', lambda _p: ('single', 6002))

    rc = cli._cmd_status(argparse.Namespace(port=6002, json=False, bind='127.0.0.1'))
    out = capsys.readouterr().out
    assert rc == 1
    assert 'status: stopped' in out


def test_cmd_status_multi_json_empty(capsys, monkeypatch):
    monkeypatch.setattr(cli, '_resolve_query_targets', lambda _p: ('multi', []))

    rc = cli._cmd_status(argparse.Namespace(port='all', json=True, bind='127.0.0.1'))
    out = capsys.readouterr()
    assert rc == 1
    assert json.loads(out.out) == {'instances': []}


def test_cmd_status_multi_json(capsys, monkeypatch):
    monkeypatch.setattr(
        cli,
        '_resolve_query_targets',
        lambda _p: ('multi', [_running(7001), _running(7002)]),
    )

    rc = cli._cmd_status(argparse.Namespace(port='all', json=True, bind='127.0.0.1'))
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert len(payload['instances']) == 2


def test_cmd_status_multi_human(capsys, monkeypatch):
    monkeypatch.setattr(
        cli,
        '_resolve_query_targets',
        lambda _p: ('multi', [_running(7001), _running(7002)]),
    )

    rc = cli._cmd_status(argparse.Namespace(port='all', json=False, bind='127.0.0.1'))
    out = capsys.readouterr().out
    assert rc == 0
    assert '7001' in out and '7002' in out


def test_cmd_url_single_fallback(capsys, monkeypatch):
    monkeypatch.setattr(cli, '_resolve_query_targets', lambda _p: ('single', 8001))
    monkeypatch.setattr(daemon, 'read_state', lambda port, base=None: None)

    rc = cli._cmd_url(argparse.Namespace(port=8001, bind='127.0.0.1'))
    assert rc == 0
    assert capsys.readouterr().out.strip() == 'socket://127.0.0.1:8001'


def test_cmd_url_multi_empty(capsys, monkeypatch):
    monkeypatch.setattr(cli, '_resolve_query_targets', lambda _p: ('multi', []))

    rc = cli._cmd_url(argparse.Namespace(port='all', bind='127.0.0.1'))
    err = capsys.readouterr().err
    assert rc == 1
    assert 'no running mock bootloader instances' in err


def test_cmd_port_single_from_state(capsys, monkeypatch):
    monkeypatch.setattr(cli, '_resolve_query_targets', lambda _p: ('single', 9001))
    monkeypatch.setattr(
        daemon,
        'read_state',
        lambda port, base=None: {'port': port, 'pid': 1},
    )

    rc = cli._cmd_port(argparse.Namespace(port=9001))
    assert rc == 0
    assert capsys.readouterr().out.strip() == '9001'


def test_cmd_stop_single(capsys, monkeypatch):
    stopped: list[int] = []
    monkeypatch.setattr(cli, '_resolve_query_targets', lambda _p: ('single', 9100))
    monkeypatch.setattr(daemon, 'stop_daemon', lambda port, base=None: stopped.append(port) or True)

    assert cli._cmd_stop(argparse.Namespace(port=9100)) == 0
    assert stopped == [9100]


def test_cmd_stop_multi_empty(capsys, monkeypatch):
    monkeypatch.setattr(cli, '_resolve_query_targets', lambda _p: ('multi', []))
    assert cli._cmd_stop(argparse.Namespace(port='all')) == 0


def test_cmd_stop_multi(capsys, monkeypatch):
    stopped: list[int] = []
    monkeypatch.setattr(
        cli,
        '_resolve_query_targets',
        lambda _p: ('multi', [_running(9201), _running(9202)]),
    )
    monkeypatch.setattr(
        daemon,
        'stop_daemon',
        lambda port, base=None: stopped.append(port) or True,
    )

    assert cli._cmd_stop(argparse.Namespace(port='all')) == 0
    assert sorted(stopped) == [9201, 9202]


def test_cmd_erase_flash_single(capsys, monkeypatch):
    monkeypatch.setattr(cli, '_resolve_query_targets', lambda _p: ('single', 9300))
    monkeypatch.setattr(daemon, 'erase_flash', lambda port='all', base=None: [9300])

    rc = cli._cmd_erase_flash(argparse.Namespace(port=9300))
    assert rc == 0
    assert 'Erased mock flash (port 9300)' in capsys.readouterr().out


def test_cmd_erase_flash_multi(capsys, monkeypatch):
    monkeypatch.setattr(cli, '_resolve_query_targets', lambda _p: ('multi', [_running(9401)]))
    monkeypatch.setattr(daemon, 'erase_flash', lambda port='all', base=None: [9401, 9402])

    rc = cli._cmd_erase_flash(argparse.Namespace(port='all'))
    out = capsys.readouterr().out
    assert rc == 0
    assert 'ports 9401, 9402' in out


def test_cmd_erase_flash_error(capsys, monkeypatch):
    monkeypatch.setattr(cli, '_resolve_query_targets', lambda _p: ('single', 9500))

    def _boom(**_kwargs):
        raise RuntimeError('erase failed')

    monkeypatch.setattr(daemon, 'erase_flash', _boom)

    rc = cli._cmd_erase_flash(argparse.Namespace(port=9500))
    assert rc == 1
    assert 'erase failed' in capsys.readouterr().err


def test_cmd_start_failure(capsys, monkeypatch):
    monkeypatch.setattr(
        daemon,
        'start_daemon',
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError('boom')),
    )
    rc = cli._cmd_start(argparse.Namespace(
        port=9600, chip='auto', startup_timeout=1.0, bind='127.0.0.1', force=False,
    ))
    assert rc == 1
    assert 'boom' in capsys.readouterr().err


def test_cmd_run_tcp(monkeypatch):
    called: dict[str, object] = {}

    def fake_run_server(*args, **kwargs):
        called['args'] = args
        called['kwargs'] = kwargs

    monkeypatch.setattr(cli, 'run_server', fake_run_server)
    rc = cli._cmd_run(argparse.Namespace(
        pty=False,
        port='9876',
        timeout=None,
        chip='esp32',
        bind='127.0.0.1',
        exit_on_disconnect=False,
        daemon_child=False,
        pty_path_file=None,
        serial_bind=None,
    ))
    assert rc == 0
    assert called['args'][0] == 9876


def test_main_invokes_subcommand(monkeypatch):
    monkeypatch.setattr(
        cli,
        'build_parser',
        lambda: type('P', (), {
            'parse_args': lambda self, argv: argparse.Namespace(func=lambda _a: 7),
        })(),
    )
    assert cli.main([]) == 7


def test_print_single_status_json(capsys):
    cli._print_single_status(_running(9700), json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload['port'] == 9700


def test_print_single_status_log_file(capsys):
    cli._print_single_status(
        _running(9701, log_file='/tmp/port-9701.log'),
        json_output=False,
    )
    assert 'log_file: /tmp/port-9701.log' in capsys.readouterr().out


def test_print_single_url_from_state(capsys, monkeypatch):
    monkeypatch.setattr(
        daemon,
        'read_state',
        lambda port, base=None: {'url': f'socket://127.0.0.1:{port}'},
    )
    cli._print_single_url(9800, '127.0.0.1')
    assert capsys.readouterr().out.strip() == 'socket://127.0.0.1:9800'


def test_build_parser_target_port_subcommands():
    parser = cli.build_parser()
    for command in ('status', 'url', 'port', 'stop', 'erase-flash'):
        args = parser.parse_args([command, '--port', 'all'])
        assert args.port == 'all'


def test_cli_run_pty_smoke(tmp_path):
    import subprocess
    import sys
    import time

    path_file = tmp_path / 'pty.path'
    proc = subprocess.Popen(
        [
            sys.executable, '-m', 'esp32_mock_bootloader.cli', 'run',
            '--pty', '--pty-path-file', str(path_file),
            '--chip', 'esp32',
            '--timeout', '30',
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            if path_file.is_file() and path_file.read_text(encoding='ascii').strip():
                break
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        assert path_file.is_file()
        assert path_file.read_text(encoding='ascii').strip()
    finally:
        if proc.poll() is None:
            mock.server.stop_subprocess(proc)


def test_cli_start_force(cli):
    port = mock.server.reserve_tcp_port()
    try:
        assert cli('start', '--port', str(port), '--chip', 'auto').returncode == 0
        replaced = cli('start', '--port', str(port), '--chip', 'esp32', '--force')
        assert replaced.returncode == 0, replaced.stderr
        status = json.loads(cli('status', '--port', str(port), '--json').stdout)
        assert status['chip'] == 'esp32'
    finally:
        daemon.stop_daemon(port)
