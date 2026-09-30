"""先建立日志，再导入第三方依赖，供桌面端读取启动错误。"""
import json
import os
from pathlib import Path
import importlib
import sys
import traceback


def main():
    session = Path(sys.argv[sys.argv.index('--session-dir') + 1]).resolve()
    session.mkdir(parents=True, exist_ok=True)
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    os.environ.setdefault('OMP_NUM_THREADS', '4')
    sys.dont_write_bytecode = True
    with (session / 'launcher.log').open('a', encoding='utf-8', buffering=1) as log:
        sys.stdout = sys.stderr = log
        try:
            importlib.import_module('server').main()
        except Exception as exc:
            traceback.print_exc()
            temp = session / 'error.tmp'
            temp.write_text(json.dumps({'error': str(exc)}, ensure_ascii=False), encoding='utf-8')
            os.replace(temp, session / 'error.json')
            raise SystemExit(1)


if __name__ == '__main__':
    main()
