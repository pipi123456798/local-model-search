"""一键准备运行环境：虚拟环境、Python 依赖、Shape 权重包，完成后启动本地检索。

由 Start-Windows.bat / Start-Mac.command 自动调用，也可手动运行：
    python scripts/setup.py [--reuse-system] [--skip-launch] [--reinstall]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / '.venv'
PROBE = (
    'import importlib.util as u, sys\n'
    'names = ("flask", "numpy", "scipy", "trimesh", "torch", "einops", "lxml", "PIL")\n'
    'ok = all(u.find_spec(n) is not None for n in names)\n'
    'ok = ok and (u.find_spec("OCP") is not None or u.find_spec("OCC") is not None)\n'
    'sys.exit(0 if ok else 1)'
)


def venv_python():
    return VENV / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def run(command):
    print('>', ' '.join(str(part) for part in command), flush=True)
    subprocess.run([str(part) for part in command], check=True)


def config_value(name, default=None):
    path = ROOT / 'config.json'
    if path.is_file():
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except ValueError:
            return default
        if isinstance(value, dict) and value.get(name):
            return value[name]
    return default


def ensure_venv(reuse_system):
    python = venv_python()
    if python.is_file():
        return python
    print('创建虚拟环境（%s）…' % VENV, flush=True)
    command = [sys.executable, '-m', 'venv']
    if reuse_system:
        # 复用系统 Python 已安装的依赖，可加快开发机上的首次安装。
        command.append('--system-site-packages')
    command.append(str(VENV))
    run(command)
    return python


def dependencies_ready(python):
    return subprocess.run([str(python), '-c', PROBE], capture_output=True).returncode == 0


def setup_environment(reuse_system, reinstall):
    python = ensure_venv(reuse_system)
    stamp = VENV / '.requirements-stamp'
    digest = hashlib.sha256((ROOT / 'requirements.txt').read_bytes()).hexdigest()
    if not reinstall and stamp.is_file() and stamp.read_text(encoding='utf-8').strip() == digest:
        if dependencies_ready(python):
            print('Python 依赖已就绪。', flush=True)
            return python
    print('安装 Python 依赖（首次需要下载约 1–2 GB，请耐心等待）…', flush=True)
    index = config_value('pip_index')
    extra = ['--index-url', str(index)] if index else []
    run([python, '-m', 'pip', 'install', '--upgrade', 'pip', '--disable-pip-version-check'] + extra)
    run([python, '-m', 'pip', 'install', '--disable-pip-version-check', '-r', str(ROOT / 'requirements.txt')] + extra)
    if not dependencies_ready(python):
        raise SystemExit('依赖安装后校验失败，请重新运行本脚本或查看上方 pip 输出。')
    stamp.write_text(digest, encoding='utf-8')
    print('Python 依赖安装完成。', flush=True)
    return python


def setup_kit(python):
    fetch = ROOT / 'scripts' / 'fetch_kit.py'
    code = subprocess.call([str(python), str(fetch)])
    if code != 0:
        raise SystemExit('权重包准备失败，请检查网络后重新运行本脚本。')


def main():
    if not (3, 11) <= sys.version_info < (3, 14):
        raise SystemExit('需要 Python 3.11 – 3.13（当前 %d.%d）。请安装后重新运行。' % sys.version_info[:2])
    parser = argparse.ArgumentParser(description='本地模型检索（独立版）一键安装')
    parser.add_argument('--reuse-system', action='store_true',
                        help='创建虚拟环境时复用系统已安装的依赖（开发机加速用）')
    parser.add_argument('--reinstall', action='store_true', help='强制重新安装 Python 依赖')
    parser.add_argument('--skip-launch', action='store_true', help='只准备环境，不启动')
    args = parser.parse_args()

    python = setup_environment(args.reuse_system, args.reinstall)
    setup_kit(python)
    if args.skip_launch:
        print('环境已就绪。双击 Start-Windows.bat / Start-Mac.command 即可使用。', flush=True)
        return 0
    print('启动本地模型检索…', flush=True)
    return subprocess.call([str(python), str(ROOT / 'app' / 'launcher.py')])


if __name__ == '__main__':
    sys.exit(main())
