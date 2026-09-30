"""在项目 build 内验收真实安装器；拒绝覆盖本机已有安装或个人数据。"""
from __future__ import annotations

import argparse
import csv
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
APP_ID = '{D9739D28-31DA-4078-A55F-7EF09D443038}_is1'


def run(command, **kwargs):
    return subprocess.run([str(item) for item in command], check=True, timeout=1200, **kwargs)


def installed():
    import winreg
    key = 'Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\' + APP_ID
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view):
                    return True
            except FileNotFoundError:
                pass
    return False


def native_window(pid):
    """只枚举本次启动的进程，避免关闭用户的其他窗口。"""
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    found = []

    @callback_type
    def visit(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            title, kind = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, title, 256)
            user32.GetClassNameW(hwnd, kind, 256)
            if title.value == '本地模型检索' and kind.value.startswith('WindowsForms10.'):
                found.append(hwnd)
        return True

    user32.EnumWindows(visit, 0)
    return found[0] if found else None


def seed_library(session, data):
    import trimesh
    source = data.parent / '模型 fixture'
    source.mkdir()
    trimesh.creation.box().export(source / '盒体.stl')
    ready = json.loads((session / 'ready.json').read_text(encoding='utf-8'))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def api(path, body=None):
        headers = {'X-Search-Token': ready['token'], 'Content-Type': 'application/json'}
        request = urllib.request.Request(ready['origin'] + path, headers=headers,
                                         data=json.dumps(body).encode() if body is not None else None)
        with opener.open(request, timeout=60) as response:
            return json.load(response)

    grant = uuid.uuid4().hex
    (session / ('grant-' + grant + '.json')).write_text(
        json.dumps({'kind': 'directory', 'path': str(source)}), encoding='utf-8')
    job = api('/api/libraries', {'name': '升级保留验收', 'grant': grant})
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        state = api('/api/tasks/' + job['id'])
        if state['status'] == 'done':
            if state['result']['library']['count'] != 1 or state['result']['failures']:
                raise RuntimeError('升级验收样例库未正确建立')
            return
        if state['status'] in ('error', 'cancelled'):
            raise RuntimeError('样例库任务失败：' + str(state.get('message')))
        time.sleep(.3)
    raise TimeoutError('样例库任务超时')


def launch(exe, data, env, gui=False, seed=False):
    session_root = data / 'sessions'
    previous = set(session_root.iterdir()) if session_root.exists() else set()
    process = subprocess.Popen([str(exe)] + ([] if gui else ['--no-browser']), cwd=data, env=env)
    session = None
    try:
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError('安装版提前退出，请查看测试目录 startup.log')
            for candidate in session_root.iterdir() if session_root.exists() else []:
                if candidate not in previous and (candidate / 'ready.json').is_file():
                    session = candidate
            if session and (not gui or native_window(process.pid)):
                break
            time.sleep(.25)
        else:
            raise TimeoutError('安装版未就绪或未创建 Windows 原生独立窗口')
        if seed:
            seed_library(session, data)
        if gui:
            time.sleep(3)
            # WM_CLOSE 只发给本次进程的主窗，验证正常关窗时的服务清理。
            user32 = ctypes.WinDLL('user32', use_last_error=True)
            user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            hwnd = native_window(process.pid)
            if not hwnd or not user32.PostMessageW(hwnd, 0x0010, 0, 0):
                raise RuntimeError('未能正常关闭本次验收的独立窗口')
        else:
            (session / 'stop').write_text('', encoding='utf-8')
        if process.wait(timeout=45) != 0:
            raise RuntimeError('安装版退出码非零')
    finally:
        if session and process.poll() is None:
            (session / 'stop').write_text('', encoding='utf-8')
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
    if session and (session / 'queries').exists():
        raise RuntimeError('正常退出后仍存在查询缓存')


def main():
    parser = argparse.ArgumentParser(description='安装、只读目录、原生窗口、覆盖安装和卸载验收')
    parser.add_argument('--installer', type=Path, required=True)
    parser.add_argument('--gui', action='store_true', help='在有交互桌面的本机短暂打开并关闭独立窗口')
    args = parser.parse_args()
    if sys.platform != 'win32' or not args.installer.is_file():
        parser.error('需要 Windows 和现有 Setup.exe')
    if installed():
        parser.error('本机已有 Local Model Search 安装，拒绝覆盖；请在干净测试机验收')
    work = ROOT / 'build' / ('安装验收 ' + uuid.uuid4().hex[:8])
    target, data = work / '应用 只读', work / '用户 数据'
    data.mkdir(parents=True)
    env = dict(os.environ, LOCAL_MODEL_SEARCH_DATA_DIR=str(data))
    env.pop('PYTHONHOME', None)
    env.pop('PYTHONPATH', None)
    env['PATH'] = os.path.join(os.environ['SystemRoot'], 'System32') + os.pathsep + os.environ['SystemRoot']
    result = {'ok': False, 'checks': [], 'limitations': ['未验证 SmartScreen 下载提示和原生文件选择器', '覆盖安装使用同一版本安装器']}
    installer = args.installer.resolve()
    install_args = ['/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', '/NOICONS', '/TASKS=', '/DIR=' + str(target)]
    uninstaller = target / 'unins000.exe'
    try:
        run([installer, *install_args, '/LOG=' + str(work / 'install.log')])
        exe = target / 'LocalModelSearch.exe'
        if not exe.is_file() or not uninstaller.is_file() or not installed():
            raise RuntimeError('安装目录、卸载入口或当前用户安装登记缺失')
        result['checks'].append('当前用户静默安装及中文空格安装目录')
        launch(exe, data, env, seed=True)
        config = data / 'config.json'
        settings = json.loads(config.read_text(encoding='utf-8'))
        settings['acceptance_marker'] = uuid.uuid4().hex
        config.write_text(json.dumps(settings), encoding='utf-8')
        registry = data / 'registry.json'
        if not registry.is_file():
            raise RuntimeError('首次运行未创建检索库注册表')
        saved = {path: path.read_bytes() for path in (config, registry)}
        saved.update({path: path.read_bytes() for path in (data / 'libraries').rglob('*') if path.is_file()})
        # ACL 仅限本次新建的程序目录；无论测试成败都恢复本次拒绝规则。
        identity = run(['whoami', '/user', '/fo', 'csv', '/nh'], capture_output=True, text=True)
        sid = next(csv.reader(identity.stdout.splitlines()))[1]
        acl = os.path.join(os.environ['SystemRoot'], 'System32', 'icacls.exe')
        try:
            run([acl, target, '/deny', '*' + sid + ':(OI)(CI)(W)'], capture_output=True)
            try:
                (target / 'must-not-write.tmp').write_bytes(b'test')
            except PermissionError:
                pass
            else:
                raise RuntimeError('测试目录并未真正禁止写入')
            report = work / 'installed-smoke.json'
            run([exe, '--self-test', report], env=env, cwd=data)
            if not json.loads(report.read_text(encoding='utf-8')).get('ok'):
                raise RuntimeError('安装产物离线推理验收失败')
            launch(exe, data, env, gui=args.gui)
        finally:
            run([acl, target, '/remove:d', '*' + sid], capture_output=True)
        result['checks'].append('程序目录 ACL 禁写时三格式建库、真实推理及退出')
        if args.gui:
            result['checks'].append('Windows 原生独立窗口创建及正常关窗退出')
        else:
            result['limitations'].append('未执行原生独立窗口验收')
        run([installer, *install_args, '/LOG=' + str(work / 'reinstall.log')])
        if any(path.read_bytes() != content for path, content in saved.items()):
            raise RuntimeError('覆盖安装改动了用户配置或索引文件')
        result['checks'].append('同版本覆盖安装保留用户配置和真实索引文件')
        run([uninstaller, '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/LOG=' + str(work / 'uninstall.log')])
        if exe.exists() or installed():
            raise RuntimeError('卸载后仍存在主程序或安装登记')
        if any(path.read_bytes() != content for path, content in saved.items()):
            raise RuntimeError('卸载改动了用户配置或索引文件')
        result['checks'].append('卸载移除程序并保留用户数据')
        result['ok'] = True
    except Exception:
        result['error'] = traceback.format_exc()
        raise
    finally:
        output = work / 'installation-report.json'
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print('验收记录：' + str(output), flush=True)
        # 失败时保留测试安装用于排查；不接触任何已有安装。
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
