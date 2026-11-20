#Requires -Version 5.1
# First use runs as the current Windows user, never as an administrator.

function ConvertTo-NativeArgument([string]$Value) {
    # ProcessStartInfo.Arguments uses Windows command-line quoting. Preserve
    # empty arguments, embedded quotes, spaces and trailing backslashes.
    $escaped = $Value -replace '(\\*)"', '$1$1\"'
    $escaped = $escaped -replace '(\\+)$', '$1$1'
    return '"' + $escaped + '"'
}

function Join-NativeArguments([string[]]$Values) {
    return (($Values | ForEach-Object { ConvertTo-NativeArgument $_ }) -join ' ')
}

function Assert-ResearchKitUsername([string]$Username) {
    if ($Username -cnotmatch '^[A-Za-z_][A-Za-z0-9_.-]{0,63}$' -or $Username -eq 'root') {
        throw '请输入自己的普通服务器账号，例如 test 或 ly；不能使用 root。'
    }
}

function Get-AccountOptions($Connection) {
    if ($null -eq $Connection -or $null -eq $Connection.PSObject.Properties['mode']) { return }
    if ($Connection.mode -ne 'account') { throw '不认识的连接配置，请使用 Start-ResearchKit.cmd -Setup 重新设置。' }
    Assert-ResearchKitUsername ([string]$Connection.username)
    $remotePort = [int]$Connection.sshPort
    if ($remotePort -lt 1 -or $remotePort -gt 65535) { throw 'SSH 端口须在 1–65535 之间。' }
    $identity = [string]$Connection.identityFile
    if (-not [IO.Path]::IsPathRooted($identity) -or $identity -match '["\r\n\x00]') { throw '本机密钥路径无效。' }
    if (-not (Test-Path -LiteralPath $identity -PathType Leaf)) {
        throw '没有找到本机密钥。请在这台电脑用 Start-ResearchKit.cmd -Setup 重新设置，不要复制别人的私钥。'
    }
    # Direct-account mode is self-contained; existing SSH aliases/config files
    # (including jump-host setups) remain available through the alias mode.
    return @('-F', 'none', '-p', [string]$remotePort, '-l', [string]$Connection.username,
             '-i', $identity, '-o', 'IdentitiesOnly=yes')
}

function Invoke-ConsoleCommand([string]$Executable, [string[]]$Arguments) {
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $Executable
    $info.Arguments = Join-NativeArguments $Arguments
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $false
    # All streams inherit the console. SSH itself reads the current password;
    # PowerShell never reads, stores or passes it as a process argument.
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $info
    $running = $false
    try {
        $running = $process.Start()
        while (-not $process.WaitForExit(200)) { }
        return $process.ExitCode
    } finally {
        if ($running -and -not $process.HasExited) { $process.Kill(); $process.WaitForExit() }
        $process.Dispose()
    }
}

function Get-EnrollmentCommand([string]$PublicKey, [string]$Username) {
    Assert-ResearchKitUsername $Username
    $line = $PublicKey.Trim()
    if ($line -cnotmatch '^ssh-ed25519 ([A-Za-z0-9+/]+={0,2})(?:[ \t]+[^\r\n]*)?$') {
        throw '本机公钥格式无效，未执行服务器操作。'
    }
    $blob = $Matches[1]
    $raw = [Convert]::FromBase64String($blob)
    $header = [byte[]](0,0,0,11,115,115,104,45,101,100,50,53,53,49,57,0,0,0,32)
    if ($raw.Length -ne 51) { throw '本机公钥长度无效。' }
    for ($index = 0; $index -lt $header.Length; $index++) {
        if ($raw[$index] -ne $header[$index]) { throw '本机公钥类型无效。' }
    }
    # Only the validated key type/blob cross the command boundary. Comments,
    # filenames, private keys and passwords are never sent in this command.
    $command = @'
set -eu
test "$(id -un)" = '__RK_USER__'
test "$(id -u)" != 0
umask 077
test ! -L "$HOME/.ssh"
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
test ! -L "$HOME/.ssh/authorized_keys"
if test -e "$HOME/.ssh/authorized_keys"; then test -f "$HOME/.ssh/authorized_keys"; fi
touch "$HOME/.ssh/authorized_keys"
chmod 600 "$HOME/.ssh/authorized_keys"
key='__RK_KEY__'
if ! grep -qxF -- "$key" "$HOME/.ssh/authorized_keys"; then printf '\n%s\n' "$key" >> "$HOME/.ssh/authorized_keys"; fi
printf 'ResearchKit: public key registered for %s\n' '__RK_USER__'
'@
    return $command.Replace('__RK_USER__', $Username).Replace('__RK_KEY__', 'ssh-ed25519 ' + $blob).Replace("`r", '')
}

function Save-ConnectionProfile([string]$Path, $Connection) {
    $temporary = $Path + '.new-' + [Guid]::NewGuid().ToString('N')
    try {
        $Connection | ConvertTo-Json | Set-Content -LiteralPath $temporary -Encoding UTF8
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function Initialize-ResearchKitConnection([string]$Ssh, [string]$ConfigPath) {
    Write-Host '首次连接：已有可用 SSH 别名可直接填写；新电脑直接按回车，使用自己的账号和当前密码。'
    $alias = (Read-Host '已有 SSH 别名（例如 svr-dl；新电脑直接回车）').Trim()
    if ($alias) {
        Assert-SshTarget $alias
        $connection = [pscustomobject]@{ target = $alias }
        Save-ConnectionProfile $ConfigPath $connection
        return $connection
    }
    $username = (Read-Host '自己的服务器账号，例如 test').Trim()
    Assert-ResearchKitUsername $username
    $server = (Read-Host '服务器地址（回车使用 125.220.134.194）').Trim()
    if (-not $server) { $server = '125.220.134.194' }
    Assert-SshTarget $server
    $enteredPort = (Read-Host 'SSH 端口（回车使用 8222）').Trim()
    $remotePort = 8222
    if ($enteredPort) { $remotePort = [int]$enteredPort }
    if ($remotePort -lt 1 -or $remotePort -gt 65535) { throw 'SSH 端口须在 1–65535 之间。' }
    $keygen = Get-Command ssh-keygen.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $keygen) { throw '没有找到 ssh-keygen.exe，请安装 Windows 的 OpenSSH 客户端。' }
    $digest = [Security.Cryptography.SHA256]::Create()
    try { $tag = ([BitConverter]::ToString($digest.ComputeHash([Text.Encoding]::UTF8.GetBytes("${username}@${server}:${remotePort}")))).Replace('-', '').Substring(0,16) }
    finally { $digest.Dispose() }
    $directory = Join-Path $env:USERPROFILE '.ssh'
    $null = New-Item -ItemType Directory -Path $directory -Force
    $identity = Join-Path $directory "researchkit_${username}_${tag}_ed25519"
    $publicPath = $identity + '.pub'
    if (-not (Test-Path -LiteralPath $identity) -and -not (Test-Path -LiteralPath $publicPath)) {
        Write-Host '正在生成这台电脑的专用密钥。Enter passphrase 是本机密钥口令，可设置或直接回车留空；若设置，以后连接也可能需要输入。'
        if ((Invoke-ConsoleCommand $keygen.Source @('-t','ed25519','-f',$identity,'-C',"researchkit-$username")) -ne 0) {
            throw '本机密钥生成未完成，未登记公钥。'
        }
    }
    if (-not (Test-Path -LiteralPath $identity -PathType Leaf) -or -not (Test-Path -LiteralPath $publicPath -PathType Leaf)) {
        throw '本机专用密钥不完整，已保留现有文件；请管理员协助核对，不会覆盖密钥。'
    }
    $command = Get-EnrollmentCommand ([IO.File]::ReadAllText($publicPath)) $username
    $connection = [pscustomobject]@{ target = $server; mode = 'account'; username = $username;
                                   sshPort = $remotePort; identityFile = $identity }
    $options = @(Get-AccountOptions $connection)
    $common = @('-T','-o','ConnectTimeout=15','-o','ServerAliveInterval=30','-o','ServerAliveCountMax=3',
                '-o','ForwardAgent=no','-o','ForwardX11=no','-o','ControlMaster=no','-o','ControlPath=none')
    Write-Host "即将连接 $username@$server。首次主机指纹请与管理员核对；password 提示请输入该账号现在的服务器密码，输入时不显示字符。"
    $enroll = $common + $options + @('-o','StrictHostKeyChecking=ask','-o','PubkeyAuthentication=no',
              '-o','PreferredAuthentications=keyboard-interactive,password','-o','NumberOfPasswordPrompts=3',$server,$command)
    if ((Invoke-ConsoleCommand $Ssh $enroll) -ne 0) {
        throw '公钥登记未完成。请核对当前密码及服务器是否允许密码登录；本机密钥已保留，下次双击可以重试。'
    }
    Write-Host '公钥已登记，正在验证本机密钥登录；如设置过本机密钥口令，请按提示输入。'
    $verify = $common + $options + @('-o','StrictHostKeyChecking=yes','-o','PasswordAuthentication=no',
              '-o','KbdInteractiveAuthentication=no','-o','PreferredAuthentications=publickey',$server,
              ('test "$(id -un)" = ''' + $username + ''' && test "$(id -u)" != 0'))
    if ((Invoke-ConsoleCommand $Ssh $verify) -ne 0) {
        throw '已登记公钥，但密钥登录验证失败，尚未保存连接设置。请保留报错交给管理员，不要反复新建密钥。'
    }
    Save-ConnectionProfile $ConfigPath $connection
    Write-Host '首次配置完成，密码未保存。正在为自己的账号打开 ResearchKit。'
    return $connection
}
