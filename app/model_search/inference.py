"""从原 webapp/engines.py 迁移的纯 Shape 推理协议；不包含训练或投影头。"""
from __future__ import annotations

import hashlib
import io
import importlib.util
import dataclasses
import os
from pathlib import Path
import sys
import zipfile

import numpy as np

MODEL_EXTS = {'.step', '.stp', '.obj', '.stl', '.ply', '.glb', '.gltf', '.3mf', '.off'}
PROTOCOL = 'shape-pooled-l2-stem-obj-roundtrip-v1'
MAX_MODEL_BYTES = 256 * 1024 * 1024


class SearchError(Exception):
    def __init__(self, message, code='invalid_input', status=400):
        super().__init__(message)
        self.code, self.status = code, status


def native_path(path) -> Path:
    path = str(Path(path).resolve())
    if os.name == 'nt' and not path.startswith('\\\\?\\'):
        path = '\\\\?\\UNC\\' + path[2:] if path.startswith('\\\\') else '\\\\?\\' + path
    return Path(path)


def display_path(path) -> str:
    value = str(path)
    if value.startswith('\\\\?\\UNC\\'):
        return '\\\\' + value[8:]
    return value[4:] if value.startswith('\\\\?\\') else value


def contained(path: Path, root: Path) -> Path:
    path, root = native_path(path), native_path(root)
    if not path.is_relative_to(root):
        raise SearchError('关联文件超出已授权目录', 'path_denied', 403)
    return path


def digest_file(path):
    h = hashlib.sha256()
    with native_path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def stem_seed(stem):
    return int(hashlib.md5(stem.encode('utf-8')).hexdigest()[:8], 16)


def normalized(feat):
    feat = np.asarray(feat, dtype=np.float32)
    norm = float(np.linalg.norm(feat))
    if feat.ndim != 1 or not np.isfinite(feat).all() or norm < 1e-12:
        raise SearchError('模型返回无效特征', 'invalid_embedding', 422)
    return feat / norm


class ShapeInference:
    """单工作线程调用；模型及全局随机数状态不跨线程共享。"""
    def __init__(self, settings):
        self.kit = Path(settings.get('kit', '')).expanduser().resolve()
        self.checkpoint = Path(settings.get('checkpoint', '')).expanduser().resolve()
        self.requested_device = settings.get('device', 'auto')
        self.model = None
        self.device = 'cpu'
        self._signature = None
        self._fingerprint = None

    def health(self):
        missing = [name for name in ('torch', 'trimesh', 'scipy', 'einops', 'lxml', 'PIL', 'yaml')
                   if importlib.util.find_spec(name) is None]
        errors = []
        if missing:
            errors.append('缺少 Python 依赖：' + ', '.join(missing))
        if not (self.kit / 'shape_foundation/models/gaot_backbone.py').is_file():
            errors.append('请选择包含 shape_foundation 的 MFCAD_shape 工具包目录')
        if not self.checkpoint.is_file():
            errors.append('Shape checkpoint 不存在，请在环境设置中选择现有权重')
        return {'ready': not errors, 'errors': errors, 'device': self.device,
                'model_loaded': self.model is not None, 'protocol': PROTOCOL,
                'step_available': importlib.util.find_spec('OCP') is not None or
                                  importlib.util.find_spec('OCC') is not None}

    def signature(self):
        sources = sorted((self.kit / 'shape_foundation').rglob('*.py'))
        sources += [self.kit / 'step_utils.py', self.checkpoint]
        try:
            fingerprint = [(str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in sources]
        except OSError as exc:
            raise SearchError('工具包或权重不存在，请检查环境设置', 'environment', 503) from exc
        if self._fingerprint is not None and fingerprint != self._fingerprint:
            raise SearchError('工具包或权重已改变，请重启本地检索服务后刷新索引', 'environment_changed', 409)
        if self._signature is None:
            health = self.health()
            if not health['ready']:
                raise SearchError('；'.join(health['errors']), 'environment', 503)
            h = hashlib.sha256(PROTOCOL.encode())
            h.update(digest_file(self.checkpoint).encode())
            # 工具包实现变化也使缓存失效，避免混合不同特征空间。
            sources = sorted((self.kit / 'shape_foundation').rglob('*.py'))
            sources += [self.kit / 'step_utils.py']
            for src in sources:
                h.update(str(src.relative_to(self.kit)).encode())
                h.update(src.read_bytes())
            self._signature = h.hexdigest()
            self._fingerprint = fingerprint
        return self._signature

    def ensure_model(self):
        if self.model is not None:
            return
        self.signature()
        sys.path.insert(0, str(self.kit))
        import torch
        from shape_foundation.models.gaot_backbone import GAOTBackbone
        from shape_foundation.configs import default as shape_config
        # 仅放行已安装工具包的配置数据类，不允许 checkpoint 执行任意 pickle 对象。
        allowed = [value for value in vars(shape_config).values()
                   if isinstance(value, type) and dataclasses.is_dataclass(value)
                   and value.__module__ == shape_config.__name__]
        with torch.serialization.safe_globals(allowed):
            ckpt = torch.load(str(self.checkpoint), map_location='cpu', weights_only=True)
        self.cfg = ckpt['config']
        self.cfg.tokenizer.neighbor.backend = 'auto'
        self.cfg.tokenizer.neighbor.enable_caching = False
        model = GAOTBackbone(self.cfg)
        model.load_state_dict(ckpt['model_state_dict'], strict=False)
        if self.requested_device == 'auto' and torch.cuda.is_available():
            try:
                free = [(torch.cuda.mem_get_info(i)[0], i)
                        for i in range(torch.cuda.device_count())]
                size, index = max(free)
                if size >= 2 * 1024 ** 3:
                    self.device = 'cuda:%d' % index
            except RuntimeError:
                self.device = 'cpu'
        torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
        try:
            model.to(self.device)
        except RuntimeError:
            self.device = 'cpu'
            model.to('cpu')
        self.model = model.eval()

    def load_mesh(self, source, root):
        import trimesh
        dependencies = {}
        source = contained(Path(source), Path(root))
        if source.suffix.lower() not in MODEL_EXTS:
            raise SearchError('不支持的模型格式', 'unsupported_format', 415)
        if not source.is_file() or source.stat().st_size > MAX_MODEL_BYTES:
            raise SearchError('模型不存在或超过 256 MB 限制', 'invalid_file', 413)
        if source.suffix.lower() == '.3mf':
            with zipfile.ZipFile(source) as archive:
                entries = archive.infolist()
                if len(entries) > 10000 or sum(entry.file_size for entry in entries) > 512 * 1024 * 1024:
                    raise SearchError('3MF 解压体积超过 512 MB 限制', 'invalid_file', 413)
        if source.suffix.lower() in {'.step', '.stp'}:
            module_path = self.kit / 'step_utils.py'
            if not module_path.is_file():
                raise SearchError('工具包中缺少 step_utils.py', 'environment', 503)
            spec = importlib.util.spec_from_file_location('local_search_step', module_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            mesh = module.step_to_trimesh(str(source))
        else:
            class LocalResolver(trimesh.resolvers.Resolver):
                def __init__(self, base):
                    self.base = base

                def get(self, name):
                    if '://' in str(name):
                        raise SearchError('模型不能引用网络资源', 'path_denied', 403)
                    asset = contained(self.base / str(name), Path(root))
                    relative = asset.relative_to(native_path(root)).as_posix()
                    dependencies[relative] = None
                    if asset.stat().st_size > MAX_MODEL_BYTES:
                        raise SearchError('模型关联资源过大', 'invalid_file', 413)
                    data = asset.read_bytes()
                    dependencies[relative] = hashlib.sha256(data).hexdigest()
                    return data

                def write(self, name, data):
                    raise SearchError('模型源目录只读', 'path_denied', 403)

                def namespaced(self, namespace):
                    return LocalResolver(contained(self.base / namespace, Path(root)))

                def keys(self):
                    return []

            # 显式 resolver 阻止 OBJ/GLTF 通过关联文件读取授权目录之外的内容。
            mesh = trimesh.load(str(source), force='mesh',
                                resolver=LocalResolver(source.parent), allow_remote=False)
            if source.suffix.lower() != '.obj' and isinstance(mesh, trimesh.Trimesh):
                # 保持原 convert_to_obj → OBJ 直载的顶点精度和面顺序；仅在内存中转换。
                obj = mesh.export(file_type='obj', include_texture=False)
                mesh = trimesh.load(io.StringIO(obj), file_type='obj', force='mesh',
                                    resolver=LocalResolver(source.parent), allow_remote=False)
        if isinstance(mesh, trimesh.Scene):
            mesh = mesh.dump(concatenate=True)
        if not isinstance(mesh, trimesh.Trimesh) or not len(mesh.faces):
            raise SearchError('文件中没有有效三角网格', 'empty_mesh', 422)
        if not np.isfinite(mesh.vertices).all() or mesh.area <= 0:
            raise SearchError('模型包含无效坐标或零面积网格', 'invalid_mesh', 422)
        mesh.metadata['local_search_dependencies'] = dependencies
        return mesh

    def embed(self, mesh, stem):
        self.ensure_model()
        import torch
        from shape_foundation.data.preprocessing import MeshPreprocessor
        from shape_foundation.data.sampling import SurfaceSampler
        seed = stem_seed(stem)
        np.random.seed(seed)
        torch.manual_seed(seed)
        prep = MeshPreprocessor(self.cfg.input)(
            np.asarray(mesh.vertices, dtype=np.float64),
            np.asarray(mesh.faces, dtype=np.int64))
        sampled = SurfaceSampler(self.cfg.input).sample(
            prep['vertices'], prep['faces'], prep['normals'], prep.get('curvature'))
        points = sampled['points']
        feats = MeshPreprocessor(self.cfg.input).build_features(
            points, sampled['normals'], sampled.get('curvature'))
        pts = torch.from_numpy(points).unsqueeze(0).to(self.device)
        fts = torch.from_numpy(feats).unsqueeze(0).to(self.device)
        nrm = torch.from_numpy(sampled['normals']).unsqueeze(0).to(self.device)
        crv = None
        if sampled.get('curvature') is not None:
            c = sampled['curvature']
            crv = torch.from_numpy(c[:, None] if c.ndim == 1 else c).unsqueeze(0).to(self.device)
        with torch.no_grad():
            feat = self.model.forward_tokens(pts, fts, nrm, crv)['pooled_embedding'][0]
        return normalized(feat.detach().cpu().numpy())

    @staticmethod
    def preview(mesh, destination):
        # 展示副本居中归一化；原始坐标仍用于特征提取及尺寸信息。
        viz = mesh.copy()
        vertices = np.asarray(viz.vertices, dtype=np.float64)
        vertices -= (vertices.max(axis=0) + vertices.min(axis=0)) / 2
        vertices /= max(float(np.linalg.norm(vertices, axis=1).max()), 1e-12)
        viz.vertices = vertices
        native_path(destination).write_bytes(viz.export(file_type='glb'))
