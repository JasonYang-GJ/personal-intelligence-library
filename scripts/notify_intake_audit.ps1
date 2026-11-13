[CmdletBinding()]
param(
    [string]$ResultFile,
    [switch]$ValidateOnly
)

# This helper never invokes report text, opens a window, or reads report bodies.
# Validation does not call Show or enumerate notification history.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

$receipt = [ordered]@{
    schema_version = 1
    request_id = 'post-intake-20261019'
    app_id = 'Microsoft.PowerShell_8wekyb3d8bbwe!App'
    checked_at = [DateTimeOffset]::Now.ToString('o')
    validate_only = [bool]$ValidateOnly
    submitted = $false
    history_match = $null
    history_supported = $false
    history_error = $null
    setting = $null
    setting_error = $null
    enabled = $null
    suppress_popup = $false
    silent_audio = $false
    issue_count = $null
    tag = 'audit20261019'
    group = 'personalintel'
    user_read = 'not_observed'
    error = $null
    error_line = $null
}

try {
    $projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\')
    $allowedRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot 'evidence\post-intake-audit')).TrimEnd('\')
    if ([string]::IsNullOrWhiteSpace($ResultFile)) {
        if (-not $ValidateOnly) { throw 'ResultFile is required when submitting a notification.' }
        $issueCount = 0
    } else {
        $candidate = [IO.Path]::GetFullPath($ResultFile)
        if (-not $candidate.StartsWith($allowedRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw 'ResultFile must be inside this project evidence/post-intake-audit directory.'
        }
        if ([IO.Path]::GetExtension($candidate) -ne '.json') { throw 'ResultFile must be a JSON file.' }
        $entry = Get-Item -LiteralPath $candidate -Force
        if ($entry.PSIsContainer) { throw 'ResultFile must be a regular file.' }
        if ($entry.Length -gt 2MB) { throw 'ResultFile is too large for an audit receipt.' }
        $cursor = $entry
        while ($null -ne $cursor) {
            if (($cursor.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Reparse points are not allowed in the ResultFile path.'
            }
            if ($cursor.FullName.TrimEnd('\').Equals($projectRoot, [StringComparison]::OrdinalIgnoreCase)) { break }
            if ($cursor -is [IO.FileInfo]) { $cursor = $cursor.Directory } else { $cursor = $cursor.Parent }
        }
        if ($null -eq $cursor) { throw 'ResultFile path did not resolve under this project.' }
        $audit = [IO.File]::ReadAllText($candidate, [Text.Encoding]::UTF8) | ConvertFrom-Json
        if ($audit.request_id -cne 'post-intake-20261019') { throw 'Unexpected audit request_id.' }
        if ($audit.status -cnotin @('issues', 'failed')) { throw 'Only issues or failed results can notify.' }
        if ($null -eq $audit.PSObject.Properties['issues'] -or $null -eq $audit.issues -or -not ($audit.issues -is [Array])) {
            throw 'Audit issues must be a JSON array.'
        }
        $issueCount = $audit.issues.Count
        if ($audit.status -ceq 'issues' -and $issueCount -lt 1) { throw 'An issues result must contain at least one issue.' }
    }
    $receipt.issue_count = $issueCount

    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    [Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    [Windows.UI.Notifications.ToastNotifier, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    [Windows.UI.Notifications.NotificationSetting, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null

    # Existing installed PowerShell identity; no registration or shortcut changes.
    # The legacy Windows PowerShell identity could not expose its notification Setting.
    $appId = $receipt.app_id
    $doc = New-Object Windows.Data.Xml.Dom.XmlDocument
    $doc.LoadXml('<toast><visual><binding template="ToastGeneric"><text>个人情报库复核发现问题，请在项目查看复核结果</text><text>问题数量：' + $issueCount + '</text></binding></visual><audio silent="true"/></toast>')
    $toast = New-Object Windows.UI.Notifications.ToastNotification -ArgumentList $doc
    $toast.SuppressPopup = $true
    $toast.Tag = $receipt.tag
    $toast.Group = $receipt.group
    $toast.ExpirationTime = [DateTimeOffset]::Now.AddDays(7)
    $receipt.suppress_popup = [bool]$toast.SuppressPopup
    $receipt.silent_audio = $doc.SelectSingleNode('/toast/audio').GetAttribute('silent') -eq 'true'
    if (-not $receipt.suppress_popup -or -not $receipt.silent_audio) { throw 'Silent notification properties could not be verified.' }
    if ($doc.SelectNodes('/toast/actions').Count -ne 0) { throw 'Notification actions are not permitted.' }

    $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId)
    try {
        # Reflection preserves a getter exception that PowerShell's property adapter
        # otherwise silently turns into null (for example an unregistered identity).
        $settingValue = ([Windows.UI.Notifications.ToastNotifier]).GetProperty('Setting').GetValue($notifier)
        if ($null -eq $settingValue) { throw 'Notification Setting getter returned null.' }
        $receipt.setting = $settingValue.ToString()
        $receipt.enabled = $receipt.setting -eq 'Enabled'
    } catch {
        $cause = $_.Exception
        while ($null -ne $cause.InnerException) { $cause = $cause.InnerException }
        $receipt.setting = 'Unknown'
        $receipt.setting_error = [ordered]@{ type = $cause.GetType().FullName; hresult = ('0x{0:X8}' -f $cause.HResult) }
    }
    $receipt.history_supported = $null -ne ([Windows.UI.Notifications.ToastNotificationManager]).GetProperty('History')
    if (-not $ValidateOnly) {
        if ($receipt.enabled -ne $true) { throw 'Windows notification availability is disabled or could not be established.' }
        $notifier.Show($toast)
        $receipt.submitted = $true
        if ($receipt.history_supported) {
            try {
                # Query only this application identity and inspect tag/group metadata only.
                # Other notifications' text/content is never accessed or emitted.
                $items = [Windows.UI.Notifications.ToastNotificationManager]::History.GetHistory($appId)
                $receipt.history_match = $false
                foreach ($item in $items) {
                    if ($item.Tag -ceq $receipt.tag -and $item.Group -ceq $receipt.group) {
                        $receipt.history_match = $true
                        break
                    }
                }
            } catch {
                $receipt.history_error = $_.Exception.GetType().FullName
            }
        }
    }
} catch {
    $receipt.error = $_.Exception.Message
    $receipt.error_line = $_.InvocationInfo.ScriptLineNumber
}

$receipt | ConvertTo-Json -Depth 4 -Compress
if ($null -ne $receipt.error) { exit 1 }
exit 0
