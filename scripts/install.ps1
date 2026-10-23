<#
.SYNOPSIS
    OnCue installer for Windows 10 (version 2004 or newer) and Windows 11.

.DESCRIPTION
    One command that turns a copy of the OnCue repository into a working install:

      1. Preflight (changes nothing): Windows version, CPU and cores, RAM, free disk
         space, Python 3.11-3.14 (64-bit), and whether the download sources answer.
      2. A virtual environment in .venv and the pip extras, in the order INSTALL.md
         prescribes (including the --only-binary route for litellm).
      3. A prebuilt whisper.cpp from the official ggml-org/whisper.cpp releases,
         pinned to one version and checked against its published sha256.
      4. The GGML transcription model, pinned to one revision and checked against
         its published sha256.
      5. A .env with the Windows defaults. An existing .env is never overwritten.
      6. A final check, including a short transcription self-test, and the exact
         line to start OnCue.

    Everything lands inside the project folder: .venv, .env and .oncue-win\ (the
    downloads, the pip cache, the install log and the install manifest). The
    script needs no administrator rights, changes no registry value, no PATH and
    no execution policy. Start it with -ExecutionPolicy Bypass on the
    powershell.exe command line; that setting lives only as long as that one
    process.

    -Uninstall removes what this script put down, and only that: .oncue-win\,
    .venv when this script created it, and .env when this script created it and
    it is unchanged since. It lists everything first and asks for confirmation.

.PARAMETER InstallProfile
    notetaker (default): recording and transcription, the minimum from INSTALL.md
    (".[windows]") plus aiosqlite, which the post-call report needs to write the
    transcript to disk.
    full: also pain-point detection (".[detector]" with the litellm wheel route).
    Alias: -Profile.

.PARAMETER Model
    The whisper.cpp GGML model. Default large-v3-turbo-q8_0, the value pick on CPU
    in docs/BENCHMARKS.md.

.PARAMETER PreflightOnly
    Run only the preflight. Changes nothing, writes nothing.

.PARAMETER Uninstall
    Remove what this script installed, after confirmation.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1 -Profile full

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1 -PreflightOnly

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [Alias('Profile')]
    [ValidateSet('notetaker', 'full')]
    [string]$InstallProfile = 'notetaker',

    [ValidateSet('large-v3-turbo-q8_0', 'large-v3-turbo-q5_0', 'large-v3-turbo', 'large-v3',
        'medium', 'medium-q8_0', 'small', 'small-q8_0', 'base')]
    [string]$Model = 'large-v3-turbo-q8_0',

    [switch]$PreflightOnly,

    [switch]$Uninstall
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
# ValidateSet accepts any casing; .env and the file names use lower case.
$InstallProfile = $InstallProfile.ToLowerInvariant()
$Model = $Model.ToLowerInvariant()

# ---------------------------------------------------------------------------
# Pinned sources
# ---------------------------------------------------------------------------
# whisper.cpp v1.9.1 is the release the 2026-07-26 physical-hardware Windows run
# used (INSTALL.md, Windows). v1.9.3 and later tags carry no binaries; those
# moved to build-number releases. The release ships no SHA256SUMS file; the
# sha256 below is the digest GitHub publishes for this exact release asset
# (GET /repos/ggml-org/whisper.cpp/releases/tags/v1.9.1, field "digest").
$WhisperCppVersion = 'v1.9.1'
$WhisperCppAsset = 'whisper-bin-x64.zip'
$WhisperCppSha256 = '7d8be46ecd31828e1eb7a2ecdd0d6b314feafd82163038ab6092594b0a063539'
$WhisperCppUrl = 'https://github.com/ggml-org/whisper.cpp/releases/download/' + $WhisperCppVersion + '/' + $WhisperCppAsset

# GGML models from huggingface.co/ggerganov/whisper.cpp, pinned to one repo
# revision. The sha256 values are the LFS object ids Hugging Face publishes for
# these files at that revision.
$ModelRevision = '5359861c739e955e79d9a303bcbc70fb988958b1'
$ModelCatalog = [ordered]@{
    'large-v3-turbo-q8_0' = @{ File = 'ggml-large-v3-turbo-q8_0.bin'; Bytes = 874188075; Sha256 = '317eb69c11673c9de1e1f0d459b253999804ec71ac4c23c17ecf5fbe24e259a1' }
    'large-v3-turbo-q5_0' = @{ File = 'ggml-large-v3-turbo-q5_0.bin'; Bytes = 574041195; Sha256 = '394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2' }
    'large-v3-turbo'      = @{ File = 'ggml-large-v3-turbo.bin'; Bytes = 1624555275; Sha256 = '1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69' }
    'large-v3'            = @{ File = 'ggml-large-v3.bin'; Bytes = 3095033483; Sha256 = '64d182b440b98d5203c4f9bd541544d84c605196c4f7b845dfa11fb23594d1e2' }
    'medium'              = @{ File = 'ggml-medium.bin'; Bytes = 1533763059; Sha256 = '6c14d5adee5f86394037b4e4e8b59f1673b6cee10e3cf0b11bbdbee79c156208' }
    'medium-q8_0'         = @{ File = 'ggml-medium-q8_0.bin'; Bytes = 823369779; Sha256 = '42a1ffcbe4167d224232443396968db4d02d4e8e87e213d3ee2e03095dea6502' }
    'small'               = @{ File = 'ggml-small.bin'; Bytes = 487601967; Sha256 = '1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b' }
    'small-q8_0'          = @{ File = 'ggml-small-q8_0.bin'; Bytes = 264464607; Sha256 = '49c8fb02b65e6049d5fa6c04f81f53b867b5ec9540406812c643f177317f779f' }
    'base'                = @{ File = 'ggml-base.bin'; Bytes = 147951465; Sha256 = '60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe' }
}
$ModelBaseUrl = 'https://huggingface.co/ggerganov/whisper.cpp/resolve/' + $ModelRevision + '/'
$PyPiUrl = 'https://pypi.org/simple/pip/'

# pip steps per profile, in the order INSTALL.md (Windows) prescribes. Installed
# editable (-e) so OnCue keeps its data inside the project folder: an editable
# install finds pyproject.toml and resolves data/ and .vnx-data/ there, a plain
# install writes them to %LOCALAPPDATA%\SalesCopilot instead.
# Every pip step installs against constraints.txt, which pins each package to the
# version uv.lock (and so CI) tests. It is a relative name on purpose: the pip
# steps run with the repository root as the current folder (Push-Location below).
$ConstraintsFile = 'constraints.txt'
$PipSteps = @{
    'notetaker' = @(
        @{ Label = 'OnCue with the Windows capture extra'; Args = @('install', '-e', '.[windows]', '-c', $ConstraintsFile) },
        @{ Label = 'aiosqlite (post-call report and saved transcript)'; Args = @('install', 'aiosqlite', '--only-binary=:all:', '-c', $ConstraintsFile) }
    )
    'full'      = @(
        @{ Label = 'OnCue with the Windows capture extra'; Args = @('install', '-e', '.[windows]', '-c', $ConstraintsFile) },
        @{ Label = 'litellm from its prebuilt wheel'; Args = @('install', 'litellm>=1.83.10', '--only-binary=:all:', '-c', $ConstraintsFile) },
        @{ Label = 'pain-point detection extra'; Args = @('install', '-e', '.[detector]', '-c', $ConstraintsFile) },
        @{ Label = 'aiosqlite (post-call report and saved transcript)'; Args = @('install', 'aiosqlite', '--only-binary=:all:', '-c', $ConstraintsFile) }
    )
}

# Disk space needed besides the model: venv, pip cache during install, and for
# full the embedding model OnCue downloads on the first call with detection.
$BaseDiskBytes = @{ 'notetaker' = 2.5GB; 'full' = 4.5GB }

$UserAgent = 'OnCue-Windows-installer'
$ManifestSchema = 1

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
$Root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).ProviderPath.TrimEnd('\')
$StateDir = Join-Path $Root '.oncue-win'
$VenvDir = Join-Path $Root '.venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
$EnvFile = Join-Path $Root '.env'
$EnvExample = Join-Path $Root '.env.example'
$ManifestPath = Join-Path $StateDir 'install-manifest.json'
$WhisperDir = Join-Path (Join-Path $StateDir 'whisper.cpp') $WhisperCppVersion
$ModelDir = Join-Path $StateDir 'models'
$DownloadDir = Join-Path $StateDir 'downloads'
$PipCacheDir = Join-Path $StateDir 'pip-cache'
$TempDir = Join-Path $StateDir 'tmp'
$LogDir = Join-Path $StateDir 'logs'

# Locations outside the project folder that an OnCue install or run could
# touch. The installer itself writes to none of them: pip's cache and temp
# files are redirected into .oncue-win for the length of the run. The manifest
# records whether each existed before the first install so -Uninstall can say
# what appeared since.
function Get-OutsidePaths {
    return @(
        (Join-Path $env:LOCALAPPDATA 'SalesCopilot'),
        (Join-Path $env:LOCALAPPDATA 'pip\Cache'),
        (Join-Path $env:USERPROFILE '.cache\huggingface\hub\models--sentence-transformers--paraphrase-multilingual-MiniLM-L12-v2'),
        (Join-Path $env:USERPROFILE '.cache\torch')
    )
}

function New-Directory {
    param([string]$Path)
    # .NET instead of New-Item: no wildcard parsing of [ ] in folder names, and
    # no error when the folder already exists.
    [void][System.IO.Directory]::CreateDirectory($Path)
}

function Remove-PathWithRetry {
    param([string]$Path)
    # Remove-Item -Recurse in Windows PowerShell 5.1 can stop on "directory is
    # not empty" while a virus scanner still holds a just-deleted file.
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        if (-not (Test-Path -LiteralPath $Path)) { return }
        try {
            Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
            return
        }
        catch {
            if ($attempt -eq 3) { throw }
            Start-Sleep -Seconds 2
        }
    }
}

function Move-DirectoryWithRetry {
    param([string]$Source, [string]$Destination)
    # NTFS refuses to rename a folder while a virus scanner still has one of the
    # freshly extracted files open.
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        try {
            [System.IO.Directory]::Move($Source, $Destination)
            return
        }
        catch {
            if ($attempt -eq 5) { throw }
            Start-Sleep -Seconds 2
        }
    }
}

# ---------------------------------------------------------------------------
# Output and log
# ---------------------------------------------------------------------------
$script:LogWriter = $null
$script:PendingLog = New-Object System.Collections.Generic.List[string]
$script:LogPath = ''

function Write-LogLine {
    param([string]$Text)
    if ($null -ne $script:LogWriter) {
        $script:LogWriter.WriteLine($Text)
    }
    else {
        $script:PendingLog.Add($Text)
    }
}

function Open-InstallLog {
    New-Directory -Path $LogDir
    $script:LogPath = Join-Path $LogDir ('install-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
    $encoding = New-Object System.Text.UTF8Encoding($false)
    $script:LogWriter = New-Object System.IO.StreamWriter -ArgumentList $script:LogPath, $true, $encoding
    $script:LogWriter.AutoFlush = $true
    foreach ($line in $script:PendingLog) {
        $script:LogWriter.WriteLine($line)
    }
    $script:PendingLog.Clear()
}

function Close-InstallLog {
    if ($null -ne $script:LogWriter) {
        $script:LogWriter.Dispose()
        $script:LogWriter = $null
    }
}

function Write-Title {
    param([string]$Text)
    Write-Host ''
    Write-Host $Text -ForegroundColor White
    Write-LogLine ''
    Write-LogLine ('[' + (Get-Date -Format 'HH:mm:ss') + '] ' + $Text)
}

function Write-Ok {
    param([string]$Text)
    Write-Host ('  OK    ' + $Text) -ForegroundColor Green
    Write-LogLine ('  OK    ' + $Text)
}

function Write-Warn {
    param([string]$Text)
    Write-Host ('  WARN  ' + $Text) -ForegroundColor Yellow
    Write-LogLine ('  WARN  ' + $Text)
}

function Write-Fail {
    param([string]$Text)
    Write-Host ('  FAIL  ' + $Text) -ForegroundColor Red
    Write-LogLine ('  FAIL  ' + $Text)
}

function Write-Info {
    param([string]$Text)
    Write-Host ('        ' + $Text)
    Write-LogLine ('        ' + $Text)
}

function Format-Size {
    param([double]$Bytes)
    if ($Bytes -ge 1GB) { return ('{0:N1} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N0} MB' -f ($Bytes / 1MB)) }
    return ('{0:N0} KB' -f ($Bytes / 1KB))
}

# ---------------------------------------------------------------------------
# Native commands
# ---------------------------------------------------------------------------
# Windows PowerShell 5.1 turns redirected stderr of a native command into error
# records, and with ErrorActionPreference Stop the first one would abort the
# script (pip prints warnings on stderr). Both helpers run with a local
# 'Continue' and read the outcome from the exit code instead.

function Invoke-Probe {
    param([string]$FilePath, [string]$ArgumentLine, [int]$TimeoutMs)
    # For read-only probes of programs that are not ours yet (any python.exe on
    # the machine, including Microsoft Store aliases). stdin is closed at once
    # so a prompt cannot wait for an answer nobody sees, output stays in
    # memory, and a program that hangs or cannot start counts as "does not run".
    try {
        $startInfo = New-Object System.Diagnostics.ProcessStartInfo
        $startInfo.FileName = $FilePath
        $startInfo.Arguments = $ArgumentLine
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardInput = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        $process = [System.Diagnostics.Process]::Start($startInfo)
        $process.StandardInput.Close()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutMs)) {
            try { $process.Kill() } catch { Write-LogLine ('probe kill failed: ' + $_.Exception.Message) }
            Write-LogLine ('probe timed out: ' + $FilePath)
            return @{ ExitCode = -1; Output = '' }
        }
        $process.WaitForExit()
        Write-LogLine ('probe ' + $FilePath + ' exit ' + $process.ExitCode + ' ' + $stderr.Result.Trim())
        return @{ ExitCode = $process.ExitCode; Output = $stdout.Result }
    }
    catch {
        Write-LogLine ('probe failed to start ' + $FilePath + ': ' + $_.Exception.Message)
        return @{ ExitCode = -1; Output = '' }
    }
}

function Invoke-Logged {
    param([string]$FilePath, [string[]]$Arguments, [string]$EchoPattern)
    $ErrorActionPreference = 'Continue'
    Write-LogLine ('$ "' + $FilePath + '" ' + ($Arguments -join ' '))
    $tail = New-Object System.Collections.Generic.Queue[string]
    & $FilePath @Arguments 2>&1 | ForEach-Object {
        $line = [string]$_
        Write-LogLine $line
        $tail.Enqueue($line)
        if ($tail.Count -gt 30) { [void]$tail.Dequeue() }
        if ($EchoPattern -and ($line -match $EchoPattern)) {
            if ($line.Length -gt 110) { $line = $line.Substring(0, 107) + '...' }
            Write-Host ('          ' + $line) -ForegroundColor DarkGray
        }
    }
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        Write-Host '        Last lines of output:' -ForegroundColor Yellow
        foreach ($tailLine in $tail) {
            Write-Host ('        | ' + $tailLine) -ForegroundColor Yellow
        }
    }
    return $code
}

function Invoke-WithTimeout {
    param([string]$FilePath, [string]$ArgumentLine, [int]$TimeoutSeconds, [string]$Name)
    $stdoutPath = Join-Path $LogDir ($Name + '.stdout.txt')
    $stderrPath = Join-Path $LogDir ($Name + '.stderr.txt')
    Write-LogLine ('$ "' + $FilePath + '" ' + $ArgumentLine)
    $process = Start-Process -FilePath $FilePath -ArgumentList $ArgumentLine -NoNewWindow -PassThru `
        -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
    # Windows PowerShell 5.1 only keeps ExitCode when the handle was opened
    # before the process exits.
    $null = $process.Handle
    $timedOut = -not $process.WaitForExit($TimeoutSeconds * 1000)
    if ($timedOut) {
        try { $process.Kill() } catch { Write-LogLine ('kill failed: ' + $_.Exception.Message) }
    }
    $process.WaitForExit()
    $stdout = [string](Get-Content -LiteralPath $stdoutPath -Raw -ErrorAction SilentlyContinue)
    $stderr = [string](Get-Content -LiteralPath $stderrPath -Raw -ErrorAction SilentlyContinue)
    Write-LogLine $stdout
    Write-LogLine $stderr
    $exitCode = $null
    if (-not $timedOut) { $exitCode = $process.ExitCode }
    return @{ TimedOut = $timedOut; ExitCode = $exitCode; StdOut = $stdout; StdErr = $stderr }
}

function Get-ExitCodeHint {
    param($ExitCode)
    if ($ExitCode -eq -1073741515) {
        return ('a DLL is missing (0xC0000135). Most likely the Microsoft Visual C++ ' +
            'Redistributable (x64). Installing it needs administrator rights once: ' +
            'https://aka.ms/vs/17/release/vc_redist.x64.exe, then run this installer again.')
    }
    if ($ExitCode -eq -1073741795) {
        return ('the CPU does not support an instruction this whisper.cpp build uses ' +
            '(0xC000001D, illegal instruction). Report this together with the CPU line from the preflight.')
    }
    return ('exit code ' + $ExitCode + '.')
}

# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------
function New-WebRequest {
    param([string]$Url, [string]$Method, [int]$TimeoutMs)
    $request = [System.Net.WebRequest]::Create($Url)
    $request.Method = $Method
    $request.Timeout = $TimeoutMs
    $request.ReadWriteTimeout = 120000
    $request.UserAgent = $UserAgent
    if ($null -ne $request.Proxy) {
        $request.Proxy.Credentials = [System.Net.CredentialCache]::DefaultCredentials
    }
    return $request
}

function Test-UrlReachable {
    param([string]$Url)
    try {
        $response = (New-WebRequest -Url $Url -Method 'HEAD' -TimeoutMs 20000).GetResponse()
        $status = [int]$response.StatusCode
        $response.Close()
        return @{ Ok = $true; Detail = ('HTTP ' + $status) }
    }
    catch {
        return @{ Ok = $false; Detail = $_.Exception.Message }
    }
}

function Get-Sha256 {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Invoke-VerifiedDownload {
    param([string]$Url, [string]$Destination, [string]$ExpectedSha256)
    $partial = $Destination + '.partial'
    if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial -Force }
    Write-LogLine ('download ' + $Url)
    $response = (New-WebRequest -Url $Url -Method 'GET' -TimeoutMs 60000).GetResponse()
    try {
        $total = $response.ContentLength
        $inStream = $response.GetResponseStream()
        $outStream = [System.IO.File]::Create($partial)
        try {
            $buffer = New-Object byte[] 1048576
            $done = [long]0
            $nextReport = 10
            while (($read = $inStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                $outStream.Write($buffer, 0, $read)
                $done += $read
                if ($total -gt 0) {
                    $percent = [int][Math]::Floor(100.0 * $done / $total)
                    if ($percent -ge $nextReport) {
                        Write-Host ('          {0,3}%  {1} of {2}' -f $percent, (Format-Size $done), (Format-Size $total)) -ForegroundColor DarkGray
                        $nextReport = ([Math]::Floor($percent / 10) + 1) * 10
                    }
                }
            }
        }
        finally {
            $outStream.Dispose()
            $inStream.Dispose()
        }
    }
    finally {
        $response.Close()
    }
    $actual = Get-Sha256 -Path $partial
    if ($actual -ne $ExpectedSha256) {
        Remove-Item -LiteralPath $partial -Force
        throw ('Checksum mismatch for ' + $Url + ': expected sha256 ' + $ExpectedSha256 + ', got ' + $actual +
            '. The file was removed. Run the installer again; if it repeats, report it.')
    }
    if (Test-Path -LiteralPath $Destination) { Remove-Item -LiteralPath $Destination -Force }
    [System.IO.File]::Move($partial, $Destination)
    Write-LogLine ('sha256 ok ' + $actual)
}

# ---------------------------------------------------------------------------
# System facts (read-only)
# ---------------------------------------------------------------------------
function Get-RegistryValue {
    param([string]$Path, [string]$Name)
    try {
        $item = Get-ItemProperty -LiteralPath $Path -Name $Name -ErrorAction Stop
        return $item.$Name
    }
    catch {
        return $null
    }
}

function Get-OsArchitecture {
    if ($env:PROCESSOR_ARCHITEW6432) { return [string]$env:PROCESSOR_ARCHITEW6432 }
    return [string]$env:PROCESSOR_ARCHITECTURE
}

function Get-CpuFacts {
    $name = 'unknown CPU'
    $physical = 0
    try {
        $cpus = @(Get-CimInstance -ClassName Win32_Processor -ErrorAction Stop)
        if ($cpus.Count -gt 0) { $name = ([string]$cpus[0].Name).Trim() }
        foreach ($cpu in $cpus) { $physical += [int]$cpu.NumberOfCores }
    }
    catch {
        Write-LogLine ('Win32_Processor query failed: ' + $_.Exception.Message)
    }
    $logical = [Environment]::ProcessorCount
    if ($physical -le 0) { $physical = [Math]::Max(1, [int][Math]::Floor($logical / 2)) }
    # One core stays free for audio capture, VAD and the browser. Above eight
    # threads whisper.cpp gains little on a laptop CPU.
    $threads = [Math]::Max(2, [Math]::Min(8, $physical - 1))
    return @{ Name = $name; Physical = $physical; Logical = $logical; Threads = $threads }
}

function Get-PythonFacts {
    param([string]$Executable)
    # -I ignores PYTHON* variables, the user site and the current folder; -B
    # writes no .pyc files. Running a python.exe directly never installs
    # anything, unlike asking the "py" launcher for a version it does not have
    # (the Python install manager can then offer to install it).
    $probe = "import sys, sysconfig; v = sys.version_info; print('{0}.{1}.{2}|{3}'.format(v[0], v[1], v[2], sysconfig.get_platform()))"
    $result = Invoke-Probe -FilePath $Executable -ArgumentLine ('-I -B -c "' + $probe + '"') -TimeoutMs 15000
    $lines = @(([string]$result.Output) -split "`r?`n" | Where-Object { $_ -match '^\d+\.\d+\.\d+\|' })
    if ($result.ExitCode -ne 0 -or $lines.Count -eq 0) {
        return @{ Runs = $false; Version = $null; Platform = ''; Supported = $false }
    }
    $parts = ([string]$lines[0]).Split('|')
    $version = [version]$parts[0]
    $supported = ($version.Major -eq 3 -and $version.Minor -ge 11 -and $version.Minor -le 14 -and $parts[1] -eq 'win-amd64')
    return @{ Runs = $true; Version = $version; Platform = $parts[1]; Supported = $supported }
}

function Get-PythonCandidatePaths {
    $paths = New-Object System.Collections.Generic.List[string]
    # PEP 514 registrations: what the python.org installer, the Python install
    # manager and the Microsoft Store build write, and what py.exe reads.
    foreach ($hive in @('HKCU:\Software\Python', 'HKLM:\Software\Python')) {
        if (-not (Test-Path -LiteralPath $hive)) { continue }
        foreach ($company in @(Get-ChildItem -LiteralPath $hive -ErrorAction SilentlyContinue)) {
            foreach ($tag in @(Get-ChildItem -LiteralPath $company.PSPath -ErrorAction SilentlyContinue)) {
                $installKey = $tag.OpenSubKey('InstallPath')
                if ($null -eq $installKey) { continue }
                try {
                    $executable = [string]$installKey.GetValue('ExecutablePath')
                    if (-not $executable) {
                        $installDir = [string]$installKey.GetValue('')
                        # Path.Combine, not Join-Path: Join-Path throws in 5.1 when a stale
                        # registration points at a drive that no longer exists.
                        if ($installDir) { $executable = [System.IO.Path]::Combine($installDir, 'python.exe') }
                    }
                }
                finally {
                    $installKey.Close()
                }
                if ($executable) { $paths.Add($executable) }
            }
        }
    }
    # Default install folders, for installs that skipped the registry.
    $patterns = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python3*\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Python\pythoncore-3*\python.exe')
    )
    if ($env:ProgramFiles) { $patterns += (Join-Path $env:ProgramFiles 'Python3*\python.exe') }
    foreach ($pattern in $patterns) {
        foreach ($item in @(Get-ChildItem -Path $pattern -File -ErrorAction SilentlyContinue)) { $paths.Add($item.FullName) }
    }
    # Whatever "python" means on PATH. The Microsoft Store alias stub answers
    # with exit code 9009 and is skipped by the probe.
    foreach ($name in @('python', 'python3')) {
        foreach ($command in @(Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue)) { $paths.Add($command.Path) }
    }
    $unique = New-Object System.Collections.Generic.List[string]
    $seen = @{}
    foreach ($path in $paths) {
        $key = $path.ToLowerInvariant()
        if ($seen.ContainsKey($key) -or -not [System.IO.File]::Exists($path)) { continue }
        $seen[$key] = $true
        $unique.Add($path)
    }
    return $unique
}

function Find-Python {
    # Preference: the newest version OnCue has run on (3.13), then older
    # supported ones, then 3.14.
    $rank = @{ 13 = 0; 12 = 1; 11 = 2; 14 = 3 }
    $rejected = New-Object System.Collections.Generic.List[string]
    $best = $null
    foreach ($path in (Get-PythonCandidatePaths)) {
        $facts = Get-PythonFacts -Executable $path
        if (-not $facts.Runs) { continue }
        if (-not $facts.Supported) {
            $reason = 'OnCue needs 64-bit Python 3.11 to 3.14; PyTorch publishes no Windows wheels outside that'
            $rejected.Add(($path + ': Python ' + $facts.Version + ' for ' + $facts.Platform + ' (' + $reason + ')'))
            continue
        }
        if ($null -eq $best -or $rank[$facts.Version.Minor] -lt $rank[$best.Version.Minor]) {
            $best = @{ Executable = $path; Version = $facts.Version }
        }
    }
    if ($null -eq $best) {
        return @{ Found = $false; Executable = ''; Version = $null; Rejected = $rejected }
    }
    return @{ Found = $true; Executable = $best.Executable; Version = $best.Version; Rejected = $rejected }
}

function Get-VenvState {
    # Reusable: a working 64-bit Python 3.11-3.14 with pip. Owned: this
    # installer created it (install manifest), so it may remove and recreate a
    # .venv that an interrupted earlier run left half-built.
    $state = @{ Exists = $false; Reusable = $false; Owned = $false; Reason = ''; Version = $null }
    if (-not (Test-Path -LiteralPath $VenvDir)) { return $state }
    $state.Exists = $true
    $manifest = Read-Manifest
    $state.Owned = ($null -ne $manifest -and [bool]$manifest['venv_created'])
    if (-not (Test-Path -LiteralPath $VenvPython)) {
        $state.Reason = '.venv exists but has no Scripts\python.exe (for example a macOS or Linux venv copied along, or an interrupted install).'
        return $state
    }
    $facts = Get-PythonFacts -Executable $VenvPython
    $state.Version = $facts.Version
    if (-not $facts.Runs) {
        $state.Reason = '.venv\Scripts\python.exe does not run (its base Python may have been removed).'
        return $state
    }
    if (-not $facts.Supported) {
        $state.Reason = ('.venv uses Python ' + $facts.Version + ' for ' + $facts.Platform + '; OnCue needs 64-bit Python 3.11 to 3.14.')
        return $state
    }
    $pip = Invoke-Probe -FilePath $VenvPython -ArgumentLine '-I -B -m pip --version' -TimeoutMs 30000
    if ($pip.ExitCode -ne 0) {
        $state.Reason = '.venv has no working pip (an interrupted install can leave it like that).'
        return $state
    }
    $state.Reusable = $true
    return $state
}

function Test-InsideOneDrive {
    foreach ($candidate in @($env:OneDrive, $env:OneDriveConsumer, $env:OneDriveCommercial)) {
        if ($candidate -and ($Root + '\').StartsWith($candidate.TrimEnd('\') + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
    }
    return $false
}

function Get-WhisperBinaries {
    $result = @{ Cli = $null; Server = $null }
    if (-not (Test-Path -LiteralPath $WhisperDir)) { return $result }
    $cli = @(Get-ChildItem -LiteralPath $WhisperDir -Recurse -File -Filter 'whisper-cli.exe' -ErrorAction SilentlyContinue)
    $server = @(Get-ChildItem -LiteralPath $WhisperDir -Recurse -File -Filter 'whisper-server.exe' -ErrorAction SilentlyContinue)
    if ($cli.Count -gt 0) { $result.Cli = $cli[0].FullName }
    if ($server.Count -gt 0) { $result.Server = $server[0].FullName }
    return $result
}

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
function Invoke-Preflight {
    $state = @{ Blocking = 0; Warnings = 0; Ok = 0; Cpu = $null; Python = $null; Venv = $null }
    $spec = $ModelCatalog[$Model]

    Write-Title 'OnCue for Windows: preflight (changes nothing)'

    if ($env:OS -ne 'Windows_NT') {
        Write-Fail 'This installer is for Windows. On macOS use: bash scripts/install.sh'
        $state.Blocking++
        return $state
    }

    $psVersion = $PSVersionTable.PSVersion
    if ($psVersion.Major -lt 5 -or ($psVersion.Major -eq 5 -and $psVersion.Minor -lt 1)) {
        Write-Fail ('PowerShell ' + $psVersion + ' found; 5.1 or newer is needed (built into Windows 10 and 11).')
        $state.Blocking++
    }
    else {
        Write-Ok ('PowerShell ' + $psVersion)
        $state.Ok++
    }

    $os = $null
    try { $os = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop } catch { Write-LogLine ('Win32_OperatingSystem query failed: ' + $_.Exception.Message) }
    $build = 0
    $caption = 'Windows'
    if ($null -ne $os) {
        $build = [int]$os.BuildNumber
        $caption = ([string]$os.Caption).Trim()
    }
    else {
        $build = [Environment]::OSVersion.Version.Build
    }
    $displayVersion = Get-RegistryValue -Path 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' -Name 'DisplayVersion'
    if (-not $displayVersion) { $displayVersion = Get-RegistryValue -Path 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' -Name 'ReleaseId' }
    $osText = $caption
    if ($displayVersion) { $osText = $osText + ' ' + $displayVersion }
    $osText = $osText + ' (build ' + $build + ')'
    if ($build -ge 19041) {
        Write-Ok $osText
        $state.Ok++
    }
    else {
        Write-Fail ($osText + ': Windows 10 version 2004 (build 19041) or Windows 11 is needed for WASAPI loopback capture.')
        $state.Blocking++
    }

    $arch = Get-OsArchitecture
    if ($arch -eq 'AMD64') {
        Write-Ok 'Architecture: x64'
        $state.Ok++
    }
    elseif ($arch -eq 'ARM64') {
        Write-Warn ('Architecture: ARM64. Untested. OnCue then runs as x64 under emulation: install the x64 Python ' +
            '(winget install -e --id Python.Python.3.13 --scope user --architecture x64). Transcription will be slower.')
        $state.Warnings++
    }
    else {
        Write-Fail ('Architecture: ' + $arch + '. OnCue needs 64-bit Windows.')
        $state.Blocking++
    }

    $cpu = Get-CpuFacts
    $state.Cpu = $cpu
    $cpuText = ('CPU: ' + $cpu.Name + ', ' + $cpu.Physical + ' cores, ' + $cpu.Logical + ' threads -> WHISPER_CPP_THREADS=' + $cpu.Threads)
    if ($cpu.Physical -lt 4) {
        Write-Warn ($cpuText + '. Fewer than 4 cores: live transcription may fall behind; consider -Model small-q8_0.')
        $state.Warnings++
    }
    else {
        Write-Ok $cpuText
        $state.Ok++
    }

    $ramBytes = [double]0
    try { $ramBytes = [double](Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction Stop).TotalPhysicalMemory } catch { Write-LogLine ('Win32_ComputerSystem query failed: ' + $_.Exception.Message) }
    if ($ramBytes -le 0) {
        Write-Warn 'RAM: could not be read.'
        $state.Warnings++
    }
    elseif ($ramBytes -lt 7.5GB) {
        Write-Warn ('RAM: ' + (Format-Size $ramBytes) + '. 8 GB is the minimum, 16 GB recommended.')
        $state.Warnings++
    }
    else {
        Write-Ok ('RAM: ' + (Format-Size $ramBytes))
        $state.Ok++
    }

    $needBytes = [double]$BaseDiskBytes[$InstallProfile] + [double]$spec.Bytes
    $freeBytes = [double]-1
    $driveName = ''
    try {
        $driveName = [System.IO.Path]::GetPathRoot($Root)
        $freeBytes = [double](New-Object System.IO.DriveInfo -ArgumentList $driveName).AvailableFreeSpace
    }
    catch {
        Write-LogLine ('free space query failed: ' + $_.Exception.Message)
    }
    if ($freeBytes -lt 0) {
        Write-Warn ('Free disk space on ' + $driveName + ' could not be read; about ' + (Format-Size $needBytes) + ' is needed.')
        $state.Warnings++
    }
    elseif ($freeBytes -lt $needBytes) {
        Write-Fail ('Free disk space on ' + $driveName + ': ' + (Format-Size $freeBytes) + '. Profile ' + $InstallProfile + ' with model ' + $Model + ' needs about ' + (Format-Size $needBytes) + '.')
        $state.Blocking++
    }
    else {
        Write-Ok ('Free disk space on ' + $driveName + ': ' + (Format-Size $freeBytes) + ' (needs about ' + (Format-Size $needBytes) + ' for ' + $InstallProfile + ' + ' + $Model + ')')
        $state.Ok++
    }

    $venv = Get-VenvState
    $state.Venv = $venv
    if ($venv.Exists -and $venv.Reusable) {
        Write-Ok ('Existing .venv (Python ' + $venv.Version + ') will be reused')
        $state.Ok++
    }
    elseif ($venv.Exists -and $venv.Owned) {
        Write-Warn ($venv.Reason + ' This installer created it, so it will be removed and created again.')
        $state.Warnings++
    }
    elseif ($venv.Exists) {
        Write-Fail ($venv.Reason + ' This installer did not create it. Rename or remove the .venv folder yourself, then run this installer again.')
        $state.Blocking++
    }

    $python = Find-Python
    $state.Python = $python
    foreach ($line in $python.Rejected) {
        Write-Info ('Skipped ' + $line)
    }
    if ($python.Found) {
        Write-Ok ('Python ' + $python.Version + ' (64-bit): ' + $python.Executable)
        $state.Ok++
    }
    elseif ($venv.Reusable) {
        Write-Warn 'No Python 3.11-3.14 (64-bit) found on this machine; the existing .venv is used as is.'
        $state.Warnings++
    }
    else {
        Write-Fail 'Python 3.11 to 3.14 (64-bit) not found. This installer does not install Python. Install Python 3.13 for your user (no administrator rights needed):'
        Write-Info '  winget install -e --id Python.Python.3.13 --scope user'
        Write-Info 'or download the "Windows installer (64-bit)" from https://www.python.org/downloads/windows/'
        Write-Info 'Then open a NEW PowerShell window and run this installer again.'
        $state.Blocking++
    }

    $longPaths = Get-RegistryValue -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name 'LongPathsEnabled'
    if ($longPaths -ne 1 -and $Root.Length -gt 60) {
        Write-Warn ('The project folder path is ' + $Root.Length + ' characters long and Windows long paths are off. ' +
            'pip can fail on deep package paths. A short folder such as ' + (Join-Path $env:USERPROFILE 'OnCue') + ' avoids this.')
        $state.Warnings++
    }

    if (Test-InsideOneDrive) {
        Write-Warn ('The project folder is inside OneDrive (' + $Root + '). OneDrive would sync several GB of venv and model files. ' +
            'Move the folder outside OneDrive, for example to ' + (Join-Path $env:USERPROFILE 'OnCue') + '.')
        $state.Warnings++
    }

    $runtimeDlls = @('vcruntime140.dll', 'vcruntime140_1.dll', 'msvcp140.dll')
    $missingRuntime = @($runtimeDlls | Where-Object { -not [System.IO.File]::Exists([System.IO.Path]::Combine($env:SystemRoot, 'System32', $_)) })
    if ($missingRuntime.Count -gt 0) {
        Write-Warn ('Microsoft Visual C++ runtime not found (' + ($missingRuntime -join ', ') + ' missing in System32). ' +
            'Unless the whisper.cpp release bundles these files, whisper.cpp cannot start and the installer stops with "a DLL is missing". ' +
            'The runtime needs administrator rights to install, once: https://aka.ms/vs/17/release/vc_redist.x64.exe')
        $state.Warnings++
    }

    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Warn 'This PowerShell runs as administrator. That is not needed; a normal PowerShell window is enough.'
        $state.Warnings++
    }

    $consentRoot = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone'
    $micAll = Get-RegistryValue -Path $consentRoot -Name 'Value'
    $micDesktop = Get-RegistryValue -Path ($consentRoot + '\NonPackaged') -Name 'Value'
    if ($micAll -eq 'Deny' -or $micDesktop -eq 'Deny') {
        Write-Warn ('Windows blocks microphone access for desktop apps. Your own voice will not be captured until you allow it: ' +
            'Settings > Privacy & security > Microphone > "Let desktop apps access your microphone".')
        $state.Warnings++
    }

    $sources = @(
        @{ Name = 'pypi.org (Python packages)'; Url = $PyPiUrl; Skip = $false },
        @{ Name = ('github.com (whisper.cpp ' + $WhisperCppVersion + ')'); Url = $WhisperCppUrl; Skip = ($null -ne (Get-WhisperBinaries).Cli) },
        @{ Name = ('huggingface.co (model ' + $Model + ')'); Url = ($ModelBaseUrl + $spec.File); Skip = (Test-Path -LiteralPath (Join-Path $ModelDir $spec.File)) }
    )
    foreach ($source in $sources) {
        if ($source.Skip) {
            Write-Ok ($source.Name + ': already downloaded')
            $state.Ok++
            continue
        }
        $probe = Test-UrlReachable -Url $source.Url
        if ($probe.Ok) {
            Write-Ok ($source.Name + ': reachable (' + $probe.Detail + ')')
            $state.Ok++
        }
        else {
            Write-Fail ($source.Name + ': not reachable: ' + $probe.Detail)
            $state.Blocking++
        }
    }

    if (Test-Path -LiteralPath $EnvFile) {
        Write-Info '.env exists and will be kept as is.'
    }

    Write-Host ''
    $summary = ('Preflight: ' + $state.Ok + ' OK, ' + $state.Warnings + ' warning(s), ' + $state.Blocking + ' blocking.')
    if ($state.Blocking -gt 0) {
        Write-Host $summary -ForegroundColor Red
    }
    elseif ($state.Warnings -gt 0) {
        Write-Host $summary -ForegroundColor Yellow
    }
    else {
        Write-Host $summary -ForegroundColor Green
    }
    Write-LogLine $summary
    return $state
}

# ---------------------------------------------------------------------------
# Manifest: what this script owns
# ---------------------------------------------------------------------------
function New-Manifest {
    $outside = @()
    foreach ($path in (Get-OutsidePaths)) {
        $outside += , @{ path = $path; existed = (Test-Path -LiteralPath $path) }
    }
    $now = (Get-Date).ToString('s')
    return [ordered]@{
        schema               = $ManifestSchema
        installer            = 'scripts/install.ps1'
        created_at           = $now
        updated_at           = $now
        profile              = $InstallProfile
        model                = $Model
        whisper_cpp          = [ordered]@{ version = $WhisperCppVersion; asset = $WhisperCppAsset; sha256 = $WhisperCppSha256 }
        venv_created         = $false
        env_created_sha256   = ''
        outside_paths_before = $outside
    }
}

function Read-Manifest {
    if (-not (Test-Path -LiteralPath $ManifestPath)) { return $null }
    $raw = [System.IO.File]::ReadAllText($ManifestPath, [System.Text.Encoding]::UTF8)
    $data = ConvertFrom-Json -InputObject $raw
    $schemaProperty = $data.PSObject.Properties['schema']
    if ($null -eq $schemaProperty -or [int]$schemaProperty.Value -ne $ManifestSchema) {
        throw ('Unsupported install manifest at ' + $ManifestPath + ' (expected schema ' + $ManifestSchema + '). It was not written by this version of the installer; nothing was changed.')
    }
    $manifest = New-Manifest
    foreach ($name in @('created_at', 'updated_at', 'profile', 'model', 'venv_created', 'env_created_sha256')) {
        $property = $data.PSObject.Properties[$name]
        if ($null -ne $property) { $manifest[$name] = $property.Value }
    }
    $manifest['venv_created'] = [bool]$manifest['venv_created']
    $manifest['env_created_sha256'] = [string]$manifest['env_created_sha256']
    $outsideProperty = $data.PSObject.Properties['outside_paths_before']
    if ($null -ne $outsideProperty -and $null -ne $outsideProperty.Value) {
        $outside = @()
        foreach ($entry in @($outsideProperty.Value)) {
            $outside += , @{ path = [string]$entry.path; existed = [bool]$entry.existed }
        }
        $manifest['outside_paths_before'] = $outside
    }
    return $manifest
}

function Save-Manifest {
    param($Manifest)
    $Manifest['updated_at'] = (Get-Date).ToString('s')
    $json = ConvertTo-Json -InputObject $Manifest -Depth 6
    $temporary = $ManifestPath + '.tmp'
    [System.IO.File]::WriteAllText($temporary, $json, (New-Object System.Text.UTF8Encoding($false)))
    if (Test-Path -LiteralPath $ManifestPath) {
        # [NullString]::Value: a plain $null would reach .NET as "" and be rejected as a path.
        [System.IO.File]::Replace($temporary, $ManifestPath, [NullString]::Value)
    }
    else {
        [System.IO.File]::Move($temporary, $ManifestPath)
    }
}

# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------
function ConvertTo-EnvPath {
    param([string]$FullPath)
    $prefix = $Root + '\'
    if (-not $FullPath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw ('Path outside the project folder: ' + $FullPath)
    }
    return './' + $FullPath.Substring($prefix.Length).Replace('\', '/')
}

function Write-NewEnvFile {
    param($Overrides, [string[]]$Header)
    $template = [System.IO.File]::ReadAllLines($EnvExample, [System.Text.Encoding]::UTF8)
    $lines = New-Object System.Collections.Generic.List[string]
    foreach ($headerLine in $Header) { $lines.Add($headerLine) }
    $seen = @{}
    foreach ($line in $template) {
        $match = [regex]::Match($line, '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=')
        if ($match.Success -and $Overrides.Contains($match.Groups[1].Value)) {
            $key = $match.Groups[1].Value
            $lines.Add($key + '=' + $Overrides[$key])
            $seen[$key] = $true
            continue
        }
        $lines.Add($line)
    }
    $missing = @($Overrides.Keys | Where-Object { -not $seen.ContainsKey($_) })
    if ($missing.Count -gt 0) {
        $lines.Add('')
        $lines.Add('# --- Added by scripts/install.ps1 -----------------------------')
        foreach ($key in $missing) { $lines.Add($key + '=' + $Overrides[$key]) }
    }
    # CreateNew refuses to touch an existing file: an existing .env is never overwritten.
    $stream = New-Object System.IO.FileStream -ArgumentList $EnvFile, ([System.IO.FileMode]::CreateNew), ([System.IO.FileAccess]::Write)
    $writer = New-Object System.IO.StreamWriter -ArgumentList $stream, (New-Object System.Text.UTF8Encoding($false))
    try {
        foreach ($line in $lines) { $writer.WriteLine($line) }
    }
    finally {
        $writer.Dispose()
    }
}

function Read-EnvValue {
    param([string]$Key)
    if (-not (Test-Path -LiteralPath $EnvFile)) { return $null }
    $value = $null
    foreach ($line in [System.IO.File]::ReadAllLines($EnvFile, [System.Text.Encoding]::UTF8)) {
        $match = [regex]::Match($line, '^\s*' + [regex]::Escape($Key) + '\s*=\s*(.*)$')
        if ($match.Success) { $value = $match.Groups[1].Value.Trim().Trim('"').Trim("'") }
    }
    return $value
}

function Resolve-EnvPath {
    param([string]$Value)
    if (-not $Value) { return $null }
    $native = $Value.Replace('/', '\')
    if ([System.IO.Path]::IsPathRooted($native)) { return $native }
    return [System.IO.Path]::GetFullPath((Join-Path $Root $native))
}

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------
function Invoke-Install {
    param($Preflight)
    $spec = $ModelCatalog[$Model]
    $threads = $Preflight.Cpu.Threads

    New-Directory -Path $StateDir
    New-Directory -Path $TempDir
    foreach ($name in $processEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $processEnvironment[$name], 'Process')
    }
    # Keeps git from listing the installer's folder as untracked.
    $gitignore = Join-Path $StateDir '.gitignore'
    if (-not (Test-Path -LiteralPath $gitignore)) {
        [System.IO.File]::WriteAllText($gitignore, "*`r`n", (New-Object System.Text.UTF8Encoding($false)))
    }
    Open-InstallLog
    $manifest = Read-Manifest
    if ($null -eq $manifest) { $manifest = New-Manifest }
    $manifest['profile'] = $InstallProfile
    $manifest['model'] = $Model
    Save-Manifest -Manifest $manifest

    # --- Python environment and packages -----------------------------------
    Write-Title ('Python environment (.venv) and packages, profile ' + $InstallProfile)
    if ($Preflight.Venv.Exists -and $Preflight.Venv.Owned -and -not $Preflight.Venv.Reusable) {
        Write-Info 'Removing the incomplete .venv an earlier run of this installer left behind ...'
        Remove-PathWithRetry -Path $VenvDir
    }
    if (-not (Test-Path -LiteralPath $VenvDir)) {
        Write-Info ('Creating .venv with Python ' + $Preflight.Python.Version + ' (' + $Preflight.Python.Executable + ') ...')
        # Recorded before creation: an interrupted run then still knows the
        # .venv is its own, and the next run or -Uninstall can remove it.
        $manifest['venv_created'] = $true
        Save-Manifest -Manifest $manifest
        $code = Invoke-Logged -FilePath $Preflight.Python.Executable -Arguments @('-m', 'venv', $VenvDir) -EchoPattern ''
        if ($code -ne 0) {
            Remove-PathWithRetry -Path $VenvDir
            throw ('Creating the virtual environment failed (exit code ' + $code + '). The incomplete .venv was removed.')
        }
        Write-Ok '.venv created'
    }
    else {
        Write-Ok '.venv exists, reusing it'
    }

    Write-Info 'Updating pip ...'
    $code = Invoke-Logged -FilePath $VenvPython -Arguments @('-m', 'pip', 'install', '--upgrade', 'pip') -EchoPattern '^(Successfully installed|ERROR)'
    if ($code -ne 0) { throw ('Updating pip failed (exit code ' + $code + ').') }

    $echo = '^(Collecting|Downloading|Installing collected packages|Successfully installed|ERROR)'
    $steps = $PipSteps[$InstallProfile]
    $stepNumber = 0
    foreach ($step in $steps) {
        $stepNumber++
        Write-Info ('pip step ' + $stepNumber + ' of ' + $steps.Count + ': ' + $step.Label + ' (pip ' + ($step.Args -join ' ') + ')')
        if ($stepNumber -eq 1) { Write-Info 'This step downloads PyTorch and takes several minutes on a normal connection.' }
        $code = Invoke-Logged -FilePath $VenvPython -Arguments (@('-m', 'pip') + $step.Args) -EchoPattern $echo
        if ($code -ne 0) { throw ('pip failed on "' + $step.Label + '" (exit code ' + $code + '). The full output is in the log.') }
    }
    # The pip cache only serves a retry of a failed install; after success it is dead weight.
    Remove-PathWithRetry -Path $PipCacheDir
    Write-Ok ('Packages installed for profile ' + $InstallProfile)

    # --- whisper.cpp -------------------------------------------------------
    Write-Title ('whisper.cpp ' + $WhisperCppVersion + ' (prebuilt, CPU)')
    $binaries = Get-WhisperBinaries
    if ($null -eq $binaries.Cli) {
        New-Directory -Path $DownloadDir
        New-Directory -Path (Split-Path -Parent $WhisperDir)
        $zipPath = Join-Path $DownloadDir $WhisperCppAsset
        Write-Info ('Downloading ' + $WhisperCppUrl)
        Invoke-VerifiedDownload -Url $WhisperCppUrl -Destination $zipPath -ExpectedSha256 $WhisperCppSha256
        Write-Ok ('Downloaded ' + $WhisperCppAsset + ', sha256 matches the digest GitHub publishes for this release asset')
        $extractDir = $WhisperDir + '.partial'
        Remove-PathWithRetry -Path $extractDir
        Expand-Archive -LiteralPath $zipPath -DestinationPath $extractDir -Force -ErrorAction Stop
        Remove-PathWithRetry -Path $WhisperDir
        Move-DirectoryWithRetry -Source $extractDir -Destination $WhisperDir
        Remove-Item -LiteralPath $zipPath -Force
        $binaries = Get-WhisperBinaries
        if ($null -eq $binaries.Cli) { throw ('whisper-cli.exe not found in ' + $WhisperCppAsset + '. Report this; the release layout may have changed.') }
    }
    Write-Ok ('whisper-cli.exe: ' + $binaries.Cli)
    if ($null -ne $binaries.Server) {
        Write-Ok ('whisper-server.exe: ' + $binaries.Server)
    }
    else {
        Write-Warn 'whisper-server.exe is not in this release zip; OnCue uses the slower one-shot whisper-cli path.'
    }
    $help = Invoke-WithTimeout -FilePath $binaries.Cli -ArgumentLine '--help' -TimeoutSeconds 60 -Name 'whisper-cli-help'
    if ($help.TimedOut -or $help.ExitCode -ne 0) {
        if ($help.TimedOut) {
            throw ('whisper-cli.exe --help did not finish within 60 seconds. If Windows showed a dialog about a missing ' +
                'MSVCP140.dll or VCRUNTIME140.dll, the Microsoft Visual C++ Redistributable (x64) is missing. Installing it ' +
                'needs administrator rights once: https://aka.ms/vs/17/release/vc_redist.x64.exe, then run this installer again.')
        }
        throw ('whisper-cli.exe does not start: ' + (Get-ExitCodeHint -ExitCode $help.ExitCode))
    }
    Write-Ok 'whisper-cli.exe starts'

    # --- Model -------------------------------------------------------------
    Write-Title ('Model ' + $Model + ' (' + (Format-Size $spec.Bytes) + ')')
    New-Directory -Path $ModelDir
    $modelPath = Join-Path $ModelDir $spec.File
    $needDownload = $true
    if (Test-Path -LiteralPath $modelPath) {
        Write-Info 'Model file present, verifying its sha256 ...'
        if ((Get-Sha256 -Path $modelPath) -eq $spec.Sha256) {
            $needDownload = $false
            Write-Ok ('Model present and verified: ' + $spec.File)
        }
        else {
            Write-Warn 'Model file does not match its sha256; downloading it again.'
            Remove-Item -LiteralPath $modelPath -Force
        }
    }
    if ($needDownload) {
        $modelUrl = $ModelBaseUrl + $spec.File
        Write-Info ('Downloading ' + $modelUrl)
        Invoke-VerifiedDownload -Url $modelUrl -Destination $modelPath -ExpectedSha256 $spec.Sha256
        Write-Ok ('Downloaded ' + $spec.File + ', sha256 matches the value Hugging Face publishes for revision ' + $ModelRevision.Substring(0, 7))
    }
    Remove-PathWithRetry -Path $DownloadDir

    # --- .env --------------------------------------------------------------
    Write-Title '.env'
    $overrides = [ordered]@{
        'LLM_PROVIDER'           = 'none'
        'AUDIO_CAPTURE_METHOD'   = ''
        'WHISPER_BACKEND'        = 'whisper.cpp'
        'WHISPER_MODEL'          = $Model
        'WHISPER_CPP_BINARY'     = (ConvertTo-EnvPath -FullPath $binaries.Cli)
        'WHISPER_CPP_MODEL_PATH' = (ConvertTo-EnvPath -FullPath $modelPath)
        'WHISPER_CPP_THREADS'    = [string]$threads
    }
    if ($null -ne $binaries.Server) {
        $overrides['WHISPER_CPP_SERVER_BINARY'] = (ConvertTo-EnvPath -FullPath $binaries.Server)
    }
    else {
        $overrides['WHISPER_CPP_SERVER_ENABLED'] = 'false'
    }
    if (-not (Test-Path -LiteralPath $EnvFile)) {
        $header = @(
            ('# Written by scripts/install.ps1 on ' + (Get-Date -Format 'yyyy-MM-dd') + ' (profile ' + $InstallProfile + ', model ' + $Model + ').'),
            '# The installer never overwrites this file. Edit it freely.',
            '# Windows defaults set by the installer: AUDIO_CAPTURE_METHOD empty (wasapi is picked',
            ('# automatically on Windows), LLM_PROVIDER=none, WHISPER_CPP_THREADS=' + $threads + ' (physical cores minus one, 2 to 8),'),
            '# and the whisper.cpp paths below pointing into .oncue-win.',
            ''
        )
        Write-NewEnvFile -Overrides $overrides -Header $header
        $manifest['env_created_sha256'] = Get-Sha256 -Path $EnvFile
        Save-Manifest -Manifest $manifest
        Write-Ok ('.env created from .env.example with the Windows defaults (WHISPER_CPP_THREADS=' + $threads + ')')
    }
    else {
        Write-Ok '.env exists and was left untouched'
        $mismatch = @()
        foreach ($key in @('WHISPER_CPP_BINARY', 'WHISPER_CPP_MODEL_PATH')) {
            $current = Read-EnvValue -Key $key
            $resolved = Resolve-EnvPath -Value $current
            if (-not $resolved -or -not (Test-Path -LiteralPath $resolved)) { $mismatch += $key }
        }
        if ($mismatch.Count -gt 0) {
            Write-Warn ('Your .env points ' + ($mismatch -join ' and ') + ' at a file that does not exist. To use what this installer set up, put these lines in .env:')
            foreach ($key in $overrides.Keys) {
                if ($key -like 'WHISPER_*') { Write-Info ('  ' + $key + '=' + $overrides[$key]) }
            }
        }
    }

    # --- Final check -------------------------------------------------------
    Write-Title 'Final check'
    $imports = 'sales_copilot.__main__, sales_copilot.modules.reports.__main__, soundcard, httpx, aiosqlite'
    if ($InstallProfile -eq 'full') { $imports = $imports + ', sales_copilot.modules.detector.__main__' }
    $code = Invoke-Logged -FilePath $VenvPython -Arguments @('-c', ('import ' + $imports + "; print('imports ok')")) -EchoPattern ''
    if ($code -ne 0) { throw ('OnCue does not import in .venv (exit code ' + $code + ').') }
    Write-Ok ('OnCue imports in .venv (' + $imports + ')')

    $wavPath = Join-Path $StateDir 'selftest-silence.wav'
    Write-SilentWav -Path $wavPath -Seconds 2
    Write-Info 'Transcription self-test: whisper.cpp loads the model and transcribes 2 seconds of silence. This takes up to a minute.'
    $started = Get-Date
    $argumentLine = ('-m "{0}" -f "{1}" -l nl -t {2} -nt -np' -f $modelPath, $wavPath, $threads)
    $selftest = Invoke-WithTimeout -FilePath $binaries.Cli -ArgumentLine $argumentLine -TimeoutSeconds 300 -Name 'whisper-cli-selftest'
    Remove-Item -LiteralPath $wavPath -Force
    $seconds = [int]((Get-Date) - $started).TotalSeconds
    if ($selftest.TimedOut) { throw 'Transcription self-test did not finish within 300 seconds. This CPU is too slow for the chosen model; try -Model small-q8_0.' }
    if ($selftest.ExitCode -ne 0) { throw ('Transcription self-test failed: ' + (Get-ExitCodeHint -ExitCode $selftest.ExitCode)) }
    Write-Ok ('Transcription self-test passed in ' + $seconds + ' s')

    foreach ($key in @('WHISPER_CPP_BINARY', 'WHISPER_CPP_MODEL_PATH')) {
        $resolved = Resolve-EnvPath -Value (Read-EnvValue -Key $key)
        if ($resolved -and (Test-Path -LiteralPath $resolved)) {
            Write-Ok ('.env ' + $key + ' -> ' + $resolved)
        }
        else {
            Write-Warn ('.env ' + $key + ' does not point at an existing file; see the lines above.')
        }
    }
    Remove-PathWithRetry -Path $TempDir

    $quotedRoot = "'" + $Root.Replace("'", "''") + "'"
    Write-Host ''
    Write-Host ('OnCue is installed (profile ' + $InstallProfile + ', model ' + $Model + ').') -ForegroundColor Green
    Write-Host ''
    Write-Host 'Start OnCue with this line in PowerShell:'
    Write-Host ('  Set-Location -LiteralPath ' + $quotedRoot + '; .\.venv\Scripts\python.exe -m sales_copilot') -ForegroundColor Cyan
    Write-Host ''
    Write-Host 'The browser opens the setup wizard; for a call, go to http://localhost:8760/dashboard'
    Write-Host 'Stop OnCue with Ctrl+C in that PowerShell window.'
    if ($InstallProfile -eq 'notetaker') {
        Write-Host ''
        Write-Host 'Notetaker profile: in the dashboard, switch OFF "AI detectie" (AI detection) before you click' -ForegroundColor Yellow
        Write-Host 'Start Call. Detection is not installed in this profile; with it switched on, OnCue stops.' -ForegroundColor Yellow
        Write-Host 'Keep "Live Transcript" and "Post-call Report" on: the report in data\reports holds the transcript.' -ForegroundColor Yellow
    }
    else {
        Write-Host ''
        Write-Host 'Full profile: the first call with detection downloads its embedding model (about 470 MB)'
        Write-Host ('to ' + (Join-Path $env:USERPROFILE '.cache\huggingface') + '. -Uninstall does not remove that cache.')
    }
    Write-Host ''
    Write-Host ('Install log: ' + $script:LogPath)
    Write-Host 'Remove this install again: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1 -Uninstall'
    Write-LogLine 'install finished'
}

function Write-SilentWav {
    param([string]$Path, [int]$Seconds)
    $sampleRate = 16000
    $dataBytes = $sampleRate * 2 * $Seconds
    $ascii = [System.Text.Encoding]::ASCII
    $stream = [System.IO.File]::Create($Path)
    $writer = New-Object System.IO.BinaryWriter -ArgumentList $stream
    try {
        $writer.Write($ascii.GetBytes('RIFF'))
        $writer.Write([int](36 + $dataBytes))
        $writer.Write($ascii.GetBytes('WAVE'))
        $writer.Write($ascii.GetBytes('fmt '))
        $writer.Write([int]16)
        $writer.Write([int16]1)
        $writer.Write([int16]1)
        $writer.Write([int]$sampleRate)
        $writer.Write([int]($sampleRate * 2))
        $writer.Write([int16]2)
        $writer.Write([int16]16)
        $writer.Write($ascii.GetBytes('data'))
        $writer.Write([int]$dataBytes)
        $writer.Write((New-Object byte[] $dataBytes))
    }
    finally {
        $writer.Dispose()
    }
}

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------
function Get-DirectorySize {
    param([string]$Path)
    $total = [double]0
    Get-ChildItem -LiteralPath $Path -Recurse -Force -File -ErrorAction SilentlyContinue | ForEach-Object { $total += $_.Length }
    return $total
}

function Invoke-Uninstall {
    Write-Title 'OnCue for Windows: uninstall'
    $manifest = Read-Manifest
    if ($null -eq $manifest) {
        Write-Info ('No install by this script found in ' + $Root + ' (no .oncue-win\install-manifest.json). Nothing was removed.')
        return 0
    }

    $running = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
            $processPath = $null
            try { $processPath = $_.Path } catch { $processPath = $null }
            $processPath -and (
                $processPath.StartsWith($VenvDir + '\', [System.StringComparison]::OrdinalIgnoreCase) -or
                $processPath.StartsWith($StateDir + '\', [System.StringComparison]::OrdinalIgnoreCase))
        })
    if ($running.Count -gt 0) {
        Write-Fail 'OnCue is still running. Stop it first (Ctrl+C in its PowerShell window), then run -Uninstall again:'
        foreach ($process in $running) { Write-Info ('  ' + $process.ProcessName + ' (pid ' + $process.Id + ')') }
        return 1
    }

    $remove = New-Object System.Collections.Generic.List[object]
    $keep = New-Object System.Collections.Generic.List[string]
    if (Test-Path -LiteralPath $EnvFile) {
        if ($manifest['env_created_sha256'] -and (Get-Sha256 -Path $EnvFile) -eq $manifest['env_created_sha256']) {
            $remove.Add(@{ Path = $EnvFile; What = '.env (created by this installer, unchanged since)'; Directory = $false })
        }
        elseif ($manifest['env_created_sha256']) {
            $keep.Add('.env: created by this installer but edited since; it may hold your own settings or keys. Delete it yourself if you no longer need it.')
        }
        else {
            $keep.Add('.env: it existed before this installer ran.')
        }
    }
    if (Test-Path -LiteralPath $VenvDir) {
        if ($manifest['venv_created']) {
            $remove.Add(@{ Path = $VenvDir; What = '.venv (Python environment created by this installer)'; Directory = $true })
        }
        else {
            $keep.Add('.venv: it existed before this installer ran.')
        }
    }
    $remove.Add(@{ Path = $StateDir; What = '.oncue-win (whisper.cpp, models, install log and manifest)'; Directory = $true })

    Write-Host ''
    Write-Host 'These items will be removed:'
    foreach ($item in $remove) {
        $size = 0
        if ($item.Directory) { $size = Get-DirectorySize -Path $item.Path } else { $size = (Get-Item -LiteralPath $item.Path).Length }
        Write-Host ('  - ' + $item.Path + '  [' + (Format-Size $size) + ']  ' + $item.What)
    }
    Write-Host ''
    Write-Host 'These stay:'
    foreach ($line in $keep) { Write-Host ('  - ' + $line) }
    Write-Host '  - data\ and .vnx-data\: your sessions, reports and transcripts. OnCue writes them while it runs; the installer did not.'
    Write-Host '  - The project folder itself and Python. Remove those yourself when you are done.'
    Write-Host ''
    $answer = Read-Host 'Remove the items listed above? Type y and press Enter to confirm'
    if ($answer -notmatch '^\s*(y|yes|j|ja)\s*$') {
        Write-Info 'Cancelled. Nothing was removed.'
        return 0
    }

    $failed = 0
    foreach ($item in $remove) {
        try {
            Remove-PathWithRetry -Path $item.Path
            Write-Ok ('Removed ' + $item.Path)
        }
        catch {
            Write-Fail ('Could not remove ' + $item.Path + ': ' + $_.Exception.Message)
            $failed++
        }
    }

    Write-Host ''
    Write-Host 'Outside the project folder:'
    foreach ($entry in $manifest['outside_paths_before']) {
        $existsNow = Test-Path -LiteralPath $entry.path
        if (-not $existsNow) {
            Write-Ok ('not present: ' + $entry.path)
        }
        elseif ($entry.existed) {
            Write-Info ('present, but it already existed before the first install: ' + $entry.path)
        }
        else {
            Write-Warn ('appeared after the install: ' + $entry.path + '. The installer does not write there; OnCue or another program created it while running. It was left in place.')
        }
    }
    if ($failed -gt 0) {
        Write-Host ''
        Write-Host ($failed.ToString() + ' item(s) could not be removed; see above. Close programs that use them and run -Uninstall again.') -ForegroundColor Red
        return 1
    }
    Write-Host ''
    Write-Host 'Uninstall finished.' -ForegroundColor Green
    return 0
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if ($PreflightOnly -and $Uninstall) {
    Write-Host 'Choose either -PreflightOnly or -Uninstall, not both.' -ForegroundColor Red
    exit 1
}
if (-not (Test-Path -LiteralPath (Join-Path $Root 'pyproject.toml')) -or -not (Test-Path -LiteralPath (Join-Path $Root $ConstraintsFile)) -or -not (Test-Path -LiteralPath $EnvExample)) {
    Write-Host ('This does not look like a complete copy of the OnCue repository: ' + $Root) -ForegroundColor Red
    Write-Host 'Run the installer from the unpacked or cloned OnCue folder, as scripts\install.ps1.' -ForegroundColor Red
    exit 1
}

# Process-scoped settings, restored on exit so a run inside an open PowerShell
# session leaves that session as it was. pip's cache and all temp files go to
# .oncue-win, so nothing lands in %LOCALAPPDATA% or %TEMP%.
$processEnvironment = [ordered]@{
    'PIP_CACHE_DIR'                 = $PipCacheDir
    'PIP_DISABLE_PIP_VERSION_CHECK' = '1'
    'PYTHONUTF8'                    = '1'
    'TEMP'                          = $TempDir
    'TMP'                           = $TempDir
}
$savedEnvironment = @{}
foreach ($name in $processEnvironment.Keys) {
    $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
$savedProgress = $ProgressPreference
$savedProtocol = [System.Net.ServicePointManager]::SecurityProtocol
$exitCode = 0
# pip resolves ".[windows]" against the current folder, which PowerShell hands
# to every program it starts.
Push-Location -LiteralPath $Root
try {
    $ProgressPreference = 'SilentlyContinue'
    [System.Net.ServicePointManager]::SecurityProtocol = $savedProtocol -bor [System.Net.SecurityProtocolType]::Tls12

    if ($Uninstall) {
        $exitCode = Invoke-Uninstall
    }
    else {
        $preflight = Invoke-Preflight
        if ($PreflightOnly) {
            if ($preflight.Blocking -gt 0) { $exitCode = 1 }
        }
        elseif ($preflight.Blocking -gt 0) {
            Write-Host 'Nothing was installed. Fix the blocking items above and run the installer again.' -ForegroundColor Red
            $exitCode = 1
        }
        else {
            Invoke-Install -Preflight $preflight
        }
    }
}
catch {
    Write-Host ''
    Write-Fail $_.Exception.Message
    Write-LogLine ($_ | Out-String)
    if ($script:LogPath) {
        Write-Host ('Install log: ' + $script:LogPath) -ForegroundColor Red
        Write-Host 'Running the installer again resumes where it stopped.' -ForegroundColor Red
    }
    $exitCode = 1
}
finally {
    Close-InstallLog
    foreach ($name in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], 'Process')
    }
    $ProgressPreference = $savedProgress
    [System.Net.ServicePointManager]::SecurityProtocol = $savedProtocol
    Pop-Location
}
exit $exitCode
