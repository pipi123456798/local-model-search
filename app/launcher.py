"""本地模型检索（独立版）启动器：准备会话、拉起本地服务并打开应用界面。

界面优先使用系统 WebView 的独立应用窗口（config.json 的 window 键可改为 browser）；
用法：
    python app/launcher.py [--no-browser]

正常情况下由 scripts/setup.py 一键准备环境后自动调用；
也可在已配置好的环境中直接运行。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
import traceback

from runtime import resource_root, user_data_dir, initialize_config

ROOT = resource_root()
SERVICE = ROOT / 'app' / 'model_search'
DEFAULTS = {'port': 0, 'device': 'auto', 'kit': '', 'checkpoint': '', 'open_browser': True,
            'window': 'auto', 'cad_exe': ''}


def load_config():
    return initialize_config(DEFAULTS)


def resolve_paths(config):
    kit = str(config.get('kit') or '').strip()
    kit_path = Path(kit).expanduser() if kit else Path('kit')
    if not kit_path.is_absolute():
        kit_path = ROOT / kit_path
    kit_path = kit_path.resolve()
    checkpoint = str(config.get('checkpoint') or '').strip()
    checkpoint_path = Path(checkpoint).expanduser() if checkpoint else kit_path / 'checkpoints/checkpoint_final.pt'
    if not checkpoint_path.is_absolute():
        checkpoint_path = ROOT / checkpoint_path
    return kit_path, checkpoint_path.resolve()


def prepare_session(config):
    data_dir = user_data_dir()
    session_dir = data_dir / 'sessions' / uuid.uuid4().hex
    session_dir.mkdir(parents=True, exist_ok=True)
    kit_path, checkpoint_path = resolve_paths(config)
    settings = {'python': sys.executable, 'kit': str(kit_path), 'checkpoint': str(checkpoint_path),
                'device': str(config.get('device') or 'auto'),
                'cad_exe': str(config.get('cad_exe') or '')}
    (session_dir / 'settings.json').write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding='utf-8')
    return data_dir, session_dir, kit_path, checkpoint_path


def start_service(data_dir, session_dir, config):
    entry = [sys.executable, '--service'] if getattr(sys, 'frozen', False) else [
        sys.executable, str(Path(__file__).resolve()), '--service']
    command = entry + ['--data-dir', str(data_dir), '--session-dir', str(session_dir),
               '--settings', str(session_dir / 'settings.json'),
               '--parent-pid', str(os.getpid()),
               '--port', str(int(config.get('port') or 0))]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    # 目录式冻结应用由同一可执行文件启动服务，依赖路径仍由 bootloader 管理。
    env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    return subprocess.Popen(command, cwd=str(data_dir), env=env,
                            stdin=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                            start_new_session=True)


def wait_ready(process, session_dir, on_wait=None):
    ready = session_dir / 'ready.json'
    deadline = time.monotonic() + 180
    print('正在启动本地检索服务（首次加载需要一些时间）…', flush=True)
    while time.monotonic() < deadline:
        if on_wait:
            on_wait()
        if ready.is_file():
            try:
                info = json.loads(ready.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                info = None
            if isinstance(info, dict) and info.get('origin') and info.get('token'):
                return info
        if process.poll() is not None:
            detail = ''
            error = session_dir / 'error.json'
            if error.is_file():
                try:
                    detail = str(json.loads(error.read_text(encoding='utf-8')).get('error', ''))
                except (OSError, ValueError):
                    detail = ''
            raise SystemExit('本地服务启动失败：%s\n日志：%s'
                             % (detail or '未知错误', session_dir / 'launcher.log'))
        time.sleep(0.2)
    raise SystemExit('本地服务启动超时，请查看日志：%s' % (session_dir / 'launcher.log'))


def run_window(url, process):
    """以系统 WebView 打开独立应用窗口；不可用时返回 False，由调用方回退浏览器。

    返回 True 表示窗口已被用户关闭（或本地服务退出），调用方应结束应用。
    """
    try:
        import webview
    except Exception:
        print('未安装 pywebview，改用浏览器打开。', flush=True)
        return False
    try:
        window = webview.create_window('本地模型检索', url, width=1440, height=920, min_size=(1080, 700))
    except Exception as exc:
        print('独立窗口不可用（%s），改用浏览器打开。' % exc, flush=True)
        return False

    def watch_service():
        process.wait()
        try:
            window.destroy()
        except Exception:
            pass

    threading.Thread(target=watch_service, daemon=True).start()
    print('已打开独立应用窗口；关闭窗口即退出程序。', flush=True)
    # pywebview 6.x 通过 start(icon=…) 设置窗口图标；透明图标用于去掉默认的 Python 图标。
    icon = ROOT / 'app' / 'assets' / 'window.ico'
    start_args = {'icon': str(icon)} if icon.is_file() else {}
    try:
        webview.start(**start_args)
    except Exception as exc:
        print('独立窗口启动失败（%s），改用浏览器打开。' % exc, flush=True)
        return False
    return True


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            try:
                stream.reconfigure(encoding='utf-8', errors='replace')
            except (OSError, ValueError):
                pass
    parser = argparse.ArgumentParser(description='本地模型检索（独立版）')
    parser.add_argument('--no-browser', action='store_true', help='启动后不自动打开浏览器')
    parser.add_argument('--self-test', metavar='OUTPUT', help='离线产物验收，写入 JSON 报告后退出')
    parser.add_argument('--open-data', action='store_true', help='打开配置与日志目录')
    args = parser.parse_args()
    if args.self_test:
        from smoke_test import run_self_test
        return run_self_test(Path(args.self_test))
    if args.open_data:
        from runtime import open_folder
        user_data_dir().mkdir(parents=True, exist_ok=True)
        open_folder(user_data_dir())
        return 0

    config = load_config()
    data_dir, session_dir, kit_path, checkpoint_path = prepare_session(config)
    if not (kit_path / 'shape_foundation/models/gaot_backbone.py').is_file():
        print('警告：未找到 Shape 工具包（%s）；\n请先双击启动脚本自动下载权重包，或运行 scripts/fetch_kit.py。'
              % kit_path)
    if not checkpoint_path.is_file():
        print('警告：未找到权重文件（%s）。' % checkpoint_path)

    progress = None
    if getattr(sys, 'frozen', False) and not args.no_browser:
        try:
            from launcher_ui import StartupWindow
            progress = StartupWindow()
        except Exception:
            traceback.print_exc()
    process = None
    try:
        process = start_service(data_dir, session_dir, config)
        info = wait_ready(process, session_dir, progress.tick if progress else None)
        if progress:
            progress.close()
            progress = None
        url = info['origin'] + '/#token=' + info['token']
        print('本地模型检索已就绪：' + info['origin'], flush=True)
        if not args.no_browser and config.get('open_browser', True):
            mode = str(config.get('window') or 'auto').strip().lower()
            if mode != 'browser' and run_window(url, process):
                return  # 独立窗口已被关闭：结束应用。
            webbrowser.open(url)
            if getattr(sys, 'frozen', False):
                from launcher_ui import browser_controls
                browser_controls(url, process)
                return 0
            print('已在浏览器中打开界面；保持本窗口打开即可使用，按 Ctrl+C 退出。', flush=True)
        while process.poll() is None:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print('\n正在退出…', flush=True)
    finally:
        if progress:
            progress.close()
        if process is not None:
            (session_dir / 'stop').write_text('', encoding='utf-8')
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def entrypoint():
    # 源码直启需要服务模块目录；冻结应用由 Analysis.pathex 收集同名模块。
    if not getattr(sys, 'frozen', False):
        sys.path.insert(0, str(SERVICE))
    if '--service' in sys.argv:
        sys.argv.remove('--service')
        from bootstrap import main as service_main
        return service_main()
    if not getattr(sys, 'frozen', False):
        return main()
    data = user_data_dir()
    log_path = data / 'startup.log'
    try:
        data.mkdir(parents=True, exist_ok=True)
        # 避免无控制台应用中的 stdout/stderr 为 None，并保留异常详情。
        with log_path.open('a', encoding='utf-8', buffering=1) as log:
            sys.stdout = sys.stderr = log
            try:
                return main()
            except (Exception, SystemExit) as exc:
                if isinstance(exc, SystemExit) and not exc.code:
                    return 0
                traceback.print_exc()
                if '--self-test' not in sys.argv and '--no-browser' not in sys.argv:
                    from launcher_ui import show_error
                    show_error('%s\n\n日志：%s' % (exc, log_path))
                return 1
    except OSError as exc:
        if '--self-test' not in sys.argv and '--no-browser' not in sys.argv:
            from launcher_ui import show_error
            show_error('无法创建用户数据或日志目录：%s' % exc)
        return 1


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    raise SystemExit(entrypoint())
