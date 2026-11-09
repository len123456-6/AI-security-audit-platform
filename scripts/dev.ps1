# =============================================================================
#  本地开发启动脚本
#  用法：
#    .\scripts\dev.ps1                 # 8000 端口 + 热重载
#    .\scripts\dev.ps1 -Port 9000      # 换端口
#    .\scripts\dev.ps1 -NoReload       # 关热重载（调试断点更稳定）
# =============================================================================
[CmdletBinding()]
param(
    [int]$Port = 8000,
    [switch]$NoReload
)

$ErrorActionPreference = 'Stop'

# 切到项目根目录（本脚本位于 scripts\ 下）
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "[1/2] 初始化数据库 ..." -ForegroundColor Cyan
python -m app.db.init_db
if ($LASTEXITCODE -ne 0) {
    throw "建库失败，中止启动"
}

Write-Host "[2/2] 启动服务 http://127.0.0.1:$Port/docs" -ForegroundColor Cyan
$uvicornArgs = @('app.main:app', '--host', '127.0.0.1', '--port', $Port)
if (-not $NoReload) {
    $uvicornArgs += '--reload'
}

python -m uvicorn @uvicornArgs
