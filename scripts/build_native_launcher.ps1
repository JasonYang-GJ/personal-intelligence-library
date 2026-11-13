param(
    [Parameter(Mandatory=$true)][string]$PythonPath,
    [ValidatePattern('^PersonalIntelNativeHostV[2-9][0-9]*$')]
    [string]$LauncherName = 'PersonalIntelNativeHostV2'
)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$hostScript = Join-Path $PSScriptRoot 'extension_native_host.py'
$outputDirectory = Join-Path $projectRoot '.intake\extension-probe\native-host'
$launcherPath = Join-Path $outputDirectory ($LauncherName + '.exe')
$sourcePath = Join-Path $outputDirectory ($LauncherName + '.cs')
$compilerPath = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) { throw '指定的 Python 解释器不存在。' }
if (-not (Test-Path -LiteralPath $hostScript -PathType Leaf)) { throw '固定接收器脚本不存在。' }
if (-not (Test-Path -LiteralPath $compilerPath -PathType Leaf)) { throw '本机 .NET Framework C# 编译器不存在。' }
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
$hostScript = (Resolve-Path -LiteralPath $hostScript).Path
if (Test-Path -LiteralPath $launcherPath) { throw '启动器已存在；本构建脚本不会覆盖现有启动器。' }
$nativeDirectory = New-Item -ItemType Directory -Path $outputDirectory -Force
if (($nativeDirectory.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw '输出目录不能是重解析点。' }
$pythonLiteral = $PythonPath.Replace('"', '""')
$scriptLiteral = $hostScript.Replace('"', '""')
$source = @'
using System;
using System.Diagnostics;
using System.IO;
using System.Text.RegularExpressions;
using System.Threading.Tasks;

internal static class PersonalIntelNativeHost
{
    private static void Pump(Stream source, Stream destination)
    {
        byte[] buffer = new byte[8192];
        int count;
        while ((count = source.Read(buffer, 0, buffer.Length)) > 0)
        {
            destination.Write(buffer, 0, count);
            // Native Messaging keeps both pipes open while awaiting the reply.
            // FileStream buffering must never defer a small frame until EOF.
            destination.Flush();
        }
    }

    private static string Quote(string value)
    {
        // Inputs are fixed Windows file paths or the strictly matched origin.
        if (value.IndexOf('"') >= 0 || value.EndsWith("\\")) throw new ArgumentException();
        return "\"" + value + "\"";
    }

    [STAThread]
    private static int Main(string[] args)
    {
        try
        {
            string origin = null;
            foreach (string argument in args)
            {
                if (Regex.IsMatch(argument, @"^chrome-extension://[a-p]{32}/$"))
                {
                    if (origin != null) return 2;
                    origin = argument;
                }
                else if (!Regex.IsMatch(argument, @"^--parent-window=\d+$")) return 2;
            }
            if (origin == null) return 2;
            ProcessStartInfo info = new ProcessStartInfo();
            info.FileName = @"__PYTHON__";
            info.Arguments = "-I -B " + Quote(@"__SCRIPT__") + " " + Quote(origin);
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            info.WindowStyle = ProcessWindowStyle.Hidden;
            info.RedirectStandardInput = true;
            info.RedirectStandardOutput = true;
            info.RedirectStandardError = true;
            using (Process child = Process.Start(info))
            {
                Task.Factory.StartNew(delegate {
                    try { Pump(Console.OpenStandardInput(), child.StandardInput.BaseStream); }
                    catch (IOException) { }
                    finally { try { child.StandardInput.Close(); } catch (Exception) { } }
                });
                Task output = Task.Factory.StartNew(delegate {
                    using (Stream destination = Console.OpenStandardOutput())
                    {
                        Pump(child.StandardOutput.BaseStream, destination);
                    }
                });
                Task.Factory.StartNew(delegate {
                    // Discard diagnostic text; private content must not enter the protocol.
                    char[] buffer = new char[4096];
                    while (child.StandardError.Read(buffer, 0, buffer.Length) > 0) { }
                });
                child.WaitForExit();
                output.Wait();
                return child.ExitCode;
            }
        }
        catch (Exception) { return 3; }
    }
}
'@
$source = $source.Replace('__PYTHON__', $pythonLiteral).Replace('__SCRIPT__', $scriptLiteral)
[IO.File]::WriteAllText($sourcePath, $source, [Text.UTF8Encoding]::new($false))
& $compilerPath /nologo /target:winexe /optimize+ "/out:$launcherPath" $sourcePath
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $launcherPath -PathType Leaf)) { throw '无窗口启动器编译失败。' }
[pscustomobject]@{
    launcher_path = $launcherPath
    launcher_sha256 = (Get-FileHash -LiteralPath $launcherPath -Algorithm SHA256).Hash.ToLowerInvariant()
    python_path = $PythonPath
    script_path = $hostScript
    registered = $false
    windows_subsystem = 'WindowsApplication'
} | ConvertTo-Json
