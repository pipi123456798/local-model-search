"""构建发布资产。

用法：
    python packaging/make_release.py --version 1.0.0 --kit-source D:/path/to/MFCAD_shape

产物（dist/）：
    local-model-search-v<版本>.zip  应用包（脚本 + 代码 + 文档）
    shape-kit.zip                   权重包（shape_foundation/ + step_utils.py + checkpoint），
                                    作为 Release 资产上传，首次运行时由 scripts/fetch_kit.py 自动下载。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / 'dist'
ROOT_DIRS = {'.git', '.venv', 'data', 'dist', 'build', '.tools'}
EXCLUDE_SUFFIXES = {'.pyc', '.pyo', '.part', '.pt', '.log', '.tmp', '.npy', '.npz'}
SOURCE_DIRS = {'app', 'scripts', 'docs', 'packaging', 'tests', 'kit', '.github'}
SOURCE_FILES = {'.gitattributes', '.gitignore', 'LICENSE', 'NOTICE.md', 'README.md',
                'Start-Windows.bat', 'Start-Mac.command', 'config.json', 'requirements.txt'}
KIT_REQUIRED = ('shape_foundation/models/gaot_backbone.py', 'step_utils.py', 'checkpoints/checkpoint_final.pt')
EXECUTABLE = 'Start-Mac.command'
FIXED_TIME = (2026, 1, 1, 0, 0, 0)
TOP_DIR = 'local-model-search/'  # 应用包顶层目录：解压后得到整洁的单一文件夹


def excluded(path):
    # 根级目录（如 data/、dist/）整树排除；__pycache__ 等则不限层级。
    return (any(part in ('__pycache__', '.pytest_cache', 'node_modules', '.git') for part in path.parts)
            or (path.parts and path.parts[0] in ROOT_DIRS)
            or path.suffix.lower() in EXCLUDE_SUFFIXES
            or path.name in ('.DS_Store', 'config.local.json') or path.name.startswith('.env'))


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def report(path):
    print('%s  (%d KB)  sha256=%s' % (path, path.stat().st_size >> 10, sha256(path)), flush=True)


def source_files():
    """只遍历发布白名单目录，不进入环境、索引或构建输出。"""
    for name in sorted(SOURCE_FILES):
        path = ROOT / name
        if path.is_file() and not path.is_symlink():
            yield path
    for name in sorted(SOURCE_DIRS):
        root = ROOT / name
        if not root.is_dir() or root.is_symlink():
            continue
        for folder, dirs, files in os.walk(root, followlinks=False):
            parent = Path(folder)
            dirs[:] = sorted(d for d in dirs if not (parent / d).is_symlink()
                             and not excluded((parent / d).relative_to(ROOT)))
            for filename in sorted(files):
                path = parent / filename
                if not path.is_symlink() and not excluded(path.relative_to(ROOT)):
                    yield path


def build_app(version):
    target = DIST / ('local-model-search-v%s.zip' % version)
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for path in source_files():
            relative = path.relative_to(ROOT)
            if not path.is_file() or path.is_symlink() or excluded(relative):
                continue
            if relative.parts[0] not in SOURCE_DIRS and relative.as_posix() not in SOURCE_FILES:
                continue
            if relative.parts[0] == 'kit' and path.suffix != '.py' and path.name != 'LICENSE':
                continue
            arcname = TOP_DIR + relative.as_posix()
            if relative.as_posix() == EXECUTABLE:
                # 为 macOS 记录可执行位（Windows 上打包时文件系统不携带该信息）。
                info = zipfile.ZipInfo(arcname, FIXED_TIME)
                info.external_attr = 0o100755 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                bundle.writestr(info, path.read_bytes())
            else:
                bundle.write(path, arcname)
            print('  应用包 + ' + arcname, flush=True)
    return target


def kit_entries(source):
    """枚举权重包必需文件：shape_foundation/** + step_utils.py + checkpoints/checkpoint_final.pt。"""
    root = source / 'shape_foundation'
    for path in sorted(root.rglob('*')) if root.is_dir() else []:
        if path.is_file() and not path.is_symlink() and path.suffix == '.py' and not excluded(path.relative_to(source)):
            yield path, path.relative_to(source)
    for name in ('step_utils.py', 'checkpoints/checkpoint_final.pt'):
        path = source / name
        if path.is_file():
            yield path, Path(name)


def build_kit(source):
    target = DIST / 'shape-kit.zip'
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        bundle.write(ROOT / 'kit/LICENSE', 'kit/LICENSE')
        bundle.write(ROOT / 'NOTICE.md', 'kit/NOTICE.md')
        bundle.write(ROOT / 'packaging/shape-manifest.json', 'kit/shape-manifest.json')
        for path, relative in kit_entries(source):
            arcname = 'kit/' + relative.as_posix()
            if path.suffix == '.pt':
                # 权重文件自身已压缩，直接存储以节省打包时间。
                info = zipfile.ZipInfo(arcname, FIXED_TIME)
                info.compress_type = zipfile.ZIP_STORED
                with path.open('rb') as stream, bundle.open(info, 'w') as writer:
                    shutil.copyfileobj(stream, writer, 1024 * 1024)
            else:
                bundle.write(path, arcname)
            print('  权重包 + ' + arcname, flush=True)
    return target


def main():
    parser = argparse.ArgumentParser(description='构建发布资产（dist/）')
    parser.add_argument('--version', default='1.0.0', help='应用包版本号')
    parser.add_argument('--kit-source', help='MFCAD_shape 源目录（构建 shape-kit.zip 用）')
    parser.add_argument('--app-only', action='store_true', help='只构建应用包')
    parser.add_argument('--kit-only', action='store_true', help='只构建权重包')
    args = parser.parse_args()

    DIST.mkdir(exist_ok=True)
    if not args.kit_only:
        report(build_app(args.version))
    if not args.app_only:
        if not args.kit_source:
            raise SystemExit('构建权重包需要 --kit-source（MFCAD_shape 目录）。')
        source = Path(args.kit_source).expanduser().resolve()
        missing = [item for item in KIT_REQUIRED if not (source / item).is_file()]
        if missing:
            raise SystemExit('源目录缺少必要文件：%s（%s）' % (', '.join(missing), source))
        manifest = json.loads((ROOT / 'packaging/shape-manifest.json').read_text(encoding='utf-8'))
        if sha256(source / 'checkpoints/checkpoint_final.pt') != manifest['checkpoint_sha256']:
            raise SystemExit('权重不是已核实许可的版本，停止发布。')
        report(build_kit(source))
    return 0


if __name__ == '__main__':
    sys.exit(main())
