"""构建自带运行环境的应用、验收产物，再生成平台安装包。"""
from __future__ import annotations

import argparse
import base64
import hashlib
from importlib.metadata import distribution, PackageNotFoundError
import json
import os
from pathlib import Path
import platform
import re
import shutil
import site
import subprocess
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / 'build'
DIST = ROOT / 'dist'
NATIVE = DIST / 'native'


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify_kit():
    manifest = json.loads((ROOT / 'packaging/shape-manifest.json').read_text(encoding='utf-8'))
    path = ROOT / 'kit/checkpoints/checkpoint_final.pt'
    if not path.is_file() or path.stat().st_size != manifest['checkpoint_bytes'] or sha256(path) != manifest['checkpoint_sha256']:
        raise RuntimeError('缺少已校验权重：请先准备 kit/checkpoints/checkpoint_final.pt，不能使用未知权重打包。')
    license_data = (ROOT / 'kit/LICENSE').read_bytes().replace(b'\r\n', b'\n')
    blob = hashlib.sha1(b'blob ' + str(len(license_data)).encode() + b'\0' + license_data).hexdigest()
    if blob != manifest['license_git_blob']:
        raise RuntimeError('Shape 许可证与已核实的上游原文不一致。')


def audit_shape_sources(upstream_tree=None):
    """比对固定上游提交的 Git blob，记录已有本地修改，供发布归属核查。"""
    manifest = json.loads((ROOT / 'packaging/shape-manifest.json').read_text(encoding='utf-8'))
    url = 'https://api.github.com/repos/simd-ai/shape/git/trees/' + manifest['source_revision'] + '?recursive=1'
    request = urllib.request.Request(url, headers={'User-Agent': 'LocalModelSearch-release-audit'})
    if upstream_tree:
        tree = json.loads(upstream_tree.read_text(encoding='utf-8'))
    else:
        with urllib.request.urlopen(request, timeout=60) as response:
            tree = json.load(response)
    if tree.get('sha') != manifest['source_revision']:
        raise RuntimeError('上游源码清单不是指定提交')
    if tree.get('truncated'):
        raise RuntimeError('上游源码清单不完整，不能完成核验')
    upstream = {entry['path']: entry['sha'] for entry in tree['tree'] if entry['type'] == 'blob'}
    results = []
    for path in sorted((ROOT / 'kit/shape_foundation').rglob('*.py')):
        data = path.read_bytes().replace(b'\r\n', b'\n')
        digest = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        relative = path.relative_to(ROOT / 'kit').as_posix()
        expected = upstream.get(relative)
        state = 'identical' if digest == expected else ('modified' if expected else 'local')
        results.append({'path': relative, 'status': state, 'local_blob': digest, 'upstream_blob': expected})
    report = BUILD / 'shape-source-audit.json'
    report.write_text(json.dumps(results, indent=2), encoding='utf-8')
    for result in results:
        print(result['status'] + ' ' + result['path'])
    return report


def decode_upstream_file(cache):
    """从已取得的 GitHub 文件响应恢复原文并核验 blob，不执行上游代码。"""
    entry = json.loads(cache.read_text(encoding='utf-8'))
    if entry.get('type') != 'file' or entry.get('encoding') != 'base64':
        raise ValueError('上游响应不是完整的 base64 文件')
    data = base64.b64decode(''.join(entry['content'].split()), validate=True)
    digest = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
    if digest != entry.get('sha') or len(data) != entry.get('size'):
        raise ValueError('上游文件 blob 或长度不符')
    name = entry['name']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', name) or name in ('.', '..'):
        raise ValueError('上游文件名不安全')
    folder = BUILD / 'upstream' / digest
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / name
    output.write_bytes(data)
    print(str(output), flush=True)
    return output


def verify_build_environment():
    """拒绝继承全局包、版本漂移及 OCP 同名模块互相覆盖的环境。"""
    prefix = Path(sys.prefix).resolve()
    if sys.prefix == sys.base_prefix or site.ENABLE_USER_SITE:
        raise RuntimeError('发布构建必须使用未启用 system-site-packages 的独立 venv。')
    for entry in sys.path:
        path = Path(entry).resolve()
        if path.name in ('site-packages', 'dist-packages') and not path.is_relative_to(prefix):
            raise RuntimeError('构建路径混入了外部 site-packages，请重建干净 venv。')
    for name in ('cadquery-ocp', 'vtk', 'numba', 'llvmlite'):
        try:
            distribution(name)
        except PackageNotFoundError:
            continue
        raise RuntimeError('构建环境混入非发布依赖：' + name)
    from packaging.requirements import Requirement
    for line in (ROOT / 'packaging/requirements-build.txt').read_text(encoding='utf-8').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        req = Requirement(line)
        if req.marker is not None and not req.marker.evaluate():
            continue
        dist = distribution(req.name)
        if dist.version not in req.specifier or not Path(dist.locate_file('')).resolve().is_relative_to(prefix):
            raise RuntimeError('依赖版本或安装位置不符合固定清单：' + req.name)
    dist = distribution('cadquery-ocp-novtk')
    files = [item for item in dist.files or [] if str(item).replace('\\', '/') == 'OCP/__init__.py'
             or (item.parts[0] == 'OCP' and item.suffix in ('.pyd', '.so'))]
    if len(files) < 2:
        raise RuntimeError('OCP wheel 缺少可核验的入口或原生库。')
    for item in files:
        path = Path(dist.locate_file(item))
        if not item.hash or item.hash.mode != 'sha256' or not path.is_file():
            raise RuntimeError('OCP 安装记录不完整：' + str(item))
        digest = base64.urlsafe_b64encode(bytes.fromhex(sha256(path))).decode().rstrip('=')
        if digest != item.hash.value:
            raise RuntimeError('OCP 文件被其他安装覆盖：' + str(item))
    import OCP
    import torch
    if not Path(OCP.__file__).resolve().is_relative_to(prefix) or torch.version.cuda is not None:
        raise RuntimeError('发布需要独立 novtk 和 CPU PyTorch 环境。')
    subprocess.run([sys.executable, '-m', 'pip', 'check'], check=True)


def collect_licenses(target):
    """递归收集运行依赖的许可证、METADATA 和版本，不收集用户环境路径。"""
    from packaging.requirements import Requirement
    target.mkdir(parents=True, exist_ok=True)
    pending = ['Flask', 'numpy', 'scipy', 'torch', 'trimesh', 'einops', 'lxml',
               'Pillow', 'PyYAML', 'cadquery-ocp-novtk', 'pywebview', 'pyinstaller']
    found = {}
    while pending:
        name = pending.pop()
        dist = distribution(name)
        canonical = dist.metadata['Name']
        if canonical.lower() in found:
            continue
        found[canonical.lower()] = dist.version
        folder = target / canonical
        folder.mkdir(exist_ok=True)
        (folder / 'METADATA.txt').write_text(dist.read_text('METADATA') or '', encoding='utf-8')
        for file in dist.files or []:
            if '..' in file.parts or not any(word in file.name.lower() for word in ('license', 'copying', 'notice')):
                continue
            original = Path(dist.locate_file(file))
            if original.is_file():
                destination = folder / Path(file)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original, destination)
        for requirement in dist.requires or []:
            req = Requirement(requirement)
            if req.marker is None or req.marker.evaluate():
                pending.append(req.name)
    for candidate in (Path(sys.base_prefix) / 'LICENSE.txt', Path(sys.base_prefix) / 'Resources/LICENSE.txt'):
        if candidate.is_file():
            shutil.copy2(candidate, target / 'Python-LICENSE.txt')
            break
    versions = json.dumps(found, sort_keys=True, indent=2)
    (target / 'versions.json').write_text(versions, encoding='utf-8')
    (BUILD / 'dependency-versions.json').write_text(versions, encoding='utf-8')
    shutil.copy2(ROOT / 'kit/LICENSE', target / 'Shape-Apache-2.0.txt')
    shutil.copy2(ROOT / 'NOTICE.md', target / 'NOTICE.md')


def executable():
    if sys.platform == 'win32':
        return NATIVE / 'LocalModelSearch/LocalModelSearch.exe'
    return NATIVE / 'LocalModelSearch.app/Contents/MacOS/LocalModelSearch'


def freeze(version):
    verify_build_environment()
    verify_kit()
    config = BUILD / 'bundle/config.json'
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({'port': 0, 'device': 'cpu', 'kit': '', 'checkpoint': '',
                                 'open_browser': True, 'window': 'auto', 'cad_exe': ''}), encoding='utf-8')
    # 每次收集到新目录，避免上一轮构建的许可和依赖缓存混入产物。
    with tempfile.TemporaryDirectory(prefix='licenses-', dir=BUILD) as licenses:
        collect_licenses(Path(licenses))
        env = dict(os.environ, LMS_BUILD_VERSION=version, LMS_LICENSE_DIR=licenses)
        subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
                        '--distpath', str(NATIVE), '--workpath', str(BUILD / 'pyinstaller'),
                        str(ROOT / 'packaging/LocalModelSearch.spec')], cwd=ROOT, env=env, check=True)


def smoke(program=None, report_name=None):
    report = BUILD / (report_name or ('smoke-' + platform.system() + '.json'))
    report.write_text(json.dumps({'ok': False, 'error': '本次验收尚未完成'}), encoding='utf-8')
    env = dict(os.environ, LOCAL_MODEL_SEARCH_DATA_DIR=str(BUILD / 'smoke-user'))
    env.pop('PYTHONPATH', None)
    env.pop('PYTHONHOME', None)
    if sys.platform == 'win32':
        env['PATH'] = os.path.join(os.environ['SystemRoot'], 'System32') + os.pathsep + os.environ['SystemRoot']
    else:
        env['PATH'] = '/usr/bin:/bin:/usr/sbin:/sbin'
    subprocess.run([str(program or executable()), '--self-test', str(report)], cwd=BUILD, env=env,
                   check=True, timeout=1200)
    result = json.loads(report.read_text(encoding='utf-8'))
    if not result.get('ok'):
        raise RuntimeError('产物验收未通过：' + str(report))
    print('产物验收通过：' + ', '.join(result['checks']), flush=True)
    return report


def verify_dmg(asset):
    """验证只读挂载的 DMG 及其中应用；不冒充 Finder/Gatekeeper 图形验收。"""
    with tempfile.TemporaryDirectory(prefix='dmg-check-', dir=BUILD) as folder:
        mount = Path(folder) / 'volume'
        subprocess.run(['hdiutil', 'attach', '-readonly', '-nobrowse', '-mountpoint', str(mount),
                        str(asset)], check=True, timeout=180)
        try:
            app = mount / 'LocalModelSearch.app'
            applications = mount / 'Applications'
            if not applications.is_symlink() or applications.readlink() != Path('/Applications'):
                raise RuntimeError('DMG 缺少正确的 Applications 拖放入口')
            subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True)
            smoke(app / 'Contents/MacOS/LocalModelSearch', 'smoke-DMG.json')
        finally:
            subprocess.run(['hdiutil', 'detach', str(mount)], check=True, timeout=180)


def installers(version, iscc=None, zip_only=False):
    prefix = 'LocalModelSearch-' + version
    assets = []
    if sys.platform == 'win32':
        archive = DIST / (prefix + '-Windows-x64-Portable')
        shutil.make_archive(str(archive), 'zip', NATIVE, 'LocalModelSearch')
        assets.append(Path(str(archive) + '.zip'))
        if not zip_only:
            local_iscc = ROOT / '.tools/inno/ISCC.exe'
            iscc = iscc or (str(local_iscc) if local_iscc.is_file() else shutil.which('ISCC.exe'))
            for base in (os.environ.get('ProgramFiles(x86)', ''), os.environ.get('ProgramFiles', ''),
                         str(Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs')):
                candidate = Path(base) / 'Inno Setup 6/ISCC.exe'
                if not iscc and candidate.is_file():
                    iscc = str(candidate)
            if not iscc:
                raise RuntimeError('便携包已生成；生成安装程序还需 Inno Setup 6，请用 --iscc 指定 ISCC.exe。')
            subprocess.run([iscc, '/DAppVersion=' + version, '/DSourceDir=' + str(NATIVE / 'LocalModelSearch'),
                            '/DOutputDir=' + str(DIST), str(ROOT / 'packaging/windows.iss')], check=True)
            assets.append(DIST / (prefix + '-Windows-x64-Setup.exe'))
    else:
        bundle = NATIVE / 'LocalModelSearch.app'
        subprocess.run(['codesign', '--verify', '--deep', '--strict', str(bundle)], check=True)
        with tempfile.TemporaryDirectory(prefix='dmg-', dir=BUILD) as stage:
            stage = Path(stage)
            shutil.copytree(bundle, stage / bundle.name, symlinks=True)
            (stage / 'Applications').symlink_to('/Applications')
            asset = DIST / (prefix + '-macOS-arm64.dmg')
            subprocess.run(['hdiutil', 'create', '-ov', '-volname', 'Local Model Search', '-srcfolder',
                            str(stage), '-format', 'UDZO', str(asset)], check=True)
            verify_dmg(asset)
            assets.append(asset)
    sums = DIST / ('SHA256SUMS-' + platform.system() + '.txt')
    sums.write_text(''.join(sha256(asset) + '  ' + asset.name + '\n' for asset in assets), encoding='utf-8')
    return assets


def main():
    parser = argparse.ArgumentParser(description='构建免 Python 安装包')
    parser.add_argument('--version', default='1.1.0')
    parser.add_argument('--iscc', help='Inno Setup 编译器路径')
    parser.add_argument('--upstream-tree', type=Path, help='核查源码时使用缓存的上游 Git tree JSON')
    parser.add_argument('--upstream-file', type=Path, action='append', help='恢复原文时使用的 GitHub 文件 API JSON')
    parser.add_argument('--stage', choices=('all', 'freeze', 'test', 'package', 'audit-sources', 'decode-upstream'), default='all')
    parser.add_argument('--zip-only', action='store_true', help='Windows 仅生成便携包')
    args = parser.parse_args()
    if not re.fullmatch(r'\d+\.\d+\.\d+', args.version):
        parser.error('版本号必须为三个数字段，例如 1.1.0')
    if sys.version_info[:2] != (3, 11):
        parser.error('发布构建固定使用 Python 3.11')
    machine = platform.machine().lower()
    if not ((sys.platform == 'win32' and machine in ('amd64', 'x86_64')) or
            (sys.platform == 'darwin' and machine == 'arm64')):
        parser.error('仅在 Windows x64 或 macOS arm64 上原生构建')
    BUILD.mkdir(exist_ok=True)
    DIST.mkdir(exist_ok=True)
    if args.stage == 'decode-upstream':
        if not args.upstream_file:
            parser.error('decode-upstream 需要 --upstream-file')
        for cache in args.upstream_file:
            decode_upstream_file(cache)
    if args.stage == 'audit-sources':
        audit_shape_sources(args.upstream_tree)
    if args.stage in ('all', 'freeze'):
        freeze(args.version)
    if args.stage in ('all', 'test', 'package'):
        smoke()
    if args.stage in ('all', 'package'):
        for asset in installers(args.version, args.iscc, args.zip_only):
            print('已生成：' + str(asset), flush=True)


if __name__ == '__main__':
    main()
