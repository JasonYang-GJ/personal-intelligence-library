param([switch]$ReadBackOnly)
$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$taskName = '个人情报库-开机补收检查'
$configPath = Join-Path $project 'data\catchup-config.json'
$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$pythonw = [IO.Path]::GetFullPath($config.pythonw)
$watcher = Join-Path $PSScriptRoot 'catchup_watch.py'
if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf) -or -not (Test-Path -LiteralPath $watcher -PathType Leaf)) { throw '运行程序不存在，未注册任务。' }
$arguments = '-B "' + $watcher + '"'
$existing = Get-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction SilentlyContinue
if (-not $ReadBackOnly) {
    if ($existing) {
        if ($existing.Actions.Count -ne 1 -or $existing.Actions[0].Execute -cne $pythonw -or $existing.Actions[0].Arguments -cne $arguments) {
            throw '同名任务不属于本项目，拒绝覆盖。'
        }
        throw '本项目补收任务已经存在；本安装器不会自动覆盖，请先核验现有配置。'
    }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $project
    $login = New-ScheduledTaskTrigger -AtLogOn -User $identity
    $login.Delay = 'PT30S'
    $periodic = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) -RepetitionInterval (New-TimeSpan -Minutes 2)
    $principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -Hidden -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 3)
    $task = New-ScheduledTask -Action $action -Trigger @($login, $periodic) -Principal $principal -Settings $settings -Description '个人情报库：登录后和每两分钟检查漏跑，仅在GPT已就绪且六类断点落后时触发现有收录任务；不修改每天9点计划。'
    Register-ScheduledTask -TaskName $taskName -TaskPath '\' -InputObject $task | Out-Null
}
$saved = Get-ScheduledTask -TaskName $taskName -TaskPath '\'
$info = Get-ScheduledTaskInfo -TaskName $taskName -TaskPath '\'
$xml = Export-ScheduledTask -TaskName $taskName -TaskPath '\'
$evidence = Join-Path $project 'evidence\catchup-setup'
[IO.Directory]::CreateDirectory($evidence) | Out-Null
$xml | Set-Content -LiteralPath (Join-Path $evidence 'windows-task.xml') -Encoding utf8
[pscustomobject]@{
    task_name=$saved.TaskName; state=[string]$saved.State
    executable=$saved.Actions[0].Execute; arguments=$saved.Actions[0].Arguments
    working_directory=$saved.Actions[0].WorkingDirectory
    user_id=$saved.Principal.UserId; logon_type=[string]$saved.Principal.LogonType
    run_level=[string]$saved.Principal.RunLevel; hidden=$saved.Settings.Hidden
    start_when_available=$saved.Settings.StartWhenAvailable
    multiple_instances=[string]$saved.Settings.MultipleInstances
    triggers=@($saved.Triggers | ForEach-Object { [pscustomobject]@{type=$_.CimClass.CimClassName;enabled=$_.Enabled;delay=$_.Delay;interval=$_.Repetition.Interval;start_boundary=$_.StartBoundary;user_id=$_.UserId} })
    last_run=$info.LastRunTime.ToString('o'); last_result=$info.LastTaskResult; next_run=$info.NextRunTime.ToString('o')
} | ConvertTo-Json -Depth 5
