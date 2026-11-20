#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$Target = '',
    [ValidateRange(0, 65535)][int]$Port = 0,
    [switch]$NoBrowser,
    [switch]$Check,
    [switch]$Configure,
    [switch]$FirstUse,
    [switch]$Setup,
    [switch]$SelfTest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'ResearchKit-FirstUse.ps1')

function Assert-SshTarget([string]$Value) {
    if ($Value -cnotmatch '^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$') {
        throw '连接名称只能是 SSH 配置别名或主机名，例如 svr-dl；不要填写整条 ssh 命令。'
    }
}

function New-SshArguments([string]$Alias, [int]$LocalPort, $Connection = $null) {
    Assert-SshTarget $Alias
    if ($LocalPort -lt 1024 -or $LocalPort -gt 65535) { throw '网页端口必须在 1024–65535 之间。' }
    # No shell evaluates these local arguments. Alias and port are restricted
    # above; only the constant remote command contains spaces or quotes.
    $template = ('-tt -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 ' +
        '-o ServerAliveCountMax=3 -o ConnectTimeout=15 -o ForwardAgent=no ' +
        '-o ForwardX11=no -o ControlMaster=no -o ControlPath=none ' +
        '-L 127.0.0.1:{0}:127.0.0.1:{0} {1} "exec /usr/local/bin/rk-web --port {0}"')
    $options = @(Get-AccountOptions $Connection)
    $prefix = ''
    if ($options.Count) { $prefix = (Join-NativeArguments $options) + ' ' }
    return $prefix + ($template -f $LocalPort, $Alias)
}

function Get-LaunchUrl([string]$Line, [int]$LocalPort) {
    $clean = ($Line -replace '\x1b\[[0-?]*[ -/]*[@-~]', '').Trim()
    if ($clean -cmatch '^http://127\.0\.0\.1:([0-9]{1,5})/#token=([A-Za-z0-9_-]{32,128})$') {
        if ([int]$Matches[1] -eq $LocalPort) { return $clean }
    }
    return $null
}

function Get-FreeLocalPort([int]$Requested) {
    if ($Requested -ne 0 -and $Requested -lt 1024) { throw '请使用大于 1023 的网页端口。' }
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Requested)
    try {
        $listener.Server.ExclusiveAddressUse = $true
        $listener.Start()
        return [int]$listener.LocalEndpoint.Port
    } finally { $listener.Stop() }
}

function Test-WebSession([string]$LaunchUrl, [int]$LocalPort) {
    # Probe only this SSH forwarding port, without proxying or redirects.
    # The session token is never sent to a different host or written to disk.
    $request = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$LocalPort/api/me")
    $request.Proxy = $null
    $request.AllowAutoRedirect = $false
    $request.Timeout = 5000
    $request.ReadWriteTimeout = 5000
    $request.Headers.Add('Authorization', 'Bearer ' + $LaunchUrl.Split('=')[-1])
    $response = $null
    try {
        $response = $request.GetResponse()
        if ([int]$response.StatusCode -ne 200) { throw '网页认证未通过。' }
    } finally { if ($null -ne $response) { $response.Close() } }
}

function Invoke-SelfTest {
    $token = 'a' * 43
    $good = "http://127.0.0.1:54321/#token=$token"
    if ((Get-LaunchUrl $good 54321) -cne $good) { throw '有效 URL 识别失败' }
    foreach ($line in @(
        "https://evil.invalid/#token=$token", "http://localhost:54321/#token=$token",
        "http://127.0.0.1:54322/#token=$token", "$good/extra", "prefix $good",
        'http://127.0.0.1:54321/#token=short', "http://127.0.0.1:54321/#token=$token&extra=x"
    )) {
        if ($null -ne (Get-LaunchUrl $line 54321)) { throw '未拒绝非法启动 URL' }
    }
    foreach ($alias in @('-oProxyCommand=bad', 'svr-dl;echo bad', 'user@host', 'a b', 'a"b', '')) {
        $rejected = $false
        try { New-SshArguments $alias 54321 | Out-Null } catch { $rejected = $true }
        if (-not $rejected) { throw '未拒绝非法 SSH 参数' }
    }
    $arguments = New-SshArguments 'svr-dl' 54321
    if (-not $arguments.Contains('-L 127.0.0.1:54321:127.0.0.1:54321 svr-dl')) { throw '转发参数错误' }
    if (-not $arguments.EndsWith('"exec /usr/local/bin/rk-web --port 54321"')) { throw '远端命令错误' }
    $available = Get-FreeLocalPort 0
    if ($available -lt 1024 -or $available -gt 65535) { throw '端口分配失败' }
    Write-Host 'Self-test passed: target validation, forwarding command, URL allowlist, local port allocation.'
}

$sshProcess = $null
$started = $false
$exitCode = 0
try {
    if ($SelfTest) { Invoke-SelfTest; exit 0 }
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw '双击启动器用于 Windows；请在 Windows 电脑上运行。'
    }
    $configPath = Join-Path $PSScriptRoot 'connection.json'
    $connection = $null
    $explicitTarget = -not [string]::IsNullOrWhiteSpace($Target)
    if (-not $Target) {
        $Target = 'svr-dl'
        if (-not $Setup -and (Test-Path -LiteralPath $configPath)) {
            $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $Target = [string]$config.target
            $connection = $config
        }
    }
    if ($Configure) {
        $entered = Read-Host "请输入平时 ssh 后面的连接别名（直接回车保留 $Target）"
        if ($entered) { $Target = $entered.Trim() }
        Assert-SshTarget $Target
        @{ target = $Target } | ConvertTo-Json | Set-Content -LiteralPath $configPath -Encoding UTF8
        Write-Host '已保存连接别名。以后双击 Start-ResearchKit.cmd 即可。'
        exit 0
    }
    Assert-SshTarget $Target
    $ssh = Get-Command ssh.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $ssh) {
        throw '没有找到 ssh.exe。请在 Windows 可选功能中安装 OpenSSH 客户端，然后重新双击；不需要安装 SSH 服务器。'
    }
    if (-not $Check -and -not $explicitTarget -and ($Setup -or ($FirstUse -and -not (Test-Path -LiteralPath $configPath)))) {
        $connection = Initialize-ResearchKitConnection $ssh.Source $configPath
        $Target = [string]$connection.target
    }
    Assert-SshTarget $Target
    $localPort = Get-FreeLocalPort $Port
    $arguments = New-SshArguments $Target $localPort $connection
    if ($Check) {
        Write-Host "本机检查通过：SSH $($ssh.Source)，连接别名 $Target，空闲端口 $localPort。"
        Write-Host '此检查没有连接服务器，也没有验证密钥、服务器或浏览器。'
        exit 0
    }
    Write-Host "正在通过 ssh $Target 连接 ResearchKit…"
    Write-Host '如有密钥口令或主机指纹提示，请按平时 SSH 登录方式处理。'
    Write-Host '连接成功后网页自动打开。保留这个窗口即可，可最小化；Ctrl+C 或关闭窗口结束连接。'
    $info = [System.Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $ssh.Source
    $info.Arguments = $arguments
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $false
    $info.RedirectStandardOutput = $true
    # stdin/stderr inherit the console: SSH handles authentication itself.
    $info.RedirectStandardInput = $false
    $info.RedirectStandardError = $false
    $sshProcess = [System.Diagnostics.Process]::new()
    $sshProcess.StartInfo = $info
    $started = $sshProcess.Start()
    $opened = $false
    $clock = [System.Diagnostics.Stopwatch]::StartNew()
    $lineTask = $sshProcess.StandardOutput.ReadLineAsync()
    while ($true) {
        # Short waits let PowerShell process Ctrl+C and reach the cleanup block.
        if (-not $lineTask.Wait(200)) {
            if (-not $opened -and $clock.Elapsed.TotalSeconds -gt 120) {
                throw '两分钟内未收到网页启动地址。请检查 SSH 登录提示；稍后重新双击启动。'
            }
            continue
        }
        $line = $lineTask.GetAwaiter().GetResult()
        if ($null -eq $line) { break }
        $launchUrl = Get-LaunchUrl $line $localPort
        if (-not $opened -and $null -ne $launchUrl) {
            try { Test-WebSession $launchUrl $localPort }
            catch { throw '已收到启动地址，但未能通过隧道验证网页。请关闭后重试，并检查服务器启动报错。' }
            $opened = $true
            if ($NoBrowser) { Write-Host $launchUrl }
            else {
                try { Start-Process -FilePath $launchUrl }
                catch {
                    Write-Host '浏览器未能自动打开，请复制以下地址到本机浏览器（不要分享令牌）：'
                    Write-Host $launchUrl
                }
            }
            Write-Host 'ResearchKit 已连接。此窗口可以最小化；结束连接不会删除项目和固定提交。'
            $launchUrl = $null
        } elseif ($line) {
            Write-Host ($line -replace '#token=[A-Za-z0-9_-]+', '#token=[已隐藏]')
        }
        $lineTask = $sshProcess.StandardOutput.ReadLineAsync()
    }
    $sshProcess.WaitForExit()
    if ($sshProcess.ExitCode -ne 0) {
        throw "SSH 已结束（退出码 $($sshProcess.ExitCode)）。若提示端口占用，重新双击会选择新端口；其他报错请先用 ssh $Target 核对登录。"
    }
    if (-not $opened) { throw 'SSH 未返回 ResearchKit 启动地址。请确认服务器已安装 /usr/local/bin/rk-web。' }
    Write-Host '连接已结束。再次双击可重新打开。'
} catch {
    Write-Host ('启动未完成：' + $_.Exception.Message) -ForegroundColor Red
    $exitCode = 1
} finally {
    if ($null -ne $sshProcess) {
        if ($started -and -not $sshProcess.HasExited) {
            $sshProcess.Kill()
            $null = $sshProcess.WaitForExit(3000)
        }
        $sshProcess.Dispose()
    }
}
exit $exitCode
