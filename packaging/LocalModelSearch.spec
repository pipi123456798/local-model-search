# -*- mode: python ; coding: utf-8 -*-
"""每个平台原生冻结；资源清单不包含个人数据或开发机配置。"""
import os
from pathlib import Path
import sys
from importlib.metadata import distribution
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).parent
sys.path.insert(0, str(root / 'kit'))
version = os.environ.get('LMS_BUILD_VERSION', '1.1.0')
datas = [
    (str(root / 'app/web/model_search'), 'app/web/model_search'),
    (str(root / 'app/assets'), 'app/assets'),
    (str(root / 'build/bundle/config.json'), '.'),
    (os.environ['LMS_LICENSE_DIR'], 'licenses'),
    (str(root / 'LICENSE'), '.'),
    (str(root / 'NOTICE.md'), '.'),
    (str(root / 'packaging/shape-manifest.json'), 'kit'),
    (str(root / 'kit/LICENSE'), 'kit'),
    (str(root / 'kit/step_utils.py'), 'kit'),
    (str(root / 'kit/checkpoints/checkpoint_final.pt'), 'kit/checkpoints'),
]
# 推理指纹需要可读的源码，不能只留 PYZ 字节码。
for path in (root / 'kit/shape_foundation').rglob('*.py'):
    datas.append((str(path), str(Path('kit') / path.relative_to(root / 'kit').parent)))
binaries = []
hidden = ['bootstrap', 'server', 'storage', 'tasks', 'inference', 'standalone_extra',
          'launcher', 'runtime', 'launcher_ui', 'smoke_test', 'step_utils',
          'PIL.Image', 'lxml.etree', 'scipy.spatial', 'scipy.special', 'scipy.sparse',
          'scipy.sparse.csgraph', 'trimesh.exchange.threemf']
hidden += collect_submodules('shape_foundation')
for package in ('OCP', 'webview'):
    data, binary, imports = collect_all(package)
    datas += data
    binaries += binary
    hidden += imports
# OCP wheel 可能把依赖放在包外的 .libs 目录。
for item in distribution('cadquery-ocp-novtk').files or []:
    if any(part.endswith('.libs') for part in item.parts):
        binaries.append((str(distribution('cadquery-ocp-novtk').locate_file(item)), str(Path(item).parent)))
for package in ('trimesh', 'einops', 'torch', 'numpy', 'scipy', 'Flask', 'pywebview', 'cadquery-ocp-novtk'):
    datas += copy_metadata(package)
datas += collect_data_files('trimesh')
a = Analysis(
    [str(root / 'app/launcher.py')],
    pathex=[str(root / 'app'), str(root / 'app/model_search'), str(root / 'kit')],
    binaries=binaries, datas=datas, hiddenimports=hidden,
    excludes=['pytest', 'IPython', 'matplotlib', 'pandas', 'torchvision', 'torchaudio',
              'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'gi', 'numba', 'llvmlite', 'vtk', 'vtkmodules'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='LocalModelSearch',
          debug=False, strip=False, upx=False, console=False,
          target_arch='arm64' if sys.platform == 'darwin' else None,
          codesign_identity=os.environ.get('LMS_CODESIGN_IDENTITY') or None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='LocalModelSearch')
if sys.platform == 'darwin':
    app = BUNDLE(coll, name='LocalModelSearch.app', bundle_identifier='io.github.pipi123456798.localmodelsearch',
                 info_plist={'CFBundleShortVersionString': version, 'CFBundleVersion': version,
                             'LSMinimumSystemVersion': '14.0', 'NSHighResolutionCapable': True,
                             'NSAppleEventsUsageDescription': '用于选择本机模型文件和文件夹。'})
