"""下载或复制 Shape 权重包到 ./kit（shape_foundation/、step_utils.py、checkpoints/）。

用法：
    python scripts/fetch_kit.py                  # 从 GitHub Release 下载（约 120 MB，支持断点续传）
    python scripts/fetch_kit.py --from <目录>    # 从本地已解压的 MFCAD_shape 目录复制
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / 'kit'
DEFAULT_URL = 'https://github.com/pipi123456798/local-model-search/releases/download/v1.1.0/shape-kit.zip'
REQUIRED = ('shape_foundation/models/gaot_backbone.py', 'step_utils.py', 'checkpoints/checkpoint_final.pt')


def checkpoint_valid(path):
    manifest = json.loads((ROOT / 'packaging/shape-manifest.json').read_text(encoding='utf-8'))
    if not path.is_file() or path.stat().st_size != manifest['checkpoint_bytes']:
        return False
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest() == manifest['checkpoint_sha256']


def kit_ready(kit=KIT):
    return (all((kit / item).is_file() for item in REQUIRED)
            and checkpoint_valid(kit / 'checkpoints/checkpoint_final.pt'))


def progress(done, total):
    if total:
        return '已下载 %d / %d MB（%.0f%%）' % (done >> 20, total >> 20, 100 * done / total)
    return '已下载 %d MB' % (done >> 20)


def download(url, target):
    resume = target.stat().st_size if target.is_file() else 0
    request = urllib.request.Request(url, headers={'User-Agent': 'local-model-search-kit'})
    if resume:
        request.add_header('Range', 'bytes=%d-' % resume)
    with urllib.request.urlopen(request, timeout=60) as response:
        if resume and response.status != 206:
            resume = 0  # 服务器不支持断点续传，从头下载。
        total = (response.length or 0) + resume
        with target.open('ab' if resume else 'wb') as stream:
            done, last = resume, time.monotonic()
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                stream.write(block)
                done += len(block)
                if time.monotonic() - last > 2:
                    print('  ' + progress(done, total), flush=True)
                    last = time.monotonic()
    print('  ' + progress(target.stat().st_size, total), flush=True)


def fetch(url, target, attempts=3):
    for attempt in range(1, attempts + 1):
        try:
            download(url, target)
            return
        except OSError as exc:
            print('下载中断（%s），第 %d/%d 次重试…' % (exc, attempt, attempts), flush=True)
            time.sleep(2 * attempt)
    raise SystemExit('权重包下载失败。请检查网络后重试，'
                     '或手动下载 shape-kit.zip 后用 --archive 指定本地 ZIP。')


def extract(archive):
    print('正在校验权重包…', flush=True)
    target = KIT / 'checkpoints/checkpoint_final.pt'
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix('.pt.part')
    expected = json.loads((ROOT / 'packaging/shape-manifest.json').read_text(encoding='utf-8'))
    try:
        with zipfile.ZipFile(archive) as bundle:
            member = bundle.getinfo('kit/checkpoints/checkpoint_final.pt')
            if member.file_size != expected['checkpoint_bytes']:
                raise ValueError('权重大小不匹配')
            with bundle.open(member) as src, temp.open('wb') as dst:
                shutil.copyfileobj(src, dst)
        if not checkpoint_valid(temp):
            raise ValueError('权重 SHA-256 校验失败，未安装')
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    # 仅安装经过验证的权重，不执行也不覆盖下载包中的 Python 源码。
    print('权重校验通过：%s' % target, flush=True)


def copy_kit(source, destination):
    """只复制运行必需的文件：shape_foundation/** + step_utils.py + checkpoints/checkpoint_final.pt。"""
    for entry in (source / 'shape_foundation', source / 'step_utils.py',
                  source / 'checkpoints' / 'checkpoint_final.pt'):
        target = destination / entry.relative_to(source)
        if entry.is_dir():
            shutil.copytree(entry, target, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo'))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry, target)


def main():
    parser = argparse.ArgumentParser(description='准备 Shape 权重包（./kit）')
    parser.add_argument('--url', default=os.environ.get('KIT_URL', DEFAULT_URL), help='权重包下载地址')
    parser.add_argument('--from', dest='source', help='从本地目录复制，替代下载')
    parser.add_argument('--force', action='store_true', help='已存在时也重新准备')
    parser.add_argument('--archive', type=Path, help='使用本地 shape-kit.zip（CI/离线准备）')
    args = parser.parse_args()
    if args.archive and args.source:
        parser.error('--archive 与 --from 不能同时使用')
    if args.archive and not args.archive.is_file():
        parser.error('指定的本地权重包不存在：%s' % args.archive)

    if kit_ready() and not args.force and not args.archive:
        print('权重包已就绪：%s' % KIT, flush=True)
        return 0
    if args.source:
        source = Path(args.source).expanduser().resolve()
        if not kit_ready(source):
            raise SystemExit('源目录缺少必要文件（需包含 shape_foundation/、step_utils.py、'
                             'checkpoints/checkpoint_final.pt）：%s' % source)
        print('正在复制权重包：%s → %s' % (source, KIT), flush=True)
        copy_kit(source, KIT)
    else:
        local = args.archive or ROOT / 'shape-kit.zip'
        if local.is_file():
            extract(local)
        else:
            archive = ROOT / 'shape-kit.zip.part'
            print('正在下载权重包（约 120 MB）：%s' % args.url, flush=True)
            fetch(args.url, archive)
            extract(archive)
            archive.unlink(missing_ok=True)
    if not kit_ready():
        raise SystemExit('权重包准备失败：%s 中缺少必要文件。' % KIT)
    print('权重包已就绪：%s' % KIT, flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
