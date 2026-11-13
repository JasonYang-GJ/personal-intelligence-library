param([switch]$Probe, [switch]$RunNow)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$taskName = '设置个人情报库每日收录'
$taskId = 'example-personal-library-intake'
$threadId = '00000000-0000-4000-8000-000000000002'
$codexDirectory = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.codex'
$configurationPath = Join-Path $codexDirectory ('automations\' + $taskId + '\automation.toml')
$rolloutPath = Join-Path $codexDirectory 'sessions\2026\10\18\rollout-2026-10-18T00-39-59-00000000-0000-4000-8000-000000000002.jsonl'
$answer = [ordered]@{
    schema_version=1; ok=$false; mode=$(if ($Probe) { 'probe' } else { 'run_now' })
    status='waiting'; reason='not_checked'; task_name=$taskName; task_id=$taskId; thread_id=$threadId
    checked_at=[DateTimeOffset]::Now.ToUniversalTime().ToString('o')
    triggered=$false; invocation_returned=$false; started=$false; ui_changed=$false
    native_state=$null; probe=$null; activation=$null; controls_invoked=@()
}
$mutex = $null
$ownsMutex = $false

function Get-NativeIntakeState {
    if (-not (Test-Path -LiteralPath $configurationPath -PathType Leaf) -or -not (Test-Path -LiteralPath $rolloutPath -PathType Leaf)) {
        throw 'native_state_files_missing'
    }
    $configuration = [IO.File]::ReadAllText($configurationPath, [Text.Encoding]::UTF8)
    foreach ($expected in @(@('id',$taskId), @('kind','heartbeat'), @('name',$taskName), @('status','ACTIVE'), @('target_thread_id',$threadId))) {
        $matches = [regex]::Matches($configuration, '(?m)^' + [regex]::Escape($expected[0]) + '\s*=\s*"([^"\r\n]*)"\s*$')
        if ($matches.Count -ne 1 -or $matches[0].Groups[1].Value -cne $expected[1]) { throw 'native_configuration_identity_or_status_mismatch' }
    }
    # 只读取这一条固定任务的日志，不搜索或打开其他聊天。
    $stream = [IO.File]::Open($rolloutPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    $reader = [IO.StreamReader]::new($stream, [Text.Encoding]::UTF8)
    $pending = [Collections.Generic.HashSet[string]]::new()
    $sessionVerified = $false; $eventCount = 0; $lastStart = $null; $lastComplete = $null; $lastAborted = $null; $lastTerminal = $null
    try {
        while (-not $reader.EndOfStream) {
            $line = $reader.ReadLine()
            if ($line -notmatch '"type"\s*:\s*"(?:session_meta|task_started|task_complete|task_aborted)"') { continue }
            try { $entry = $line | ConvertFrom-Json -ErrorAction Stop } catch { throw 'native_lifecycle_json_incomplete' }
            if ($entry.type -eq 'session_meta') {
                if ($entry.payload.id -cne $threadId) { throw 'native_rollout_thread_mismatch' }
                $sessionVerified = $true
            } elseif ($entry.type -eq 'event_msg' -and $entry.payload.type -in @('task_started','task_complete','task_aborted')) {
                $turn = [string]$entry.payload.turn_id
                if (-not $turn) { throw 'native_lifecycle_turn_id_missing' }
                $eventCount++
                $stamp = [pscustomobject]@{ turn_id=$turn; timestamp=$entry.timestamp; type=$entry.payload.type }
                if ($entry.payload.type -eq 'task_started') { $null = $pending.Add($turn); $lastStart = $stamp }
                else {
                    $null = $pending.Remove($turn)
                    $lastTerminal = $stamp
                    if ($entry.payload.type -eq 'task_complete') { $lastComplete = $stamp }
                    else { $lastAborted = $stamp }
                }
            }
        }
    } finally { $reader.Dispose(); $stream.Dispose() }
    if (-not $sessionVerified -or $eventCount -eq 0) { throw 'native_lifecycle_unknown' }
    return [pscustomobject]@{
        state=$(if ($pending.Count) { 'busy' } else { 'idle' }); pending_turns=$pending.Count
        last_started=$lastStart; last_completed=$lastComplete; last_aborted=$lastAborted; last_terminal=$lastTerminal
    }
}

function Initialize-TriggerTypes {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    Add-Type -AssemblyName WindowsBase
    if (-not ('PersonalIntelTriggerNative' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class PersonalIntelTriggerNative {
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr handle);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr handle);
    [DllImport("user32.dll", SetLastError=true)] public static extern IntPtr OpenInputDesktop(uint flags, bool inherit, uint access);
    [DllImport("user32.dll", CharSet=CharSet.Unicode, SetLastError=true)] public static extern bool GetUserObjectInformation(IntPtr handle, int index, StringBuilder value, uint length, out uint needed);
    [DllImport("user32.dll")] public static extern bool CloseDesktop(IntPtr handle);
}
'@
    }
}

function Test-InteractiveDesktop {
    $desktop = [PersonalIntelTriggerNative]::OpenInputDesktop(0, $false, 1)
    if ($desktop -eq [IntPtr]::Zero) { return $false }
    try {
        $name = [Text.StringBuilder]::new(128)
        [uint32]$needed = 0
        return ([PersonalIntelTriggerNative]::GetUserObjectInformation($desktop, 2, $name, 256, [ref]$needed) -and $name.ToString() -ceq 'Default')
    } finally { $null = [PersonalIntelTriggerNative]::CloseDesktop($desktop) }
}

function Get-VisibleNamedControls {
    param($Scope, [string]$Name, $ControlType=$null)
    $condition = [Windows.Automation.PropertyCondition]::new([Windows.Automation.AutomationElement]::NameProperty, $Name)
    $nodes = $Scope.FindAll([Windows.Automation.TreeScope]::Descendants, $condition)
    for ($i=0; $i -lt $nodes.Count; $i++) {
        $node = $nodes.Item($i)
        if (-not $node.Current.IsOffscreen -and $node.Current.Name -ceq $Name -and
            (-not $ControlType -or $node.Current.ControlType -eq $ControlType)) { $node }
    }
}

function Get-VisibleMenus {
    param($Scope)
    $condition = [Windows.Automation.OrCondition]::new(
        [Windows.Automation.PropertyCondition]::new([Windows.Automation.AutomationElement]::ControlTypeProperty, [Windows.Automation.ControlType]::Menu),
        [Windows.Automation.PropertyCondition]::new([Windows.Automation.AutomationElement]::ControlTypeProperty, [Windows.Automation.ControlType]::MenuItem))
    $nodes = $Scope.FindAll([Windows.Automation.TreeScope]::Descendants, $condition)
    $walker = [Windows.Automation.TreeWalker]::ControlViewWalker
    for ($i=0; $i -lt $nodes.Count; $i++) {
        $node = $nodes.Item($i)
        if ($node.Current.IsOffscreen) { continue }
        if ($node.Current.ControlType -eq [Windows.Automation.ControlType]::Menu) { $node; continue }
        # 常驻 MenuBar 下的 MenuItem 只是菜单入口；仅 Menu 容器内的项代表已弹出菜单。
        $ancestor = $walker.GetParent($node)
        for ($depth=0; $depth -lt 64 -and $ancestor; $depth++) {
            if ($ancestor.Current.ControlType -eq [Windows.Automation.ControlType]::Menu) { $node; break }
            if ($ancestor.Current.ControlType -eq [Windows.Automation.ControlType]::MenuBar -or
                [Windows.Automation.Automation]::Compare($ancestor, $Scope)) { break }
            $ancestor = $walker.GetParent($ancestor)
        }
    }
}

function Test-ScheduledView {
    param($Scope)
    return (@(Get-VisibleNamedControls $Scope '新建任务').Count -eq 1 -and @(Get-VisibleNamedControls $Scope '搜索定时任务').Count -eq 1)
}

function Get-WindowReadiness {
    if (-not (Test-InteractiveDesktop)) { throw 'desktop_locked_or_not_interactive' }
    $processes = @(Get-Process -Name ChatGPT -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 })
    if ($processes.Count -ne 1) { throw 'chatgpt_window_missing_or_ambiguous' }
    $process = $processes[0]
    $handle = [IntPtr]$process.MainWindowHandle
    if ([PersonalIntelTriggerNative]::IsIconic($handle)) { throw 'chatgpt_window_minimized' }
    $window = [Windows.Automation.AutomationElement]::FromHandle($handle)
    $nodes = $window.FindAll([Windows.Automation.TreeScope]::Descendants, [Windows.Automation.Condition]::TrueCondition)
    for ($i=0; $i -lt $nodes.Count; $i++) {
        $node = $nodes.Item($i)
        if ($node.Current.IsOffscreen) { continue }
        if ($node.Current.Name -cin @('登录','登入','登录 ChatGPT','登录或注册','Log in','Sign in')) { throw 'chatgpt_login_required' }
        if ($node.Current.LocalizedControlType -match '^(?:dialog|对话框)$') { throw 'chatgpt_dialog_open' }
        $pattern = $null
        if ($node.Current.ControlType -eq [Windows.Automation.ControlType]::Window -and
            $node.TryGetCurrentPattern([Windows.Automation.WindowPattern]::Pattern, [ref]$pattern) -and $pattern.Current.IsModal) { throw 'chatgpt_modal_dialog_open' }
    }
    if (@(Get-VisibleMenus $window).Count) { throw 'chatgpt_menu_already_open' }
    $scheduled = Test-ScheduledView $window
    if (-not $scheduled -and @(Get-VisibleNamedControls $window '定时任务' ([Windows.Automation.ControlType]::Button)).Count -ne 1) {
        throw 'scheduled_view_or_navigation_not_accessible'
    }
    return [pscustomobject]@{ process=$process; handle=$handle; root=$window; scheduled_view=$scheduled }
}

function Assert-TriggerWindow {
    param($Context)
    if (-not (Test-InteractiveDesktop)) { throw 'desktop_became_locked' }
    $Context.process.Refresh()
    if ($Context.process.HasExited -or $Context.process.MainWindowHandle -ne $Context.handle -or
        [PersonalIntelTriggerNative]::GetForegroundWindow() -ne $Context.handle) { throw 'foreground_window_changed' }
    $actual = [Windows.Automation.AutomationElement]::FromHandle($Context.handle)
    if (-not [Windows.Automation.Automation]::Compare($Context.root, $actual)) { throw 'window_identity_changed' }
}

function Activate-TriggerWindow {
    param($Context)
    $answer.activation = [ordered]@{
        initially_foreground=([PersonalIntelTriggerNative]::GetForegroundWindow() -eq $Context.handle)
        method='already_foreground'; foreground=$false
    }
    if (-not $answer.activation.initially_foreground) {
        $answer.activation.method = 'SetForegroundWindow'
        $null = [PersonalIntelTriggerNative]::SetForegroundWindow($Context.handle)
        $answer.ui_changed = $true
        Start-Sleep -Milliseconds 250
        if ([PersonalIntelTriggerNative]::GetForegroundWindow() -ne $Context.handle) {
            # 正常的控件聚焦；不发送按键，不更改 Windows 前台权限。
            if (-not (Test-InteractiveDesktop)) { throw 'desktop_became_locked' }
            $Context.process.Refresh()
            if ($Context.process.HasExited -or $Context.process.MainWindowHandle -ne $Context.handle) { throw 'window_identity_changed' }
            if (@(Get-VisibleMenus $Context.root).Count) { throw 'unexpected_menu_before_window_focus' }
            $nav = @(Get-VisibleNamedControls $Context.root '定时任务' ([Windows.Automation.ControlType]::Button))
            if ($nav.Count -ne 1 -or -not $nav[0].Current.IsEnabled -or -not $nav[0].Current.IsKeyboardFocusable) {
                throw 'scheduled_navigation_not_uniquely_focusable'
            }
            $answer.activation.method = 'UIA scheduled navigation SetFocus'
            $nav[0].SetFocus()
            for ($attempt=0; $attempt -lt 5 -and [PersonalIntelTriggerNative]::GetForegroundWindow() -ne $Context.handle; $attempt++) {
                Start-Sleep -Milliseconds 200
            }
        }
    }
    Assert-TriggerWindow $Context
    $answer.activation.foreground = $true
}

function Assert-TriggerControl {
    param($Context, $Target, [switch]$AllowMenu)
    $walker = [Windows.Automation.TreeWalker]::ControlViewWalker
    for ($attempt=0; $attempt -lt 7; $attempt++) {
        Assert-TriggerWindow $Context
        $rect = $Target.Current.BoundingRectangle
        if ($Target.Current.IsOffscreen -or -not $Target.Current.IsEnabled -or $rect.Width -lt 2 -or $rect.Height -lt 2) { throw 'target_not_visible_or_enabled' }
        $point = [Windows.Point]::new($rect.X + $rect.Width / 2, $rect.Y + $rect.Height / 2)
        $current = [Windows.Automation.AutomationElement]::FromPoint($point)
        for ($depth=0; $depth -lt 128 -and $current; $depth++) {
            if ([Windows.Automation.Automation]::Compare($current, $Target)) { Assert-TriggerWindow $Context; return }
            if (-not $AllowMenu -and $current.Current.ControlType -in @([Windows.Automation.ControlType]::Menu,[Windows.Automation.ControlType]::MenuItem)) { throw 'target_obscured_by_menu' }
            $current = $walker.GetParent($current)
        }
        if ($attempt -lt 6) { Start-Sleep -Milliseconds 200 }
    }
    throw 'target_obscured_or_hit_identity_unknown'
}

function Invoke-TriggerControl {
    param($Context, $Target, [string]$AllowedName, [switch]$AllowMenu)
    if ($AllowedName -cnotin @('定时任务','已安排任务操作','立即运行') -or $Target.Current.Name -cne $AllowedName) { throw 'control_not_authorized' }
    Assert-TriggerControl $Context $Target -AllowMenu:$AllowMenu
    $pattern = $null
    $method = 'Invoke'
    if (-not $Target.TryGetCurrentPattern([Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
        # 此版本的导航及下拉按钮提供 ExpandCollapse，而非 Invoke。
        # 仅允许展开这两个精确按钮；最终“立即运行”仍必须提供 Invoke。
        $pattern = $null
        if ($AllowedName -cnotin @('定时任务','已安排任务操作') -or
            -not $Target.TryGetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern, [ref]$pattern) -or
            $pattern.Current.ExpandCollapseState -ne [Windows.Automation.ExpandCollapseState]::Collapsed) {
            throw ('control_action_unavailable_no_mouse_fallback:' + $AllowedName)
        }
        $method = 'Expand'
    }
    Assert-TriggerWindow $Context
    if ($AllowedName -ceq '立即运行') { $answer.triggered = $true }
    $answer.ui_changed = $true
    $answer.controls_invoked += [pscustomobject]@{name=$AllowedName;method=$method}
    if ($method -ceq 'Expand') { $pattern.Expand() } else { $pattern.Invoke() }
    if ($AllowedName -ceq '立即运行') { $answer.invocation_returned = $true }
}

function Get-OwnTaskRow {
    param($Scope)
    $titles = @(Get-VisibleNamedControls $Scope $taskName)
    if ($titles.Count -ne 1) { throw 'own_task_title_missing_or_ambiguous' }
    $walker = [Windows.Automation.TreeWalker]::ControlViewWalker
    $row = $titles[0]
    for ($depth=0; $depth -lt 8 -and $row; $depth++) {
        if ([Windows.Automation.Automation]::Compare($row, $Scope)) { break }
        $actions = @(Get-VisibleNamedControls $row '已安排任务操作' ([Windows.Automation.ControlType]::Button))
        if ($actions.Count -gt 1) { throw 'task_row_scope_contains_multiple_action_buttons' }
        if ($actions.Count -eq 1) {
            $insideTitles = @(Get-VisibleNamedControls $row $taskName)
            # 任务标题按钮本身就是最小行容器时，Descendants 不包含它自身。
            if ($row.Current.Name -ceq $taskName) { $insideTitles = @($row) + $insideTitles }
            if ($insideTitles.Count -ne 1) { throw 'own_task_row_title_not_unique' }
            $titleRect = $titles[0].Current.BoundingRectangle
            $actionRect = $actions[0].Current.BoundingRectangle
            if ($actionRect.Bottom -lt $titleRect.Top -or $actionRect.Top -gt $titleRect.Bottom) { throw 'task_action_not_on_own_title_row' }
            return [pscustomobject]@{ row=$row; title=$titles[0]; action=$actions[0] }
        }
        $row = $walker.GetParent($row)
    }
    throw 'own_task_row_action_not_proven'
}

function Get-OwnRunMenuItem {
    param($Scope, $OwnAction)
    $items = @(Get-VisibleNamedControls $Scope '立即运行' ([Windows.Automation.ControlType]::MenuItem))
    if ($items.Count -ne 1) { return $null }
    $walker = [Windows.Automation.TreeWalker]::ControlViewWalker
    $menu = $walker.GetParent($items[0])
    for ($depth=0; $depth -lt 8 -and $menu; $depth++) {
        if ($menu.Current.ControlType -eq [Windows.Automation.ControlType]::Menu) {
            $menus = @(Get-VisibleMenus $Scope | Where-Object { $_.Current.ControlType -eq [Windows.Automation.ControlType]::Menu })
            if ($menus.Count -ne 1 -or -not [Windows.Automation.Automation]::Compare($menus[0], $menu)) { throw 'run_menu_scope_ambiguous' }
            # 门户菜单不一定是行元素的后代，额外核对它仍紧邻刚打开的本任务按钮。
            $menuRect = $menu.Current.BoundingRectangle
            $actionRect = $OwnAction.Current.BoundingRectangle
            $verticalGap = [Math]::Min([Math]::Abs($menuRect.Top - $actionRect.Bottom), [Math]::Abs($menuRect.Bottom - $actionRect.Top))
            if ($verticalGap -gt 32 -or $menuRect.Right -lt $actionRect.Left - 32 -or $menuRect.Left -gt $actionRect.Right + 32) {
                throw 'run_menu_not_anchored_to_own_task_action'
            }
            return [pscustomobject]@{ item=$items[0]; menu=$menu }
        }
        $menu = $walker.GetParent($menu)
    }
    throw 'run_item_menu_ownership_not_proven'
}

try {
    if ($Probe -and $RunNow) { throw 'probe_and_run_now_are_mutually_exclusive' }
    Initialize-TriggerTypes
    $native = Get-NativeIntakeState
    $answer.native_state = $native
    if ($native.state -eq 'busy') {
        $answer.status = 'busy'; $answer.reason = 'native_task_has_unfinished_turn'
    } else {
        $context = Get-WindowReadiness
        $answer.probe = [pscustomobject]@{ unlocked=$true; window_available=$true; login_view_accessible=$true; scheduled_view=$context.scheduled_view }
        if ($Probe) {
            $answer.ok = $true; $answer.status = 'ready'; $answer.reason = 'read_only_readiness_checks_passed'
        } else {
            $mutex = [Threading.Mutex]::new($false, 'Local\PersonalIntelDailyIntakeTrigger_09_00')
            try { $ownsMutex = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $ownsMutex = $true }
            if (-not $ownsMutex) { throw 'another_trigger_process_is_active' }
            $native = Get-NativeIntakeState
            $answer.native_state = $native
            if ($native.state -ne 'idle') { throw 'native_task_became_busy' }
            Activate-TriggerWindow $context
            if (-not $context.scheduled_view) {
                $nav = @(Get-VisibleNamedControls $context.root '定时任务' ([Windows.Automation.ControlType]::Button))
                if ($nav.Count -ne 1) { throw 'scheduled_navigation_ambiguous' }
                Invoke-TriggerControl $context $nav[0] '定时任务'
                for ($attempt=0; $attempt -lt 12 -and -not (Test-ScheduledView $context.root); $attempt++) { Start-Sleep -Milliseconds 250; Assert-TriggerWindow $context }
                if (-not (Test-ScheduledView $context.root)) { throw 'scheduled_view_did_not_open' }
            }
            if (@(Get-VisibleMenus $context.root).Count) { throw 'unexpected_menu_before_own_task_action' }
            $own = Get-OwnTaskRow $context.root
            Invoke-TriggerControl $context $own.action '已安排任务操作'
            $runItem = $null
            for ($attempt=0; $attempt -lt 12; $attempt++) {
                Assert-TriggerWindow $context
                $runItem = Get-OwnRunMenuItem $context.root $own.action
                if ($runItem) { break }
                Start-Sleep -Milliseconds 200
            }
            if (-not $runItem) { throw 'own_task_run_menu_not_available' }
            $currentOwn = Get-OwnTaskRow $context.root
            if (-not [Windows.Automation.Automation]::Compare($own.row, $currentOwn.row) -or
                -not [Windows.Automation.Automation]::Compare($own.action, $currentOwn.action)) { throw 'own_task_row_changed_after_menu_open' }
            $native = Get-NativeIntakeState
            $answer.native_state = $native
            if ($native.state -ne 'idle') { throw 'native_task_became_busy_before_run' }
            $beforeTurn = $native.last_started.turn_id
            $currentMenu = Get-OwnRunMenuItem $context.root $own.action
            if (-not $currentMenu -or -not [Windows.Automation.Automation]::Compare($runItem.menu, $currentMenu.menu) -or
                -not [Windows.Automation.Automation]::Compare($runItem.item, $currentMenu.item)) { throw 'own_run_menu_changed_before_invoke' }
            Invoke-TriggerControl $context $currentMenu.item '立即运行' -AllowMenu
            $answer.ok = $true; $answer.status = 'trigger_requested'; $answer.reason = 'run_now_invoked_waiting_for_native_start'
            for ($attempt=0; $attempt -lt 20; $attempt++) {
                Start-Sleep -Milliseconds 250
                $after = Get-NativeIntakeState
                $answer.native_state = $after
                if ($after.last_started -and $after.last_started.turn_id -cne $beforeTurn) {
                    $answer.started = $true; $answer.status = 'started'; $answer.reason = 'new_native_task_started_event_observed'; break
                }
            }
        }
    }
} catch {
    $answer.ok = $false
    $answer.status = $(if ($answer.triggered) { 'trigger_uncertain' } else { 'waiting' })
    $answer.reason = $_.Exception.Message.Substring(0, [Math]::Min(400, $_.Exception.Message.Length))
} finally {
    if ($ownsMutex) { $mutex.ReleaseMutex() }
    if ($mutex) { $mutex.Dispose() }
    $answer.checked_at = [TimeZoneInfo]::ConvertTime([DateTimeOffset]::UtcNow, [TimeZoneInfo]::FindSystemTimeZoneById('China Standard Time')).ToString('o')
    [pscustomobject]$answer | ConvertTo-Json -Depth 8 -Compress
}
