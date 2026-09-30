"""独立版（standalone）扩展端点：平台原生选择窗口、文件管理器定位与 CAD 打开。

独立版启动器（app/launcher.py）拉起服务时加载本模块；
嵌入 Bambu Studio 桌面程序时由宿主提供等价的原生能力，不走这些端点。
"""
from __future__ import annotations

import base64
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from flask import jsonify, request

from inference import SearchError, display_path, native_path
from storage import atomic_json, read_json

CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
PICK_TIMEOUT = 600  # 等待用户在选择窗口中操作的时间上限（秒）。

# PowerShell 脚本以 -EncodedCommand（UTF-16LE）执行，结果写入 __OUTPUT__ 指定的 UTF-8 文件，
# 避免控制台代码页对中文路径的影响；用户取消时不会写出该文件。
_DIRECTORY_DIALOG = r"""
Add-Type -AssemblyName System.Windows.Forms | Out-Null
[System.Windows.Forms.Application]::EnableVisualStyles()
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = '选择包含 3D 模型的文件夹'
$dialog.ShowNewFolderButton = $false
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    $dialog.SelectedPath | Out-File -FilePath '__OUTPUT__' -Encoding utf8 -NoNewline
}
"""

_FILE_DIALOG = r"""
Add-Type -AssemblyName System.Windows.Forms | Out-Null
[System.Windows.Forms.Application]::EnableVisualStyles()
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = '选择查询模型'
$dialog.Filter = '3D 模型|*.step;*.stp;*.stl;*.obj;*.ply;*.glb;*.gltf;*.3mf;*.off|所有文件|*.*'
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    $dialog.FileName | Out-File -FilePath '__OUTPUT__' -Encoding utf8 -NoNewline
}
"""


def _run_powershell(script, output):
    # Out-File 不接受 \\?\ 扩展路径前缀，写入前转为常规路径（会话目录由 native_path 构造）。
    script = script.replace('__OUTPUT__', display_path(output).replace("'", "''"))
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    try:
        subprocess.run(
            ['powershell.exe', '-NoProfile', '-STA', '-ExecutionPolicy', 'Bypass',
             '-EncodedCommand', encoded],
            stdin=subprocess.DEVNULL, capture_output=True, check=False,
            creationflags=CREATE_NO_WINDOW, timeout=PICK_TIMEOUT)
    except FileNotFoundError as exc:
        raise SearchError('未找到 PowerShell，无法打开系统选择窗口', 'environment', 503) from exc
    except subprocess.TimeoutExpired:
        output.unlink(missing_ok=True)
        return None
    if not output.is_file():
        return None
    try:
        text = output.read_text(encoding='utf-8-sig').strip()
    finally:
        output.unlink(missing_ok=True)
    return Path(text) if text else None


def _run_osascript(command):
    try:
        result = subprocess.run(['osascript', '-e', command], capture_output=True,
                                text=True, encoding='utf-8', timeout=PICK_TIMEOUT)
    except FileNotFoundError as exc:
        raise SearchError('未找到系统选择器（osascript）', 'environment', 503) from exc
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None  # 用户取消或关闭了选择窗口。
    text = (result.stdout or '').strip()
    return Path(text) if text else None


def _pick_dialog(kind, session_dir):
    if os.name == 'nt':
        output = session_dir / ('pick-' + uuid.uuid4().hex + '.txt')
        script = _DIRECTORY_DIALOG if kind == 'directory' else _FILE_DIALOG
        return _run_powershell(script, output)
    if sys.platform == 'darwin':
        if kind == 'directory':
            command = 'POSIX path of (choose folder with prompt "选择包含 3D 模型的文件夹")'
        else:
            command = 'POSIX path of (choose file with prompt "选择查询模型")'
        return _run_osascript(command)
    raise SearchError('当前系统暂不支持原生选择窗口（仅支持 Windows 与 macOS）', 'environment', 503)


def _reveal(path):
    if os.name == 'nt':
        os.startfile(path)
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', path])
    else:
        raise SearchError('当前系统暂不支持打开所在文件夹', 'environment', 503)


def _open_in_cad(path, executable):
    """用 CAD 打开模型：优先 config.json 指定的程序，否则交给系统默认关联程序。"""
    if executable:
        if not Path(executable).is_file():
            raise SearchError('config.json 中配置的 CAD 程序不存在：%s' % executable, 'environment', 503)
        subprocess.Popen([executable, path])
        return
    if os.name == 'nt':
        os.startfile(path)
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', path])
    else:
        raise SearchError('当前系统暂不支持在 CAD 中打开模型', 'environment', 503)


def register_standalone_routes(app, session_dir):
    session_dir = Path(session_dir)

    @app.post('/api/standalone/data-folder')
    def data_folder():
        root = app.extensions['search_store'].root
        _reveal(display_path(root))
        return jsonify(ok=True, path=display_path(root))

    @app.post('/api/standalone/import-legacy')
    def import_legacy():
        if app.extensions['search_tasks'].active():
            raise SearchError('请等待当前任务完成后再导入旧库', 'library_busy', 409)
        selection = _pick_dialog('directory', session_dir)
        if selection is None:
            return jsonify(cancelled=True)
        from runtime import import_legacy_data
        try:
            target = import_legacy_data(selection, app.extensions['search_store'].root)
        except ValueError as exc:
            raise SearchError(str(exc)) from exc
        return jsonify(ok=True, path=str(target))

    @app.post('/api/standalone/pick')
    def standalone_pick():
        body = request.get_json(silent=True)
        kind = body.get('kind') if isinstance(body, dict) else None
        if kind not in ('directory', 'file'):
            raise SearchError('选择类型无效')
        selection = _pick_dialog(kind, session_dir)
        if selection is None:
            return jsonify(cancelled=True)
        selection = selection.resolve()
        if kind == 'directory' and not selection.is_dir():
            raise SearchError('所选文件夹不存在，请重新选择')
        if kind == 'file' and not selection.is_file():
            raise SearchError('所选文件不存在，请重新选择')
        # 写入与桌面宿主一致的授权文件，由 /api/libraries 与 /api/search 消费。
        gid = uuid.uuid4().hex
        atomic_json(session_dir / ('grant-' + gid + '.json'),
                    {'kind': kind, 'path': str(native_path(selection))})
        return jsonify(grant=gid, path=display_path(selection), name=selection.name)

    @app.post('/api/standalone/reveal')
    def standalone_reveal():
        body = request.get_json(silent=True)
        ticket = body.get('ticket') if isinstance(body, dict) else None
        if not isinstance(ticket, str) or len(ticket) != 32 or any(c not in '0123456789abcdef' for c in ticket):
            raise SearchError('无效的定位票据', 'invalid_id')
        record = session_dir / ('reveal-' + ticket + '.json')
        entry = read_json(record, None)
        if not entry or time.time() - record.stat().st_mtime > 300:
            raise SearchError('定位票据已过期，请重新打开所在文件夹', 'not_found', 404)
        record.unlink(missing_ok=True)
        _reveal(entry['path'])
        return jsonify(ok=True)

    @app.post('/api/standalone/edit')
    def standalone_edit():
        body = request.get_json(silent=True)
        ticket = body.get('ticket') if isinstance(body, dict) else None
        program = body.get('program') if isinstance(body, dict) else None
        if not isinstance(ticket, str) or len(ticket) != 32 or any(c not in '0123456789abcdef' for c in ticket):
            raise SearchError('无效的编辑票据', 'invalid_id')
        record = session_dir / ('edit-' + ticket + '.json')
        entry = read_json(record, None)
        if not entry or time.time() - record.stat().st_mtime > 300:
            raise SearchError('编辑票据已过期，请重新发起', 'not_found', 404)
        record.unlink(missing_ok=True)
        opened = ''
        if program:
            # 用户在程序选择层指定的程序：必须是票据里登记过的候选之一。
            match = next((item for item in entry.get('choices') or []
                          if str(item.get('path', '')).lower() == str(program).lower()), None)
            if not match:
                raise SearchError('所选的打开程序不可用，请重新发起编辑', 'invalid_id')
            _open_in_cad(entry['path'], match['path'])
            opened = str(match.get('name') or Path(match['path']).stem)
        else:
            executable = str(read_json(session_dir / 'settings.json', {}).get('cad_exe') or '').strip()
            _open_in_cad(entry['path'], executable)
            opened = Path(executable).stem if executable else ''
        return jsonify(ok=True, mode=entry.get('mode', 'copy'), name=entry.get('name', ''),
                       path=entry.get('path', ''), program=opened)
