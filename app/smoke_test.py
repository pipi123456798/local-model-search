"""对真实可执行产物进行离线验收，仅使用临时生成的几何模型。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import tempfile
import time
import traceback
import urllib.error
import urllib.request
import uuid


def run_self_test(output):
    from launcher import prepare_session, start_service, wait_ready
    from runtime import resource_root
    from storage import atomic_json
    import trimesh
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.STEPControl import STEPControl_Writer, STEPControl_AsIs

    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    result = {'ok': False, 'platform': platform.system(), 'machine': platform.machine(), 'checks': []}
    previous = os.environ.get('LOCAL_MODEL_SEARCH_DATA_DIR')
    try:
        with tempfile.TemporaryDirectory(prefix='验收 models ', dir=output.parent) as folder:
            work = Path(folder)
            os.environ['LOCAL_MODEL_SEARCH_DATA_DIR'] = str(work / '用户数据')
            source = work / '模型库'
            source.mkdir()
            mesh = trimesh.creation.box(extents=(2, 3, 4))
            mesh.export(source / '测试.stl')
            (source / '测试.3mf').write_bytes(trimesh.Scene(mesh).export(file_type='3mf'))
            writer = STEPControl_Writer()
            writer.Transfer(BRepPrimAPI_MakeBox(2, 3, 4).Shape(), STEPControl_AsIs)
            if int(writer.Write(str(source / '测试.step'))) != 1:
                raise RuntimeError('STEP 样例生成失败')
            config = {'device': 'cpu', 'kit': str(resource_root() / 'kit')}
            data, session, _, _ = prepare_session(config)
            process = start_service(data, session, config)
            try:
                info = wait_ready(process, session)
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

                def api(path, body=None, token=True):
                    headers = {'X-Search-Token': info['token']} if token else {}
                    encoded = None
                    if body is not None:
                        headers['Content-Type'] = 'application/json'
                        encoded = json.dumps(body).encode('utf-8')
                    req = urllib.request.Request(info['origin'] + path, data=encoded, headers=headers)
                    with opener.open(req, timeout=60) as response:
                        content = response.read()
                        return json.loads(content) if 'application/json' in response.headers.get('Content-Type', '') else content

                def finished(state):
                    until = time.monotonic() + 600
                    while time.monotonic() < until:
                        job = api('/api/tasks/' + state['id'])
                        if job['status'] == 'done':
                            return job['result']
                        if job['status'] in ('error', 'cancelled'):
                            raise RuntimeError(job.get('message', str(job)))
                        time.sleep(.3)
                    raise TimeoutError('验收任务超时')

                health = api('/api/health')
                if not health['ready'] or not health['step_available']:
                    raise RuntimeError('环境校验失败：' + str(health))
                result['checks'].append('依赖与权重资源齐全')
                if b'Local Model Search' not in api('/') or not api('/static/viewer.bundle.js'):
                    raise RuntimeError('网页资源缺失')
                result['checks'].append('界面与离线 3D 资源')
                try:
                    api('/api/libraries', token=False)
                    raise RuntimeError('匿名 API 请求未被拒绝')
                except urllib.error.HTTPError as exc:
                    if exc.code != 401:
                        raise
                result['checks'].append('会话令牌鉴权')
                gid = uuid.uuid4().hex
                atomic_json(session / ('grant-' + gid + '.json'), {'kind': 'directory', 'path': str(source)})
                built = finished(api('/api/libraries', {'name': '离线验收', 'grant': gid}))
                if built['library']['count'] != 3 or built['failures']:
                    raise RuntimeError('STL/STEP/3MF 建库失败：' + str(built))
                result['checks'].append('STL/STEP/3MF 三格式建库及真实推理')
                gid = uuid.uuid4().hex
                atomic_json(session / ('grant-' + gid + '.json'), {'kind': 'file', 'path': str(source / '测试.stl')})
                found = finished(api('/api/search', {'grant': gid, 'topk': 3}))
                own = next((row for row in found['results'] if row['name'] == '测试.stl'), None)
                if own is None or own['similarity'] < .999:
                    raise RuntimeError('自查询相似度不正确')
                if not api(found['preview']).startswith(b'glTF'):
                    raise RuntimeError('GLB 预览生成失败')
                result['checks'].append('自查询与 GLB 预览')
                result['similarity'] = own['similarity']
                refreshed = finished(api('/api/libraries/' + built['library']['id'] + '/refresh', {}))
                if refreshed['reused'] != 3:
                    raise RuntimeError('刷新未复用三份缓存')
                result['checks'].append('已有缓存与中文空格路径')
            finally:
                (session / 'stop').write_text('', encoding='utf-8')
                try:
                    process.wait(timeout=20)
                except Exception:
                    process.kill()
                    process.wait(timeout=5)
                    raise
                if (session / 'queries').exists():
                    raise RuntimeError('后台退出后未清理会话查询缓存')
            if process.returncode != 0:
                raise RuntimeError('后台退出码：%s' % process.returncode)
            result['checks'].append('后台退出与查询缓存清理')
            result['ok'] = True
    except Exception:
        result['error'] = traceback.format_exc()
        traceback.print_exc()
    finally:
        if previous is None:
            os.environ.pop('LOCAL_MODEL_SEARCH_DATA_DIR', None)
        else:
            os.environ['LOCAL_MODEL_SEARCH_DATA_DIR'] = previous
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if result['ok'] else 1
