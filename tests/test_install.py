import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows installer')
def test_installed_shim_runs_outside_source_tree(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    dest = tmp_path / '安装 路径'
    env = {**os.environ, 'AGY_POOL_HOME': str(tmp_path / 'isolated-home')}
    env.pop('AGY_POOL_DB', None)
    installed = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive',
                                '-File', str(repo / 'install.ps1'), '-InstallDir', str(dest), '-NoPath'],
                               cwd=tmp_path, env=env, capture_output=True, timeout=25)
    assert installed.returncode == 0, installed.stderr
    listed = subprocess.run([str(dest / 'agy-pool.cmd'), 'list', '--json'], cwd=tmp_path,
                            env=env, capture_output=True, timeout=15)
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout) == {'active_machine_account': None, 'accounts': [], 'threads': []}
