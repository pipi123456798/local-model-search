"""Bambu Studio 本地模型检索服务，仅监听回环地址，不依赖旧 Web 服务。"""
from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import re
from pathlib import Path
import secrets
import shutil
import threading
import time
import uuid
import sys

import numpy as np
from flask import Flask, jsonify, request, send_file, send_from_directory
from werkzeug.exceptions import HTTPException
from werkzeug.serving import make_server

from inference import MAX_MODEL_BYTES, MODEL_EXTS, SearchError, ShapeInference, contained, native_path, display_path
from storage import Store, atomic_json, check_id, read_json
from tasks import Tasks

WEB_ROOT = (Path(sys._MEIPASS) / 'app/web/model_search' if getattr(sys, 'frozen', False)
            else Path(__file__).resolve().parents[1] / 'web/model_search')


def copy_sibling(source, limit=1000):
    """在原文件旁创建编辑副本（<名称>_copy、_copy2…），返回副本路径。"""
    for index in range(1, limit + 1):
        suffix = '_copy' if index == 1 else '_copy%d' % index
        target = source.with_name(source.stem + suffix + source.suffix)
        if not target.exists():
            shutil.copy2(native_path(source), native_path(target))
            return target
    raise SearchError('无法创建编辑副本：原文件夹中同名文件过多')


_EDIT_EXE_SKIP = {'rundll32.exe', 'cmd.exe'}


def _command_exe(command):
    """从注册表 shell\\open\\command 命令串中解析可执行文件路径。"""
    match = re.match(r'\s*"([^"]+)"', command or '')
    if match:
        return match.group(1)
    match = re.match(r'\s*(.+?\.exe)\b', command or '', re.IGNORECASE)
    return match.group(1).strip() if match else ''


def _resolve_app(exe_name):
    """把关联表中的 exe 文件名解析为完整路径（App Paths / Applications / 卸载信息 / PATH）。"""
    import winreg
    keys = ((winreg.HKEY_CURRENT_USER, r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\%s' % exe_name),
            (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\%s' % exe_name),
            (winreg.HKEY_CLASSES_ROOT, r'Applications\%s\shell\open\command' % exe_name))
    for root, sub in keys:
        try:
            with winreg.OpenKey(root, sub) as key:
                value = str(winreg.QueryValueEx(key, '')[0] or '')
        except OSError:
            continue
        resolved = _command_exe(value) or value
        if resolved:
            return resolved
    return _resolve_installed(exe_name) or (shutil.which(exe_name) or '')


def _resolve_installed(exe_name):
    """在卸载注册表的 DisplayIcon / InstallLocation 中按文件名匹配已安装路径。"""
    import winreg
    target = exe_name.lower()
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for sub in (r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
                    r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'):
            try:
                with winreg.OpenKey(hive, sub) as root:
                    names = []
                    index = 0
                    while True:
                        try:
                            names.append(winreg.EnumKey(root, index))
                        except OSError:
                            break
                        index += 1
            except OSError:
                continue
            for name in names:
                try:
                    with winreg.OpenKey(hive, sub + '\\' + name) as entry:
                        values = []
                        for value_name in ('DisplayIcon', 'InstallLocation'):
                            try:
                                values.append(str(winreg.QueryValueEx(entry, value_name)[0] or ''))
                            except OSError:
                                values.append('')
                except OSError:
                    continue
                for value in (values[0].split(',')[0].strip().strip('"'),
                              (values[1].rstrip('\\') + '\\' + exe_name) if values[1] else ''):
                    if value and os.path.basename(value).lower() == target and os.path.isfile(value):
                        return value
    return ''


def candidate_editors(ext):
    """枚举本机已安装且能打开该后缀的程序（Windows 注册表关联；其他系统返回空列表）。

    数据来源：打开方式历史、OpenWithProgids 与当前默认程序；磁盘上不存在的程序会被过滤，
    默认程序排在首位并带 default 标记，供前端多程序选择层展示。
    """
    if os.name != 'nt' or not ext:
        return []
    import winreg
    ext = ext.lower()
    ext_key = r'Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts' + '\\' + ext
    scripts = {}

    def register(path):
        path = os.path.expandvars(str(path or '').strip().strip('"'))
        if not path.lower().endswith('.exe') or Path(path).name.lower() in _EDIT_EXE_SKIP:
            return
        if not Path(path).is_file():
            return
        stem = Path(path).stem
        name = re.sub(r'[-_]+', ' ', stem).strip()
        if name != stem:
            name = name.title()
        scripts.setdefault(path.lower(), {'name': name, 'path': path})

    try:  # 用户在「打开方式」中用过的程序。
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, ext_key + r'\OpenWithList') as key:
            index = 0
            while True:
                try:
                    _, value, _ = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                if str(value).lower().endswith('.exe'):
                    register(_resolve_app(str(value)))
    except OSError:
        pass

    progids = set()
    for root, sub in ((winreg.HKEY_CURRENT_USER, ext_key + r'\OpenWithProgids'),
                      (winreg.HKEY_CLASSES_ROOT, ext + r'\OpenWithProgids')):  # 登记过该格式的程序。
        try:
            with winreg.OpenKey(root, sub) as key:
                index = 0
                while True:
                    try:
                        name, _, _ = winreg.EnumValue(key, index)
                    except OSError:
                        break
                    index += 1
                    progids.add(name)
        except OSError:
            continue

    default_progid = ''
    try:  # 当前默认程序。
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, ext_key + r'\UserChoice') as key:
            default_progid = str(winreg.QueryValueEx(key, 'ProgId')[0] or '')
    except OSError:
        pass
    if default_progid:
        progids.add(default_progid)

    default_path = ''
    for progid in progids:
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid + r'\shell\open\command') as key:
                command = str(winreg.QueryValueEx(key, '')[0] or '')
        except OSError:
            continue
        exe = os.path.expandvars(_command_exe(command))
        if not exe:
            continue
        register(exe)
        if progid == default_progid:
            default_path = exe.lower()

    for entry in scripts.values():
        entry['default'] = bool(default_path) and entry['path'].lower() == default_path
    return sorted(scripts.values(), key=lambda item: (not item['default'], item['name'].lower()))


def create_app(data_dir, session_dir, settings, engine=None):
    data_dir, session_dir = native_path(data_dir), native_path(session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)
    store = Store(data_dir)
    engine = engine or ShapeInference(settings)
    tasks = Tasks(session_dir / 'tasks')
    token = secrets.token_hex(32)
    app = Flask(__name__, static_folder=None)
    app.config.update(MAX_CONTENT_LENGTH=MAX_MODEL_BYTES + 1024 * 1024,
                      SEARCH_ORIGIN='', SEARCH_TOKEN=token, SEARCH_SESSION=str(session_dir))
    app.extensions.update(search_store=store, search_engine=engine, search_tasks=tasks)
    queries = session_dir / 'queries'
    queries.mkdir(exist_ok=True)
    build_pending = set()
    pending_lock = threading.Lock()
    grants = {}
    grant_lock = threading.Lock()
    query_lock = threading.Lock()

    @app.before_request
    def authenticate():
        expected = app.config['SEARCH_ORIGIN']
        if request.remote_addr not in ('127.0.0.1', '::1'):
            raise SearchError('仅允许本机访问', 'forbidden', 403)
        if expected and request.host_url.rstrip('/') != expected:
            raise SearchError('无效 Host', 'forbidden', 403)
        origin = request.headers.get('Origin')
        if origin and origin != expected:
            raise SearchError('不允许跨站访问', 'forbidden', 403)
        if request.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise SearchError('不允许跨站访问', 'forbidden', 403)
        if request.path.startswith('/api/'):
            supplied = request.headers.get('X-Search-Token', '')
            if not hmac.compare_digest(supplied.encode('utf-8'), token.encode('ascii')):
                raise SearchError('会话失效，请重新打开本地检索页面', 'unauthorized', 401)

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Content-Security-Policy'] = (
            "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' blob: data:; connect-src 'self' blob:; worker-src 'self' blob:; "
            "font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        return response

    @app.errorhandler(SearchError)
    def search_error(exc):
        return jsonify(error=str(exc), code=exc.code), exc.status

    @app.errorhandler(HTTPException)
    def http_error(exc):
        message = '文件超过 256 MB 限制' if exc.code == 413 else exc.description
        return jsonify(error=message, code='http_error'), exc.code

    @app.errorhandler(Exception)
    def unknown_error(exc):
        app.logger.exception('本地检索请求失败')
        return jsonify(error='操作失败，请查看本地服务日志并重试', code='internal_error'), 500

    @app.get('/')
    def index():
        return send_from_directory(WEB_ROOT, 'index.html')

    @app.get('/static/<path:name>')
    def static_asset(name):
        if name not in {'app.js', 'style.css', 'viewer.bundle.js', 'viewer.bundle.js.LEGAL.txt',
                        'standalone-bridge.js'}:
            raise SearchError('资源不存在', 'not_found', 404)
        return send_from_directory(WEB_ROOT, name)

    @app.get('/api/health')
    def health():
        return jsonify(**engine.health(), python=os.sys.executable, data_dir=display_path(data_dir),
                       kit=str(engine.kit), checkpoint=str(engine.checkpoint))

    def payload():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise SearchError('请求必须为 JSON 对象')
        return value

    def grant(gid, kind):
        check_id(gid)
        with grant_lock:
            entry = grants.get(gid)
            if entry is None:
                path = session_dir / ('grant-' + gid + '.json')
                entry = read_json(path)
                if not entry or time.time() - path.stat().st_mtime > 300:
                    raise SearchError('授权已过期，请重新选择本地文件或目录', 'grant_expired', 403)
                grants[gid] = entry
                path.unlink(missing_ok=True)
            if entry.get('kind') != kind:
                raise SearchError('授权类型不匹配', 'path_denied', 403)
            return Path(entry['path']).resolve()

    def queue_build(lid, name, source):
        with pending_lock:
            if lid in build_pending:
                raise SearchError('该库已有建库任务', 'library_busy', 409)
            build_pending.add(lid)
        def release():
            with pending_lock:
                build_pending.discard(lid)
        try:
            state = tasks.submit('build', name,
                                 lambda job: store.build(lid, name, source, engine, job), release)
        except Exception:
            release()
            raise
        return jsonify(state), 202

    @app.get('/api/libraries')
    def libraries():
        return jsonify(libraries=store.libraries())

    @app.post('/api/libraries')
    def create_library():
        body = payload()
        name = body.get('name', '')
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise SearchError('请输入 1–80 个字符的库名称')
        source = grant(body.get('grant'), 'directory')
        lid = uuid.uuid5(uuid.NAMESPACE_URL, os.path.normcase(str(source)) + '\0' + name.strip()).hex
        return queue_build(lid, name, source)

    @app.post('/api/libraries/<lid>/refresh')
    def refresh_library(lid):
        lib = store.library(lid)
        return queue_build(lid, lib['name'], Path(lib['source']))

    @app.delete('/api/libraries/<lid>')
    def delete_library(lid):
        with pending_lock:
            if lid in build_pending:
                raise SearchError('请先等待或取消该库建库任务', 'library_busy', 409)
        store.delete(lid)
        return jsonify(ok=True)

    @app.get('/api/libraries/<lid>/models')
    def models(lid):
        lib, rows, _ = store.snapshot(lid)
        keyword = request.args.get('q', '').casefold()
        try:
            offset = max(0, int(request.args.get('offset', 0)))
            limit = max(1, min(100, int(request.args.get('limit', 24))))
        except ValueError as exc:
            raise SearchError('分页参数无效') from exc
        rows = [row for row in rows if keyword in (row['name'] + row['relative_path']).casefold()]
        return jsonify(total=len(rows), models=[{**row, 'library': lid, 'library_name': lib['name']}
                                               for row in rows[offset:offset + limit]])

    @app.get('/api/models/<lid>/<mid>/preview')
    def model_preview(lid, mid):
        return send_file(store.preview(lid, mid), mimetype='model/gltf-binary')

    @app.post('/api/models/<lid>/<mid>/reveal')
    def reveal(lid, mid):
        lib, model = store.model(lid, mid)
        source = contained(Path(model['path']), Path(lib['source']))
        if not source.is_file():
            raise SearchError('源文件已移动或删除，请刷新检索库', 'not_found', 404)
        ticket = uuid.uuid4().hex
        atomic_json(session_dir / ('reveal-' + ticket + '.json'), {'path': display_path(source.parent)})
        return jsonify(ticket=ticket)

    @app.post('/api/models/<lid>/<mid>/edit')
    def edit_model(lid, mid):
        body = request.get_json(silent=True)
        mode = body.get('mode') if isinstance(body, dict) else None
        if mode not in ('direct', 'copy'):
            raise SearchError('打开编辑方式无效（direct 或 copy）')
        lib, model = store.model(lid, mid)
        source = contained(Path(model['path']), Path(lib['source']))
        if not source.is_file():
            raise SearchError('源文件已移动或删除，请刷新检索库', 'not_found', 404)
        target = copy_sibling(source) if mode == 'copy' else source
        # config.json 指定了 CAD 程序时直接使用；否则枚举本机可打开该格式的程序供用户选择。
        configured = str(read_json(session_dir / 'settings.json', {}).get('cad_exe') or '').strip()
        choices = [] if configured else candidate_editors(target.suffix)
        ticket = uuid.uuid4().hex
        atomic_json(session_dir / ('edit-' + ticket + '.json'),
                    {'path': display_path(target), 'mode': mode, 'name': target.name, 'choices': choices})
        return jsonify(ticket=ticket, mode=mode, name=target.name, choices=choices)

    def search_options(body):
        try:
            topk = int(body.get('topk', 10))
        except (ValueError, TypeError) as exc:
            raise SearchError('Top-K 必须为整数') from exc
        if not 1 <= topk <= 50:
            raise SearchError('Top-K 应在 1–50 之间')
        lids = body.get('libraries')
        if isinstance(lids, str):
            try:
                lids = json.loads(lids)
            except ValueError as exc:
                raise SearchError('检索库参数无效') from exc
        if lids is not None and (not isinstance(lids, list) or not lids or
                                 any(not isinstance(lid, str) for lid in lids)):
            raise SearchError('请至少选择一个检索库')
        keywords = body.get('keywords', '')
        if not isinstance(keywords, str) or len(keywords) > 200:
            raise SearchError('关键词过长或格式无效')
        return lids, keywords, topk

    def query_file(source, allowed_root, options, query_dir):
        lids, keywords, topk = options
        def run(job):
            started = time.monotonic()
            job.update(stage='convert', message='三角化与预处理')
            mesh = engine.load_mesh(source, allowed_root)
            engine.preview(mesh, query_dir / 'preview.glb')
            job.update(preview='/api/queries/' + query_dir.name + '/preview', query_name=source.name)
            job.check_cancelled()
            job.update(stage='embed', message='提取 Shape 特征')
            feat = engine.embed(mesh, source.stem)
            job.check_cancelled()
            job.update(stage='search', message='检索本地模型库')
            signature = engine.signature()
            np.save(query_dir / 'feature.npy', feat, allow_pickle=False)
            atomic_json(query_dir / 'query.json', {'name': source.name, 'signature': signature})
            rows = store.search(feat, lids, keywords, topk, signature)
            return {'results': rows, 'elapsed': round(time.monotonic() - started, 2),
                    'query': query_dir.name, 'query_name': source.name,
                    'preview': '/api/queries/' + query_dir.name + '/preview'}
        return run

    @app.post('/api/search_cached')
    def search_cached():
        body = payload()
        qid = check_id(body.get('query'))
        options = search_options(body)
        directory = queries / qid
        def run(job):
            meta = read_json(directory / 'query.json')
            if not meta:
                raise SearchError('查询缓存已过期，请重新选择模型', 'not_found', 404)
            signature = engine.signature()
            if meta['signature'] != signature:
                raise SearchError('模型版本已改变，请重新选择查询模型', 'stale_index', 409)
            feature = np.load(directory / 'feature.npy', allow_pickle=False)
            job.update(stage='search', message='使用查询缓存特征检索')
            rows = store.search(feature, *options, signature)
            return {'results': rows, 'query': qid, 'query_name': meta['name'],
                    'preview': '/api/queries/' + qid + '/preview'}
        return jsonify(tasks.submit('search', '调整范围检索', run)), 202

    @app.post('/api/search')
    def search_file():
        if not query_lock.acquire(blocking=False):
            raise SearchError('正在接收另一个查询模型，请稍后重试', 'query_busy', 409)
        try:
            return receive_query()
        finally:
            query_lock.release()

    def receive_query():
        # 上传总量按当前会话限额，避免无限占用磁盘；保留最近 24 小时预览。
        occupied = 0
        for folder in queries.iterdir():
            if folder.is_dir():
                if (time.time() - folder.stat().st_mtime > 86400 and
                        not tasks.active()):
                    shutil.rmtree(folder, ignore_errors=True)
                else:
                    occupied += sum(p.stat().st_size for p in folder.rglob('*') if p.is_file())
        if occupied + MAX_MODEL_BYTES > 1024 ** 3:
            raise SearchError('查询缓存达到 1 GB，请重新启动检索服务释放空间', 'cache_full', 429)
        directory = queries / uuid.uuid4().hex
        directory.mkdir()
        try:
            if request.is_json:
                body = payload()
                source = grant(body.get('grant'), 'file')
                root = source.parent
            else:
                body = request.form
                upload = request.files.get('file')
                if not upload or not upload.filename:
                    raise SearchError('请选择查询模型')
                name = upload.filename.replace('\\', '/').rsplit('/', 1)[-1]
                if len(name) > 200 or ':' in name or Path(name).suffix.lower() not in MODEL_EXTS:
                    raise SearchError('文件名或模型格式无效', 'unsupported_format', 415)
                source = directory / name
                upload.save(source)
                root = directory
            options = search_options(body)
            state = tasks.submit('search', source.name, query_file(source, root, options, directory))
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return jsonify(state), 202

    @app.post('/api/similar')
    def similar():
        body = payload()
        lid, mid = check_id(body.get('library')), check_id(body.get('model'))
        options = search_options(body)
        def run(job):
            lib, models, features = store.snapshot(lid)
            idx = next((i for i, model in enumerate(models) if model['id'] == mid), None)
            if idx is None:
                raise SearchError('模型已不在当前库中', 'not_found', 404)
            job.update(stage='search', message='使用库内预计算特征检索')
            signature = engine.signature()
            if lib['signature'] != signature:
                raise SearchError('查询模型所在库需要刷新', 'stale_index', 409)
            result = store.search(features[idx], *options, signature, exclude=(lid, mid))
            return {'results': result, 'query_name': models[idx]['name'],
                    'preview': '/api/models/%s/%s/preview' % (lid, mid)}
        return jsonify(tasks.submit('search', '库内相似检索', run)), 202

    @app.get('/api/queries/<qid>/preview')
    def query_preview(qid):
        file = queries / check_id(qid) / 'preview.glb'
        if not file.is_file():
            raise SearchError('预览未生成或已过期', 'not_found', 404)
        return send_file(file, mimetype='model/gltf-binary')

    @app.get('/api/tasks/<tid>')
    def task_status(tid):
        return jsonify(tasks.get(tid))

    @app.post('/api/tasks/<tid>/cancel')
    def cancel_task(tid):
        return jsonify(tasks.cancel(tid))

    # 独立版（standalone）扩展：原生选择器与文件管理器定位；桌面宿主模式下不使用。
    from standalone_extra import register_standalone_routes
    register_standalone_routes(app, session_dir)

    return app


def parent_alive(pid):
    if not pid:
        return True
    if pid < 0:
        return False
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = wintypes.DWORD()
        ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel.CloseHandle(handle)
        return bool(ok and code.value == 259)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main():
    parser = argparse.ArgumentParser(description='Bambu Studio 本地 Shape 检索服务')
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--session-dir', required=True)
    parser.add_argument('--settings', required=True)
    parser.add_argument('--parent-pid', type=int, default=0)
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    session_dir = native_path(args.session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=session_dir / 'service.log', level=logging.INFO, encoding='utf-8')
    try:
        settings = read_json(args.settings, {})
        sessions = native_path(args.data_dir) / 'sessions'
        if sessions.is_dir():
            for old in sessions.iterdir():
                if not old.is_dir() or old.is_symlink() or old == session_dir:
                    continue
                try:
                    check_id(old.name)
                    ready = read_json(old / 'ready.json', {})
                    if time.time() - old.stat().st_mtime > 7 * 86400 and not parent_alive(ready.get('pid', -1)):
                        shutil.rmtree(old, ignore_errors=True)
                except (OSError, SearchError):
                    pass
        app = create_app(args.data_dir, session_dir, settings)
        server = make_server('127.0.0.1', args.port, app, threaded=True)
        origin = 'http://127.0.0.1:%d' % server.server_port
        app.config['SEARCH_ORIGIN'] = origin
        atomic_json(session_dir / 'ready.json', {'origin': origin, 'token': app.config['SEARCH_TOKEN'],
                                                     'pid': os.getpid(), 'process_parent': os.getppid()})
        def watch_parent():
            while not (session_dir / 'stop').exists() and parent_alive(args.parent_pid):
                time.sleep(0.5)
            app.extensions['search_tasks'].stop()
            server.shutdown()
        threading.Thread(target=watch_parent, daemon=True).start()
        server.serve_forever()
        server.server_close()
    except Exception as exc:
        atomic_json(session_dir / 'error.json', {'error': str(exc)})
        logging.exception('检索服务启动失败')
        raise
    finally:
        # 会话退出后清除上传内容；索引及可续建缓存保留。
        shutil.rmtree(session_dir / 'queries', ignore_errors=True)


if __name__ == '__main__':
    main()
