#!/bin/bash
# 一键启动（macOS，Apple Silicon）：探测 Python 3.11–3.13，然后交由 scripts/setup.py 处理。
cd "$(dirname "$0")" || exit 1

check() {
    "$1" -c 'import sys; raise SystemExit(0 if (3, 11) <= sys.version_info < (3, 14) else 1)' >/dev/null 2>&1
}

for candidate in python3.11 python3.12 python3.13; do
    if command -v "$candidate" >/dev/null 2>&1 && check "$candidate"; then
        exec "$candidate" scripts/setup.py
    fi
done
if command -v python3 >/dev/null 2>&1 && check python3; then
    exec python3 scripts/setup.py
fi

echo "未找到 Python 3.11 – 3.13。"
echo "请安装后重试，任选其一："
echo "  1. 官网下载：https://www.python.org/downloads/"
echo "  2. Homebrew：brew install python@3.11"
read -r -p "按回车键退出…" _ || true
exit 1
