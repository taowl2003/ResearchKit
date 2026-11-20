param([string]$Repository, [string]$Temporary, [string]$Python)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
function Assert($Condition, [string]$Message) { if (-not $Condition) { throw $Message } }
function Rejects([scriptblock]$Action, [string]$Message) {
    $rejected = $false
    try { $null = & $Action } catch { $rejected = $true }
    Assert $rejected $Message
}
. (Join-Path $Repository 'tools/windows/ResearchKit-FirstUse.ps1')
# Parse the actual launcher and import only its functions, without launching SSH.
$tokens = $null; $parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile((Join-Path $Repository 'tools/windows/ResearchKit.ps1'), [ref]$tokens, [ref]$parseErrors)
Assert ($parseErrors.Count -eq 0) 'launcher parse errors'
foreach ($node in $ast.FindAll({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] }, $false)) {
    Invoke-Expression $node.Extent.Text
}
Invoke-SelfTest

# Execute real child processes to check argument quoting, including empty args.
$capture = Join-Path $Temporary 'argv.json'
$probe = Join-Path $Temporary 'argument probe.py'
[IO.File]::WriteAllText($probe, 'import json,sys; open(sys.argv[1], "w").write(json.dumps(sys.argv[2:]))')
$values = @('', 'C:\Windows user\key', 'embedded "quote"', 'trailing\', "line1`nline2", '中文')
Assert ((Invoke-ConsoleCommand $Python (@($probe, $capture) + $values)) -eq 0) 'native process failed'
$actual = [IO.File]::ReadAllText($capture) | ConvertFrom-Json
Assert ($actual.Count -eq $values.Count) 'native argument count'
for ($i=0; $i -lt $values.Count; $i++) { Assert ($actual[$i] -ceq $values[$i]) "native argument $i changed" }

$header = [byte[]](0,0,0,11,115,115,104,45,101,100,50,53,53,49,57,0,0,0,32)
$publicKey = 'ssh-ed25519 ' + [Convert]::ToBase64String([byte[]]($header + ([byte[]]::new(32))))
foreach ($bad in @('test;touch bad', '-lroot', 'root', '', "test`nother")) {
    Rejects { Get-EnrollmentCommand $publicKey $bad } 'unsafe username accepted'
}
foreach ($bad in @("$publicKey`nssh-ed25519 AAAA", 'ssh-ed25519 AAAA', 'ssh-rsa AAAA', 'ssh-ed25519 abcd;id')) {
    Rejects { Get-EnrollmentCommand $bad 'test' } 'unsafe public key accepted'
}
$command = Get-EnrollmentCommand ($publicKey + ' $(touch BAD); ''" injected comment') ([Environment]::UserName)
Assert (-not $command.Contains('injected comment')) 'comment crossed shell boundary'

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    # The exact generated remote command runs as this unprivileged test user.
    $remoteHome = Join-Path $Temporary 'remote user'
    $sshDirectory = Join-Path $remoteHome '.ssh'
    $null = New-Item -ItemType Directory -Path $sshDirectory -Force
    $authorized = Join-Path $sshDirectory 'authorized_keys'
    [IO.File]::WriteAllText($authorized, 'preserved-existing-key-without-newline')
    & {
        Assert ((Invoke-ConsoleCommand '/usr/bin/env' @("HOME=$remoteHome",'/bin/sh','-c',$command)) -eq 0) 'remote enrollment failed'
        Assert ((Invoke-ConsoleCommand '/usr/bin/env' @("HOME=$remoteHome",'/bin/sh','-c',$command)) -eq 0) 'repeat enrollment failed'
        $content = [IO.File]::ReadAllText($authorized)
        Assert ($content.StartsWith('preserved-existing-key-without-newline')) 'old key lost'
        Assert (([regex]::Matches($content, [regex]::Escape($publicKey))).Count -eq 1) 'duplicate key'
        $outside = Join-Path $Temporary 'outside.txt'
        [IO.File]::WriteAllText($outside, 'unchanged')
        Remove-Item -LiteralPath $authorized
        $null = New-Item -ItemType SymbolicLink -Path $authorized -Target $outside
        Assert ((Invoke-ConsoleCommand '/usr/bin/env' @("HOME=$remoteHome",'/bin/sh','-c',$command)) -ne 0) 'authorized_keys symlink accepted'
        Assert ([IO.File]::ReadAllText($outside) -ceq 'unchanged') 'symlink target changed'
    }
}

# Simulate authentication outcomes, keeping real profile/key filesystem work.
$script:answers = [Collections.Generic.Queue[string]]::new()
$script:calls = [Collections.Generic.List[object]]::new()
$script:failure = ''
function Read-Host([string]$Prompt) { return $script:answers.Dequeue() }
function Get-Command { return [pscustomobject]@{Source='test-keygen.exe'} }
function Invoke-ConsoleCommand([string]$Executable, [string[]]$Arguments) {
    $script:calls.Add([pscustomobject]@{exe=$Executable; arguments=$Arguments})
    if ($Executable -eq 'test-keygen.exe') {
        $path = $Arguments[[Array]::IndexOf($Arguments, '-f') + 1]
        Assert (-not (Test-Path -LiteralPath $path)) 'existing private key overwritten'
        [IO.File]::WriteAllText($path, 'PRIVATE-LOCAL-ONLY')
        [IO.File]::WriteAllText($path + '.pub', $publicKey + ' test')
        return 0
    }
    if ($script:failure -eq 'enroll' -and $Arguments -contains 'PubkeyAuthentication=no') { return 1 }
    if ($script:failure -eq 'verify' -and $Arguments -contains 'PreferredAuthentications=publickey') { return 1 }
    return 0
}
function Answers([string[]]$Values) {
    $script:answers.Clear()
    foreach ($value in $Values) { $script:answers.Enqueue($value) }
    $script:calls.Clear()
}
$env:USERPROFILE = Join-Path $Temporary 'Windows user 中文'
$config = Join-Path $Temporary 'connection.json'
Answers @('', 'test', '', '')
$profile = Initialize-ResearchKitConnection 'test-ssh.exe' $config
Assert ($profile.username -eq 'test' -and $profile.sshPort -eq 8222) 'wrong profile account'
Assert ($script:calls.Count -eq 3) 'keygen/enrollment/verification sequence'
Assert ($script:calls[1].arguments -contains 'PubkeyAuthentication=no') 'password bootstrap missing'
Assert ($script:calls[2].arguments -contains 'PasswordAuthentication=no') 'verification allows password fallback'
$rawConfig = [IO.File]::ReadAllText($config)
Assert (-not $rawConfig.Contains('password') -and -not $rawConfig.Contains('PRIVATE-LOCAL-ONLY')) 'secret persisted'
Assert (-not (($script:calls | ConvertTo-Json -Depth 5).Contains('PRIVATE-LOCAL-ONLY'))) 'private key sent'
$launch = New-SshArguments $profile.target 54321 $profile
Assert ($launch.Contains('"-l" "test"') -and $launch.Contains('"-p" "8222"')) 'daily connection lost account'
Assert ($launch.Contains('"' + $profile.identityFile + '"')) 'daily connection lost identity'

Answers @('', 'test', '', '')
$null = Initialize-ResearchKitConnection 'test-ssh.exe' $config
Assert ($script:calls.Count -eq 2) 'repeat setup recreated a key'
foreach ($failure in @('enroll','verify')) {
    $script:failure = $failure
    Answers @('', 'test', '', '')
    Rejects { Initialize-ResearchKitConnection 'test-ssh.exe' $config } 'authentication failure accepted'
    Assert ([IO.File]::ReadAllText($config) -ceq $rawConfig) 'failed setup changed previous profile'
    $absent = Join-Path $Temporary ($failure + '.json')
    Answers @('', 'test', '', '')
    Rejects { Initialize-ResearchKitConnection 'test-ssh.exe' $absent } 'new failed setup accepted'
    Assert (-not (Test-Path -LiteralPath $absent)) 'failed authentication saved a profile'
}
$script:failure = ''
Answers @('svr-dl')
$legacy = Initialize-ResearchKitConnection 'test-ssh.exe' $config
Assert ($legacy.target -eq 'svr-dl' -and $script:calls.Count -eq 0) 'alias mode changed keys or server'
Assert ((New-SshArguments 'svr-dl' 54321 $legacy).StartsWith('-tt ')) 'legacy connection changed'
Write-Host 'First-use tests passed: native arguments, bounded enrollment, idempotency, authentication failures, legacy aliases.'
