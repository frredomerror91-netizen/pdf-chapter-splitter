$scriptPath = Join-Path $PSScriptRoot 'split_pdf_by_chapter.py'
$bundledPython = 'C:\Users\21058\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'

if (Test-Path -LiteralPath $bundledPython) {
    & $bundledPython $scriptPath
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python $scriptPath
} else {
    Write-Host '未找到 Python。请先安装 Python 3.10 或更高版本，并执行 pip install -r requirements.txt。' -ForegroundColor Red
    Read-Host '按回车键退出'
    exit 1
}

if ($LASTEXITCODE -ne 0) {
    Read-Host '程序退出时出现错误，按回车键关闭窗口'
}
