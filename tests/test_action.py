# SPDX-FileCopyrightText: 2026 Lucas Saavedra Vaz
# SPDX-License-Identifier: Apache-2.0

"""GitHub Action helper smoke tests."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ACTION_DIR = Path(__file__).resolve().parents[1] / 'action'


@pytest.mark.skipif(shutil.which('node') is None, reason='node not installed')
def test_action_lib_js():
    result = subprocess.run(
        ['node', str(ACTION_DIR / 'test_lib.js')],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=ACTION_DIR,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert 'OK' in result.stdout
