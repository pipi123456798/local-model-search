"""本地库：只读源模型、不可变索引版本、原子注册表和跨进程写锁。"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
import time
import uuid

import numpy as np
from inference import MAX_MODEL_BYTES, MODEL_EXTS, SearchError, contained, digest_file, normalized, native_path, display_path


def atomic_json(path, value):
    path = native_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        # Windows 的索引器、杀毒扫描或并发短读可能临时占用目标句柄。
        for attempt in range(8):
            try:
                os.replace(temp, path)
                break
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(.02 * (attempt + 1))
    finally:
        temp.unlink(missing_ok=True)


def read_json(path, default=None):
    try:
        return json.loads(native_path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default
    except (ValueError, OSError) as exc:
        raise SearchError('本地数据读取失败：' + str(exc), 'storage_error', 500) from exc


def check_id(value):
    if not isinstance(value, str) or len(value) != 32 or any(c not in '0123456789abcdef' for c in value):
        raise SearchError('无效的资源 ID', 'invalid_id')
    return value


def dependencies_match(source, dependencies):
    if not isinstance(dependencies, dict):
        return False
    for relative, expected in dependencies.items():
        asset = contained(source / relative, source)
        if expected is None:
            if asset.exists():
                return False
        elif (not asset.is_file() or asset.stat().st_size > MAX_MODEL_BYTES or
              digest_file(asset) != expected):
            return False
    return True


class Store:
    def __init__(self, root):
        self.root = native_path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.registry = self.root / 'registry.json'
        self._lock = threading.Lock()

    @contextmanager
    def writing(self):
        if not self._lock.acquire(blocking=False):
            raise SearchError('另一个建库或删除任务正在运行，请稍后重试', 'library_busy', 409)
        stream = None
        locked = False
        try:
            stream = (self.root / 'writer.lock').open('a+b')
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError as exc:
                raise SearchError('其他 Studio 实例正在更新检索库', 'library_busy', 409) from exc
            yield
        finally:
            if stream:
                if locked:
                    stream.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream, fcntl.LOCK_UN)
                stream.close()
            self._lock.release()

    def libraries(self):
        data = read_json(self.registry, {'libraries': []})
        if not isinstance(data, dict) or not isinstance(data.get('libraries'), list):
            raise SearchError('检索库注册表损坏，请恢复备份', 'storage_error', 500)
        return data['libraries']

    def library(self, lid):
        check_id(lid)
        for lib in self.libraries():
            if lib['id'] == lid:
                return lib
        raise SearchError('检索库不存在', 'not_found', 404)

    def snapshot(self, lid):
        lib = self.library(lid)
        folder = self.root / 'libraries' / check_id(lid) / 'versions' / check_id(lib['version'])
        metadata = read_json(folder / 'models.json')
        if metadata is None:
            raise SearchError('索引元数据不存在，请刷新检索库', 'storage_error', 500)
        with np.load(folder / 'index.npz', allow_pickle=False) as data:
            feats = data['feats'].copy()
        return lib, metadata, feats

    def model(self, lid, mid):
        check_id(mid)
        lib, models, _ = self.snapshot(lid)
        for model in models:
            if model['id'] == mid:
                return lib, model
        raise SearchError('模型不在当前索引中', 'not_found', 404)

    def preview(self, lid, mid):
        lib, model = self.model(lid, mid)
        path = self.root / 'libraries' / lib['id'] / 'cache' / model['cache'] / 'preview.glb'
        path = contained(path, self.root)
        if not path.is_file():
            raise SearchError('预览缓存不存在，请刷新检索库', 'not_found', 404)
        return path

    def delete(self, lid):
        with self.writing():
            self.library(lid)
            atomic_json(self.registry, {'libraries': [lib for lib in self.libraries() if lib['id'] != lid]})
            # 只清理自有数据根目录，绝不使用注册表中的源目录作为删除目标。
            shutil.rmtree(self.root / 'libraries' / check_id(lid), ignore_errors=True)

    def build(self, lid, name, source, engine, job):
        check_id(lid)
        source = native_path(source)
        if not source.is_dir() or source == self.root or source.is_relative_to(self.root):
            raise SearchError('请选择检索数据目录，不能选择索引缓存目录')
        if not name.strip() or len(name) > 80:
            raise SearchError('库名称应为 1–80 个字符')
        with self.writing():
            signature = engine.signature()
            job.update(stage='scan', message='扫描模型目录')
            files = []
            for parent, dirs, names in os.walk(source, followlinks=False):
                job.check_cancelled()
                dirs[:] = sorted(d for d in dirs if not (Path(parent) / d).is_symlink()
                                 and not (Path(parent) / d).resolve().is_relative_to(self.root))
                for filename in sorted(names):
                    path = Path(parent) / filename
                    if path.suffix.lower() in MODEL_EXTS and not path.is_symlink():
                        contained(path, source)
                        files.append(path)
            files.sort(key=lambda p: p.relative_to(source).as_posix().casefold())
            old = next((lib for lib in self.libraries() if lib['id'] == lid), None)
            if not files and not old:
                raise SearchError('目录中没有支持的模型文件', 'empty_library')
            base = self.root / 'libraries' / lid
            cache = base / 'cache'
            cache.mkdir(parents=True, exist_ok=True)
            # 按实际读取的关联文件验证缓存，不依赖扩展名，也不使无关模型失效。
            manifests = base / 'manifests'
            manifests.mkdir(parents=True, exist_ok=True)
            models, vectors, failures = [], [], []
            reused = 0
            job.update(total=len(files), done=0)
            for index, path in enumerate(files):
                job.check_cancelled()
                rel = path.relative_to(source).as_posix()
                job.update(stage='embed', message=rel, done=index)
                try:
                    if path.stat().st_size > MAX_MODEL_BYTES:
                        raise SearchError('模型超过 256 MB 限制', 'invalid_file', 413)
                    original_hash = digest_file(path)
                    identity = hashlib.sha256((signature + original_hash + path.stem + rel).encode()).hexdigest()
                    manifest_path = manifests / (identity + '.json')
                    cached = False
                    try:
                        manifest = read_json(manifest_path, {})
                        key = manifest.get('cache', '')
                        dependencies = manifest.get('dependencies')
                        if (len(key) == 64 and all(c in '0123456789abcdef' for c in key)
                                and dependencies_match(source, dependencies)):
                            artifact = cache / key
                            feature = normalized(np.load(artifact / 'feature.npy', allow_pickle=False))
                            info = read_json(artifact / 'info.json')
                            cached = (isinstance(info, dict) and 'dimensions' in info and 'faces' in info
                                      and (artifact / 'preview.glb').is_file())
                    except (ValueError, TypeError, AttributeError, OSError, SearchError):
                        pass
                    if cached:
                        reused += 1
                    else:
                        mesh = engine.load_mesh(path, source)
                        dependencies = mesh.metadata.get('local_search_dependencies', {})
                        dependency_key = json.dumps(dependencies, sort_keys=True, ensure_ascii=False)
                        key = hashlib.sha256((identity + dependency_key).encode()).hexdigest()
                        artifact = cache / key
                        artifact.mkdir(parents=True, exist_ok=True)
                        feature = normalized(engine.embed(mesh, path.stem))
                        info = {'dimensions': np.asarray(mesh.extents).tolist(), 'faces': len(mesh.faces)}
                        engine.preview(mesh, artifact / 'preview.glb')
                        with (artifact / 'feature.npy').open('wb') as stream:
                            np.save(stream, feature, allow_pickle=False)
                        atomic_json(artifact / 'info.json', info)
                    if digest_file(path) != original_hash or not dependencies_match(source, dependencies):
                        raise SearchError('文件或关联资源在处理期间被修改，请重试', 'source_changed', 409)
                    if not cached:
                        atomic_json(manifest_path, {'cache': key, 'dependencies': dependencies})
                    mid = hashlib.md5(rel.encode('utf-8')).hexdigest()
                    parts = path.relative_to(source).parts
                    models.append({'id': mid, 'stem': path.stem, 'name': path.name,
                                   'category': parts[0] if len(parts) > 1 else '未分类',
                                   'relative_path': rel, 'path': display_path(path), 'cache': key,
                                   'sha256': original_hash, 'mtime_ns': path.stat().st_mtime_ns,
                                   'bytes': path.stat().st_size, **info})
                    vectors.append(feature)
                except Exception as exc:
                    failures.append({'file': rel, 'error': str(exc)})
                job.update(done=index + 1, success=len(models), failed=len(failures), reused=reused,
                           failures=failures[-100:])
            job.check_cancelled()
            if files and not vectors:
                raise SearchError('全部模型处理失败，旧索引保持不变。' + failures[0]['error'], 'build_failed', 422)
            version = uuid.uuid4().hex
            destination = base / 'versions' / version
            destination.mkdir(parents=True)
            feats = np.stack(vectors).astype(np.float32) if vectors else np.empty((0, 0), np.float32)
            with (destination / 'index.npz').open('wb') as stream:
                np.savez_compressed(stream, stems=np.asarray([m['stem'] for m in models], dtype=str),
                                    categories=np.asarray([m['category'] for m in models], dtype=str), feats=feats)
            atomic_json(destination / 'models.json', models)
            atomic_json(destination / 'failures.json', failures)
            job.check_cancelled()
            entry = {'id': lid, 'name': name.strip(), 'source': display_path(source), 'count': len(models),
                     'version': version, 'signature': signature, 'updated_at': time.time(),
                     'failed': len(failures), 'dimension': int(feats.shape[1])}
            libs = [lib for lib in self.libraries() if lib['id'] != lid]
            libs.append(entry)
            atomic_json(self.registry, {'libraries': libs})
            # 不在请求中删除旧快照，避免其他实例仍在读取时发生竞争。
            return {'library': entry, 'failures': failures, 'reused': reused}

    def search(self, feat, lids, keywords, topk, signature, exclude=None):
        libs = self.libraries()
        if lids is not None:
            if not isinstance(lids, list) or not lids:
                raise SearchError('请至少选择一个检索库', 'empty_scope')
            unknown = set(lids) - {lib['id'] for lib in libs}
            if unknown:
                raise SearchError('所选检索库已被删除，请刷新列表', 'not_found', 404)
            libs = [lib for lib in libs if lib['id'] in lids]
        if not libs:
            raise SearchError('请先建立本地检索库', 'empty_scope')
        feat = normalized(feat)
        words = [word.casefold() for word in keywords.split() if word]
        results = []
        for lib in libs:
            lib, models, feats = self.snapshot(lib['id'])
            if lib['signature'] != signature:
                raise SearchError('库“%s”的特征版本不同，请先刷新该库' % lib['name'], 'stale_index', 409)
            if not models:
                continue
            if feats.shape[1] != len(feat):
                raise SearchError('索引维度与当前模型不一致，请重建', 'stale_index', 409)
            scores = feats @ feat
            for idx in np.argsort(-scores, kind='stable'):
                model = models[int(idx)]
                if exclude == (lib['id'], model['id']):
                    continue
                text = '\n'.join([lib['name'], model['name'], model['category'], model['relative_path']]).casefold()
                if not all(word in text for word in words):
                    continue
                results.append({**model, 'library': lib['id'], 'library_name': lib['name'],
                                'similarity': float(np.clip(scores[idx], -1, 1))})
        results.sort(key=lambda row: (-row['similarity'], row['library'], row['id']))
        return [{**row, 'rank': index + 1} for index, row in enumerate(results[:topk])]
