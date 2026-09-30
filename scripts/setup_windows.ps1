# 一键启动（Windows）：探测 Python 3.11–3.13，必要时通过 winget 安装，然后交由 scripts/setup.py 处理。
$ErrorActionPreference = 'Continue'

function Test-PythonVersion {
    param([string]$Exe, [string[]]$Prefix)
    if (-not (Get-Command $Exe -ErrorAction SilentlyContinue)) { return $false }
    $check = 'import sys; raise SystemExit(0 if (3, 11) <= sys.version_info < (3, 14) else 1)'
    & $Exe @Prefix -c $check 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

$python = $null
foreach ($version in '3.11', '3.12', '3.13') {
    if (Test-PythonVersion -Exe 'py' -Prefix @("-$version")) { $python = @('py', "-$version"); break }
}
if (-not $python -and (Test-PythonVersion -Exe 'python' -Prefix @())) { $python = @('python') }

if (-not $python) {
    Write-Host '未检测到 Python 3.11–3.13，尝试通过 winget 安装 Python 3.11 …'
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install -e --id Python.Python.3.11 --scope user --accept-package-agreements --accept-source-agreements
        if (Test-PythonVersion -Exe 'py' -Prefix @('-3.11')) { $python = @('py', '-3.11') }
    }
}

if (-not $python) {
    Write-Host ''
    Write-Host '未找到可用的 Python（需要 3.11 – 3.13）。' -ForegroundColor Red
    Write-Host '请先安装 Python 3.11 后重试：https://www.python.org/downloads/'
    Read-Host '按回车键退出'
    exit 1
}

$setup = Join-Path $PSScriptRoot 'setup.py'
$arguments = @($python | Select-Object -Skip 1) + @($setup)
& $python[0] @arguments
exit $LASTEXITCODE
