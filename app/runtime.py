"""源码和独立应用共用的资源定位、用户配置与显式数据迁移。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

APP_NAME = 'LocalModelSearch'


def resource_root():
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parents[1]


def user_data_dir():
    override = os.environ.get('LOCAL_MODEL_SEARCH_DATA_DIR')
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == 'win32':
        base = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData/Local')
    elif sys.platform == 'darwin':
        base = Path.home() / 'Library/Application Support'
    else:
        base = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share')
    return base / APP_NAME


def read_config(path):
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('配置文件必须是 JSON 对象：%s' % path)
    return value


def initialize_config(defaults):
    root = user_data_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'config.json'
    config = dict(defaults)
    if not path.exists():
        from storage import Store, atomic_json, SearchError
        config.update(read_config(resource_root() / 'config.json'))
        # 首次配置只创建一次；同时打开两个实例时不覆盖另一个实例。
        for attempt in range(100):
            try:
                with Store(root).writing():
                    if not path.exists():
                        atomic_json(path, config)
                break
            except SearchError:
                if attempt == 99:
                    raise
                time.sleep(.05)
    config.update(read_config(path))
    return config


def open_folder(path):
    path = str(Path(path).resolve())
    if sys.platform == 'win32':
        os.startfile(path)
    elif sys.platform == 'darwin':
        subprocess.Popen(['/usr/bin/open', path])
    else:
        subprocess.Popen(['xdg-open', path])


def import_legacy_data(source, destination=None):
    """仅由用户明确触发：复制索引到空目标，拒绝覆盖、活动写锁及符号链接。"""
    source = Path(source).expanduser().resolve()
    destination = Path(destination or user_data_dir()).resolve()
    registry = source / 'registry.json'
    if not registry.is_file():
        raise ValueError('请选择旧版本包含 registry.json 的 data 文件夹。')
    if source == destination or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError('新旧数据目录不能相同或相互包含。')
    destination.mkdir(parents=True, exist_ok=True)
    if (destination / 'registry.json').exists() or (destination / 'libraries').exists():
        raise ValueError('当前用户已有检索库，为避免覆盖，请保留旧目录并在新版本中重新建库。')
    # 使用存储层相同的跨进程锁；绝不修改旧注册表或源模型。
    from storage import Store
    with Store(source).writing(), Store(destination).writing():
        if (destination / 'registry.json').exists() or (destination / 'libraries').exists():
            raise ValueError('目标已有数据，已取消迁移。')
        meta = read_config(registry)
        if not isinstance(meta.get('libraries'), list):
            raise ValueError('旧检索库注册表无效。')
        libraries = source / 'libraries'
        if libraries.is_dir() and any(p.is_symlink() for p in libraries.rglob('*')):
            raise ValueError('旧数据含符号链接，请重新建库。')
        if libraries.is_symlink():
            raise ValueError('旧数据含符号链接，请重新建库。')
        # 先暂存；复制失败不写入注册表，原有数据保持不变。
        staging = destination / ('migration-' + uuid.uuid4().hex)
        staging.mkdir()
        moved_libraries = False
        try:
            if libraries.is_dir():
                shutil.copytree(libraries, staging / 'libraries')
            shutil.copy2(registry, staging / 'registry.json')
            if (staging / 'libraries').exists():
                (staging / 'libraries').replace(destination / 'libraries')
                moved_libraries = True
            try:
                (staging / 'registry.json').replace(destination / 'registry.json')
            except BaseException:
                if moved_libraries:
                    (destination / 'libraries').replace(staging / 'libraries')
                raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    return destination
