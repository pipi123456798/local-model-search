"""校验并准备项目内的 Inno Setup 编译器，不修改系统 PATH。"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
VERSION = '6.7.3'
URL = 'https://github.com/jrsoftware/issrc/releases/download/is-6_7_3/innosetup-6.7.3.exe'
# 来源：microsoft/winget-pkgs 的 JRSoftware.InnoSetup 6.7.3 安装清单。
SHA256 = '9c73c3bae7ed48d44112a0f48e66742c00090bdb5bef71d9d3c056c66e97b732'


def main():
    parser = argparse.ArgumentParser(description='准备固定版本的 Inno Setup 6')
    parser.add_argument('--archive', type=Path, help='使用已下载的官方安装程序')
    parser.add_argument('--no-proxy', action='store_true', help='仅本次下载直连，不改变系统代理')
    args = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('此工具仅用于 Windows 原生构建')
    tools = ROOT / '.tools'
    tools.mkdir(exist_ok=True)
    archive = args.archive or tools / ('innosetup-' + VERSION + '.exe')
    if args.archive and not archive.is_file():
        parser.error('指定的安装程序不存在')
    if not archive.is_file():
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if args.no_proxy else urllib.request.build_opener()
        temp = archive.with_suffix('.exe.part')
        with opener.open(URL, timeout=60) as response, temp.open('wb') as output:
            shutil.copyfileobj(response, output)
        with temp.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != SHA256:
                raise RuntimeError('安装程序 SHA-256 不符，未执行；请重新取得官方文件')
        os.replace(temp, archive)
    with archive.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != SHA256:
            raise RuntimeError('安装程序 SHA-256 不符，未执行')
    destination = tools / 'inno'
    subprocess.run([str(archive.resolve()), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
                    '/CURRENTUSER', '/NOICONS', '/TASKS=', '/DIR=' + str(destination)],
                   check=True, timeout=180)
    compiler = destination / 'ISCC.exe'
    if not compiler.is_file():
        raise RuntimeError('安装完成但未找到 ISCC.exe')
    print('编译器已准备：' + str(compiler))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
