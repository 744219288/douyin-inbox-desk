$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$target = Join-Path $projectRoot 'vendor/Douyin_Spider'
if (Test-Path -LiteralPath $target) { throw '目标目录已存在，请检查后自行处理；脚本不会覆盖。' }
git clone --no-checkout https://github.com/cv-cat/Douyin_Spider.git $target
if ($LASTEXITCODE -ne 0) { throw '下载失败' }
git -C $target checkout --detach fc8b9c2490a9561265425c26058290f01c064eda
if ($LASTEXITCODE -ne 0) { throw '固定版本检出失败' }
Write-Host '已获取独立第三方依赖。该目录不属于本项目 MIT 授权范围，请自行核实上游使用许可。'
