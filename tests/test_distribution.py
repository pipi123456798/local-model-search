"""不使用个人模型或网络的分发回归测试。"""
import importlib.util
import json
import os
from pathlib import Path
import sys
from unittest.mock import Mock
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT / 'app/model_search')]
import launcher
import runtime
from storage import Store
from server import create_app


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCAL_MODEL_SEARCH_DATA_DIR', str(tmp_path / '数据 user'))


def test_config_survives_new_defaults(tmp_path, monkeypatch):
    resources = tmp_path / 'resources'
    resources.mkdir()
    (resources / 'config.json').write_text('{"device":"cpu"}', encoding='utf-8')
    monkeypatch.setattr(runtime, 'resource_root', lambda: resources)
    assert runtime.initialize_config({'port': 0})['device'] == 'cpu'
    config = runtime.user_data_dir() / 'config.json'
    config.write_text('{"port":12345,"cad_exe":"custom"}', encoding='utf-8')
    assert runtime.initialize_config({'port': 0})['port'] == 12345
    assert runtime.initialize_config({'port': 0})['cad_exe'] == 'custom'
    assert not (resources / 'data').exists()


def test_frozen_resource_root(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path), raising=False)
    assert runtime.resource_root() == tmp_path


def test_service_uses_executable_mode_and_writable_cwd(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    spawn = Mock()
    monkeypatch.setattr(launcher.subprocess, 'Popen', spawn)
    launcher.start_service(tmp_path, tmp_path / 'session', {})
    args, kwargs = spawn.call_args
    assert args[0][:2] == [sys.executable, '--service']
    assert not any(arg.endswith('.py') for arg in args[0])
    assert kwargs['cwd'] == str(tmp_path)


def test_session_is_outside_resources():
    data, session, kit, checkpoint = launcher.prepare_session({})
    assert data == runtime.user_data_dir()
    assert session.is_relative_to(data)
    assert checkpoint == kit / 'checkpoints/checkpoint_final.pt'


def legacy(tmp_path):
    source = tmp_path / '旧版本 data'
    source.mkdir()
    (source / 'registry.json').write_text('{"libraries":[]}', encoding='utf-8')
    (source / 'libraries').mkdir()
    (source / 'libraries/keep.txt').write_text('index', encoding='utf-8')
    (source / 'sessions').mkdir()
    return source


def test_import_copies_only_index_and_preserves_source(tmp_path):
    source = legacy(tmp_path)
    target = runtime.import_legacy_data(source)
    assert (source / 'libraries/keep.txt').read_text() == 'index'
    assert (target / 'libraries/keep.txt').read_text() == 'index'
    assert not (target / 'sessions').exists()
    with pytest.raises(ValueError, match='已有'):
        runtime.import_legacy_data(source)


@pytest.mark.parametrize('phase', ['copy', 'commit'])
def test_import_failure_rolls_back_and_can_retry(tmp_path, monkeypatch, phase):
    source = legacy(tmp_path)
    target = runtime.user_data_dir()
    with monkeypatch.context() as patch:
        if phase == 'copy':
            original = runtime.shutil.copy2

            def fail_copy(src, dst, *args, **kwargs):
                if Path(src).name == 'registry.json':
                    raise OSError('模拟复制失败')
                return original(src, dst, *args, **kwargs)

            patch.setattr(runtime.shutil, 'copy2', fail_copy)
        else:
            original = Path.replace

            def fail_replace(path, destination):
                if Path(destination) == target / 'registry.json':
                    raise OSError('模拟提交失败')
                return original(path, destination)

            patch.setattr(Path, 'replace', fail_replace)
        with pytest.raises(OSError, match='模拟'):
            runtime.import_legacy_data(source)
    assert not (target / 'registry.json').exists()
    assert not (target / 'libraries').exists()
    assert runtime.import_legacy_data(source) == target


def test_explicit_missing_archive_never_downloads(tmp_path, monkeypatch):
    fetch = load_script('fetch_missing_test', 'scripts/fetch_kit.py')
    download = Mock()
    monkeypatch.setattr(fetch, 'fetch', download)
    monkeypatch.setattr(sys, 'argv', ['fetch_kit.py', '--archive', str(tmp_path / 'missing.zip')])
    with pytest.raises(SystemExit):
        fetch.main()
    download.assert_not_called()


def test_config_creation_is_atomic(tmp_path, monkeypatch):
    import storage
    target = runtime.user_data_dir() / 'config.json'
    with monkeypatch.context() as patch:
        patch.setattr(storage, 'atomic_json', Mock(side_effect=OSError('模拟写入失败')))
        with pytest.raises(OSError):
            runtime.initialize_config({'port': 0})
    assert not target.exists()
    assert runtime.initialize_config({'port': 0})['port'] == 0


def test_import_rejects_running_writer(tmp_path):
    source = legacy(tmp_path)
    with Store(source).writing():
        with pytest.raises(Exception, match='其他|另一个'):
            runtime.import_legacy_data(source)
    assert not (runtime.user_data_dir() / 'registry.json').exists()


def test_download_does_not_extract_unverified_code(tmp_path, monkeypatch):
    fetch = load_script('fetch_kit_test', 'scripts/fetch_kit.py')
    monkeypatch.setattr(fetch, 'KIT', tmp_path / 'kit')
    archive = tmp_path / 'bad.zip'
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr('kit/checkpoints/checkpoint_final.pt', b'bad')
        bundle.writestr('../overwrite.py', b'bad')
        bundle.writestr('kit/step_utils.py', b'bad')
    with pytest.raises(ValueError, match='大小'):
        fetch.extract(archive)
    assert not (tmp_path / 'overwrite.py').exists()
    assert not (tmp_path / 'kit/step_utils.py').exists()
    assert not (tmp_path / 'kit/checkpoints/checkpoint_final.pt').exists()


def test_distribution_excludes_private_data():
    release = load_script('release_test', 'packaging/make_release.py')
    for path in ('.venv/a.py', 'data/registry.json', 'build/report.json', '.tools/a.exe',
                 'kit/checkpoints/checkpoint_final.pt', 'app/__pycache__/a.pyc', 'app/debug.log',
                 'app/node_modules/a.js', 'docs/.git/config', 'app/.env.local',
                 'app/config.local.json', 'kit/private.NPY'):
        assert release.excluded(Path(path)), path
    assert not release.excluded(Path('kit/shape_foundation/models/gaot_backbone.py'))


def test_git_keeps_required_shape_data_sources():
    import shutil
    import subprocess
    if not (ROOT / '.git').exists() or not shutil.which('git'):
        pytest.skip('源码归档或无 Git 环境不执行仓库忽略规则测试')
    paths = ['kit/data_placeholder.py', 'kit/shape_foundation/data/__init__.py',
             'kit/shape_foundation/data/preprocessing.py', 'kit/shape_foundation/data/sampling.py']
    result = subprocess.run(['git', '-C', str(ROOT), 'check-ignore', '--no-index', '--', *paths],
                            text=True, capture_output=True)
    assert result.returncode == 1 and not result.stdout, result.stdout + result.stderr
    for path in paths[1:]:
        assert (ROOT / path).is_file()


def test_source_archive_uses_allowlist(tmp_path, monkeypatch):
    release = load_script('release_allowlist_test', 'packaging/make_release.py')
    monkeypatch.setattr(release, 'ROOT', tmp_path)
    monkeypatch.setattr(release, 'DIST', tmp_path / 'dist')
    (tmp_path / 'dist').mkdir()
    included = ['app/main.py', 'kit/LICENSE', 'kit/shape_foundation/model.py',
                'packaging/build_app.py', 'Start-Mac.command']
    excluded = ['build/venv/private.py', 'data/registry.json', 'extra.py',
                'app/.env.local', 'app/node_modules/private.js', 'docs/.git/config',
                'kit/private.txt', 'kit/checkpoints/checkpoint_final.pt']
    for name in included + excluded:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('fixture', encoding='utf-8')
    with zipfile.ZipFile(release.build_app('1.1.0')) as archive:
        assert set(archive.namelist()) == {release.TOP_DIR + name for name in included}
        assert (archive.getinfo(release.TOP_DIR + 'Start-Mac.command').external_attr >> 16) & 0o111


@pytest.mark.parametrize('mode', ['global', 'user-site', 'external', 'vtk'])
def test_build_rejects_nonisolated_environment(tmp_path, monkeypatch, mode):
    build = load_script('build_environment_test', 'packaging/build_app.py')
    monkeypatch.setattr(sys, 'prefix', str(tmp_path))
    monkeypatch.setattr(sys, 'base_prefix', str(tmp_path if mode == 'global' else tmp_path / 'base'))
    monkeypatch.setattr(build.site, 'ENABLE_USER_SITE', mode == 'user-site')
    monkeypatch.setattr(sys, 'path', [str(tmp_path.parent / 'site-packages')] if mode == 'external' else [])
    if mode == 'vtk':
        monkeypatch.setattr(build, 'distribution', Mock(return_value=object()))
    with pytest.raises(RuntimeError, match='venv|site-packages|非发布依赖'):
        build.verify_build_environment()


@pytest.mark.parametrize('download', [False, True])
def test_inno_rejects_unverified_installer(tmp_path, monkeypatch, download):
    import io
    prepare = load_script('prepare_inno_test', 'packaging/prepare_inno.py')
    monkeypatch.setattr(prepare, 'ROOT', tmp_path)
    monkeypatch.setattr(sys, 'platform', 'win32')
    installer = tmp_path / 'untrusted.exe'
    installer.write_bytes(b'untrusted')
    args = ['prepare_inno.py'] if download else ['prepare_inno.py', '--archive', str(installer)]
    monkeypatch.setattr(sys, 'argv', args)
    execute = Mock()
    opener = Mock()
    opener.open.return_value = io.BytesIO(b'untrusted download')
    monkeypatch.setattr(prepare.urllib.request, 'build_opener', Mock(return_value=opener))
    monkeypatch.setattr(prepare.subprocess, 'run', execute)
    with pytest.raises(RuntimeError, match='SHA-256'):
        prepare.main()
    execute.assert_not_called()
    assert not (tmp_path / '.tools' / ('innosetup-' + prepare.VERSION + '.exe')).exists()


def test_shape_runtime_modules_import(monkeypatch):
    import importlib
    import pkgutil
    monkeypatch.syspath_prepend(str(ROOT / 'kit'))
    import shape_foundation
    for module in pkgutil.walk_packages(shape_foundation.__path__, 'shape_foundation.'):
        importlib.import_module(module.name)


def test_health_reports_missing_yaml(tmp_path, monkeypatch):
    import inference
    original = inference.importlib.util.find_spec
    monkeypatch.setattr(inference.importlib.util, 'find_spec',
                        lambda name: None if name == 'yaml' else original(name))
    health = inference.ShapeInference({'kit': str(ROOT / 'kit'),
                                       'checkpoint': str(tmp_path / 'checkpoint.pt')}).health()
    assert not health['ready']
    assert any('yaml' in error for error in health['errors'])


def test_release_python_sources_parse():
    import ast
    release = load_script('release_parse_test', 'packaging/make_release.py')
    for path in release.source_files():
        if path.suffix == '.py':
            ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))


@pytest.mark.parametrize('case', ['valid', 'bad-hash', 'unsafe-name'])
def test_decode_upstream_verifies_blob(tmp_path, monkeypatch, case):
    import base64
    import hashlib
    build = load_script('upstream_decode_test', 'packaging/build_app.py')
    monkeypatch.setattr(build, 'BUILD', tmp_path)
    data = b'license fixture\n'
    digest = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
    entry = {'type': 'file', 'encoding': 'base64', 'content': base64.b64encode(data).decode(),
             'sha': digest, 'size': len(data), 'name': 'LICENSE'}
    if case == 'bad-hash':
        entry['sha'] = '0' * 40
    elif case == 'unsafe-name':
        entry['name'] = '../LICENSE'
    cache = tmp_path / 'input.json'
    cache.write_text(json.dumps(entry), encoding='utf-8')
    if case == 'valid':
        assert build.decode_upstream_file(cache).read_bytes() == data
    else:
        with pytest.raises(ValueError):
            build.decode_upstream_file(cache)
        assert not (tmp_path / 'upstream').exists()


def test_smoke_does_not_accept_stale_report(tmp_path, monkeypatch):
    build = load_script('smoke_stale_test', 'packaging/build_app.py')
    monkeypatch.setattr(build, 'BUILD', tmp_path)
    monkeypatch.setattr(build.subprocess, 'run', Mock())
    report = tmp_path / 'report.json'
    report.write_text('{"ok":true,"checks":[]}', encoding='utf-8')
    with pytest.raises(RuntimeError, match='未通过'):
        build.smoke(tmp_path / 'app.exe', report.name)


@pytest.mark.parametrize('fails', [False, True])
def test_dmg_detaches_after_checks(tmp_path, monkeypatch, fails):
    build = load_script('dmg_cleanup_test', 'packaging/build_app.py')
    monkeypatch.setattr(build, 'BUILD', tmp_path)
    calls = Mock()
    monkeypatch.setattr(build.subprocess, 'run', calls)
    monkeypatch.setattr(Path, 'is_symlink', lambda path: path.name == 'Applications')
    monkeypatch.setattr(Path, 'readlink', lambda path: Path('/Applications'))
    check = Mock(side_effect=RuntimeError('模拟产物失败') if fails else None)
    monkeypatch.setattr(build, 'smoke', check)
    if fails:
        with pytest.raises(RuntimeError, match='模拟'):
            build.verify_dmg(tmp_path / 'fixture.dmg')
    else:
        build.verify_dmg(tmp_path / 'fixture.dmg')
    assert calls.call_args_list[0].args[0][:4] == ['hdiutil', 'attach', '-readonly', '-nobrowse']
    assert calls.call_args_list[-1].args[0][:2] == ['hdiutil', 'detach']
    assert check.call_args.args[1] == 'smoke-DMG.json'


def test_native_actions_require_session_token(tmp_path):
    engine = Mock()
    app = create_app(tmp_path / 'data', tmp_path / 'session', {}, engine)
    try:
        client = app.test_client()
        for route in ('data-folder', 'import-legacy'):
            assert client.post('/api/standalone/' + route, json={}).status_code == 401
    finally:
        app.extensions['search_tasks'].stop()
