param([switch]$ReadBackOnly)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$auditTaskName = '个人情报库-20261019收录复核'
$config = Get-Content -LiteralPath (Join-Path $projectRoot 'data\catchup-config.json') -Raw | ConvertFrom-Json
$requestPath = Join-Path $projectRoot '.intake\post-intake-audit\request.json'
$request = Get-Content -LiteralPath $requestPath -Raw | ConvertFrom-Json
if ($request.id -cne 'post-intake-20261019' -or $request.target -cne '2026-10-19T09:00:00+08:00' -or $request.not_before -cne '2026-10-19T09:05:00+08:00') { throw '复核日期或身份不正确，未注册。' }
$auditPython = [IO.Path]::GetFullPath($config.pythonw)
$auditScript = Join-Path $PSScriptRoot 'post_intake_audit.py'
$auditArguments = '-B "' + $auditScript + '"'
foreach ($file in @($auditPython, $auditScript, (Join-Path $PSScriptRoot 'intake_audit_checks.py'), (Join-Path $PSScriptRoot 'notify_intake_audit.ps1'))) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw '复核所需文件缺失。' }
}
$existing = Get-ScheduledTask -TaskName $auditTaskName -TaskPath '\' -ErrorAction SilentlyContinue
if (-not $ReadBackOnly) {
    if ($existing) { throw '同名复核任务已存在；不覆盖已有任务。' }
    $auditIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $action = New-ScheduledTaskAction -Execute $auditPython -Argument $auditArguments -WorkingDirectory $projectRoot
    $first = New-ScheduledTaskTrigger -Once -At ([DateTimeOffset]::Parse($request.not_before).LocalDateTime) -RepetitionInterval (New-TimeSpan -Minutes 2)
    # PowerShell 7 can parse JSON ISO strings as DateTime; assign the validated
    # ISO literal so CIM never receives a locale-formatted StartBoundary.
    $first.StartBoundary = '2026-10-19T09:05:00+08:00'
    $login = New-ScheduledTaskTrigger -AtLogOn -User $auditIdentity
    $login.Delay = 'PT45S'
    $principal = New-ScheduledTaskPrincipal -UserId $auditIdentity -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -Hidden -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
    $task = New-ScheduledTask -Action $action -Trigger @($first,$login) -Principal $principal -Settings $settings -Description '仅一次：2026-10-19 09:00收录完成后后台复核六类完整性、去重、原文和首页；未完成先等；成功安静，问题只入通知中心；完成后自停用。'
    Register-ScheduledTask -TaskName $auditTaskName -TaskPath '\' -InputObject $task | Out-Null
}
$saved = Get-ScheduledTask -TaskName $auditTaskName -TaskPath '\'
$info = Get-ScheduledTaskInfo -TaskName $auditTaskName -TaskPath '\'
$destination = Join-Path $projectRoot 'evidence\post-intake-audit'
[IO.Directory]::CreateDirectory($destination) | Out-Null
Export-ScheduledTask -TaskName $auditTaskName -TaskPath '\' | Set-Content -LiteralPath (Join-Path $destination 'windows-task.xml') -Encoding utf8
$details = [pscustomobject]@{
    task_name=$saved.TaskName; state=[string]$saved.State; hidden=$saved.Settings.Hidden
    start_when_available=$saved.Settings.StartWhenAvailable; time_limit=$saved.Settings.ExecutionTimeLimit
    multiple_instances=[string]$saved.Settings.MultipleInstances; user=$saved.Principal.UserId
    logon_type=[string]$saved.Principal.LogonType; run_level=[string]$saved.Principal.RunLevel
    executable=$saved.Actions[0].Execute; arguments=$saved.Actions[0].Arguments; working_directory=$saved.Actions[0].WorkingDirectory
    next_run=$info.NextRunTime.ToString('o'); last_result=$info.LastTaskResult
    triggers=@($saved.Triggers | ForEach-Object { [pscustomobject]@{type=$_.CimClass.CimClassName;start=$_.StartBoundary;interval=$_.Repetition.Interval;delay=$_.Delay} })
}
$details | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $destination 'task-readback.json') -Encoding utf8
$details | ConvertTo-Json -Depth 4
