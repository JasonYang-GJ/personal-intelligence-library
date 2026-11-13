[CmdletBinding()]
param([switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$stageDirectory = Join-Path $projectRoot '.intake\extension-probe'
$config = Get-Content -LiteralPath (Join-Path $stageDirectory 'config.json') -Raw | ConvertFrom-Json
$extensionManifest = Get-Content -LiteralPath (Join-Path $projectRoot 'browser-extension\manifest.json') -Raw | ConvertFrom-Json
if (-not $extensionManifest.key) { throw '公开模板未设置扩展公钥。请先配置你自己的扩展身份及来源。' }
$digest = [Security.Cryptography.SHA256]::HashData([Convert]::FromBase64String($extensionManifest.key))
$extensionId = -join ($digest[0..15] | ForEach-Object { [char](97 + ($_ -shr 4)); [char](97 + ($_ -band 15)) })
if ($extensionId -ne $config.extension_id -or $config.mode -ne 'probe') { throw '扩展身份或验证模式不匹配。' }
$launcher = Join-Path $stageDirectory 'native-host\PersonalIntelNativeHostV2.exe'
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) { throw '本机接收启动器尚未构建。' }
$manifestPath = Join-Path $stageDirectory 'native-host\com.personal_intel.collector.json'
$manifest = [ordered]@{
    name='com.personal_intel.collector'
    description='个人情报库六类报告验证接收器'
    path=$launcher
    type='stdio'
    allowed_origins=@("chrome-extension://$extensionId/")
}
$registryPath = 'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.personal_intel.collector'
$existingValue = if (Test-Path -LiteralPath $registryPath) { (Get-Item -LiteralPath $registryPath).GetValue('') } else { $null }
if ($existingValue -and $existingValue -ne $manifestPath) { throw '已有同名本机接收器指向其他位置，停止并保留原配置。' }
if (Test-Path -LiteralPath $manifestPath) {
    $existingManifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ($existingManifest.path -ne $launcher -or $existingManifest.name -ne $manifest.name -or
        @($existingManifest.allowed_origins).Count -ne 1 -or $existingManifest.allowed_origins[0] -ne $manifest.allowed_origins[0]) {
        throw '已有接收器清单不同，停止并保留原配置。'
    }
}
if (-not $CheckOnly) {
    if (-not (Test-Path -LiteralPath $manifestPath)) {
        [IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 4), [Text.UTF8Encoding]::new($false))
    }
    if (-not $existingValue) {
        New-Item -Path $registryPath -Force | Out-Null
        Set-Item -LiteralPath $registryPath -Value $manifestPath
    }
}
$registered = (Test-Path -LiteralPath $registryPath) -and ((Get-Item -LiteralPath $registryPath).GetValue('') -eq $manifestPath)
[ordered]@{extension_id=$extensionId;registered=$registered;probe_enabled=$config.enabled;manifest_path=$manifestPath;launcher_sha256=(Get-FileHash -LiteralPath $launcher -Algorithm SHA256).Hash.ToLower()} | ConvertTo-Json
