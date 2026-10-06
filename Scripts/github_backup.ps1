<#
Safely backs up Fund Server code and infrastructure to origin/main.

This script never force-pushes, merges, rebases, resets, cleans, or changes
the Data directory. A remote change that is not already incorporated in the
local branch stops the backup for manual review.
#>

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    # Native Git commands are evaluated by their process exit codes below.
    $PSNativeCommandUseErrorActionPreference = $false
}

$RepoRoot = 'C:\Fund_Server'
$LogPath = Join-Path $RepoRoot 'Logs\github_backup.log'
$ExpectedBranch = 'main'
$MaximumStagedFileBytes = 100MB # GitHub rejects files at or above 100 MiB.
$TelegramPython = 'C:\Users\thc1\AppData\Local\Python\pythoncore-3.14-64\python.exe'
$TelegramNotifier = Join-Path $RepoRoot 'Scripts\send_backup_telegram.py'

function Write-BackupLog {
    param([Parameter(Mandatory = $true)][string]$Message)

    $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -LiteralPath $LogPath -Value "$timestamp | $Message" -Encoding UTF8
}

function Send-BackupTelegram {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('success', 'failure')][string]$Status,
        [string]$CommitHash = ''
    )

    # Reuse the existing macro pipeline notifier. It reads Telegram credentials
    # from the Windows user environment/registry and never exposes them in Git.
    if (-not (Test-Path -LiteralPath $TelegramPython -PathType Leaf) -or
        -not (Test-Path -LiteralPath $TelegramNotifier -PathType Leaf)) {
        Write-BackupLog 'Telegram notification skipped: notifier runtime is unavailable.'
        return
    }

    $arguments = @('-X', 'utf8', '-u', $TelegramNotifier, '--status', $Status)
    if ($CommitHash) { $arguments += @('--commit', $CommitHash) }
    try {
        $previousErrorActionPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            & $TelegramPython @arguments 2>$null | Out-Null
            $exitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
        if ($exitCode -eq 0) {
            Write-BackupLog "Telegram $Status notification requested."
        }
        else {
            Write-BackupLog "Telegram $Status notification could not be requested (exit code $exitCode)."
        }
    }
    catch {
        # Telegram must never change the backup result or reveal transport details.
        try { Write-BackupLog "Telegram $Status notification could not be requested." } catch { }
    }
}

function Invoke-Git {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Operation
    )

    # Suppress Git's informational stderr. Process exit code, rather than
    # stderr, determines success, and raw diagnostics are not logged.
    # Temporarily relax PowerShell's native-error handling so a Git warning
    # (for example, line-ending advice) cannot be mistaken for a failure.
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & git @Arguments 2>$null
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($exitCode -ne 0) {
        Write-BackupLog "ERROR: $Operation failed (git exit code $exitCode)."
        throw "$Operation failed (git exit code $exitCode)."
    }

    return @($output)
}

function Get-GitText {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Operation
    )

    return ((Invoke-Git -Arguments $Arguments -Operation $Operation) -join "`n").Trim()
}

function Test-GitAncestor {
    param(
        [Parameter(Mandatory = $true)][string]$Ancestor,
        [Parameter(Mandatory = $true)][string]$Descendant
    )

    & git merge-base --is-ancestor $Ancestor $Descendant 2>$null
    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 0) { return $true }
    if ($exitCode -eq 1) { return $false }

    Write-BackupLog "ERROR: Unable to compare $Ancestor and $Descendant (git exit code $exitCode)."
    throw "Unable to compare $Ancestor and $Descendant (git exit code $exitCode)."
}

function Get-StagedPaths {
    $names = Get-GitText -Arguments @('diff', '--cached', '--name-only') -Operation 'Reading staged file list'
    if ([string]::IsNullOrWhiteSpace($names)) { return @() }
    return @($names -split "`r?`n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
}

function Test-SafeRootBackupFileName {
    param([Parameter(Mandatory = $true)][string]$Name)

    if ($Name -in @('.gitignore', '.gitattributes', '.editorconfig', 'README', 'LICENSE', 'NOTICE')) {
        return $true
    }

    return ([System.IO.Path]::GetExtension($Name).ToLowerInvariant() -in @(
        '.md', '.txt', '.json', '.yml', '.yaml', '.ini', '.cfg', '.ps1', '.cmd', '.bat'
    ))
}

function Test-EligibleBackupPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    $normalizedPath = $Path -replace '\\', '/'
    if ($normalizedPath -match '^(Code|Scripts)/') { return $true }
    if ($normalizedPath -notmatch '/') { return (Test-SafeRootBackupFileName -Name $normalizedPath) }
    return $false
}

function Get-BackupStageTargets {
    $targets = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    [void]$targets.Add('Code')
    [void]$targets.Add('Scripts')

    $rootNames = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    Get-ChildItem -LiteralPath $RepoRoot -Force -File | ForEach-Object { [void]$rootNames.Add($_.Name) }
    $headRootNames = Get-GitText -Arguments @('ls-tree', '--name-only', 'HEAD') -Operation 'Reading root backup scope'
    if (-not [string]::IsNullOrWhiteSpace($headRootNames)) {
        $headRootNames -split "`r?`n" | ForEach-Object { [void]$rootNames.Add($_) }
    }
    foreach ($name in $rootNames) {
        if (Test-SafeRootBackupFileName -Name $name) { [void]$targets.Add($name) }
    }
    return @($targets)
}

function Assert-SafeStaging {
    $allStagedPaths = @(Get-StagedPaths)
    $protectedPaths = [System.Collections.Generic.List[string]]::new()
    $credentialPaths = [System.Collections.Generic.List[string]]::new()
    $unexpectedPaths = [System.Collections.Generic.List[string]]::new()

    foreach ($path in $allStagedPaths) {
        $normalizedPath = $path -replace '\\', '/'
        if ($normalizedPath -match '^(Data|Logs|Backups|recovery)/' -or
            $normalizedPath -match '(^|/)(\.venv|venv|env|__pycache__|work)(/|$)') {
            $protectedPaths.Add($normalizedPath)
        }

        if ($normalizedPath -match '(^|/)(\.env(?:\.|$)|google_credentials\.json$|service_account\.json$|[^/]*credentials[^/]*\.json$|[^/]*\.pem$|[^/]*\.key$|id_rsa$|id_ed25519$|secrets(/|$)|credentials(/|$))') {
            $credentialPaths.Add($normalizedPath)
        }
        if (-not (Test-EligibleBackupPath -Path $normalizedPath)) {
            $unexpectedPaths.Add($normalizedPath)
        }
    }

    if ($protectedPaths.Count -gt 0 -or $credentialPaths.Count -gt 0 -or $unexpectedPaths.Count -gt 0) {
        Write-BackupLog 'STOPPED: protected, credential-named, or out-of-scope files are staged; no commit was created.'
        throw 'Protected, credential-named, or out-of-scope files are staged. Remove them from the index and review manually.'
    }

    # Only additions/modifications/renames/copies have an index blob to inspect.
    $contentPathsText = Get-GitText -Arguments @('diff', '--cached', '--name-only', '--diff-filter=ACMR') -Operation 'Reading staged content list'
    $contentPaths = @()
    if (-not [string]::IsNullOrWhiteSpace($contentPathsText)) {
        $contentPaths = @($contentPathsText -split "`r?`n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    }

    $secretPatterns = @(
        '-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----',
        '(?i)(?:ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,})',
        'AKIA[0-9A-Z]{16}',
        'AIza[0-9A-Za-z\-_]{35}',
        'xox[baprs]-[A-Za-z0-9-]{10,}'
    )

    foreach ($path in $contentPaths) {
        $blobSizeText = Get-GitText -Arguments @('cat-file', '-s', ":$path") -Operation 'Checking staged file size'
        $blobSize = [Int64]::Parse($blobSizeText)
        if ($blobSize -ge $MaximumStagedFileBytes) {
            Write-BackupLog "STOPPED: staged file exceeds GitHub's 100 MiB limit; no commit was created."
            throw "A staged file is at or above GitHub's 100 MiB limit."
        }

        # Scan the staged version, never the worktree version, and never log content.
        $blobContent = (& git show --no-textconv ":$path" 2>$null) -join "`n"
        if ($LASTEXITCODE -ne 0) {
            Write-BackupLog "ERROR: Unable to inspect staged file content (git exit code $LASTEXITCODE)."
            throw "Unable to inspect staged file content (git exit code $LASTEXITCODE)."
        }
        foreach ($pattern in $secretPatterns) {
            if ($blobContent -match $pattern) {
                Write-BackupLog 'STOPPED: an obvious credential or private key pattern was found in staged content; no commit was created.'
                throw 'An obvious credential or private key pattern was found in staged content.'
            }
        }
    }

    Write-BackupLog "Staging safety checks passed for $($allStagedPaths.Count) staged file(s)."
    return $allStagedPaths.Count
}

function Assert-RemoteCanBePushed {
    $localCommit = Get-GitText -Arguments @('rev-parse', 'HEAD') -Operation 'Resolving local main commit'
    $remoteCommit = Get-GitText -Arguments @('rev-parse', 'origin/main') -Operation 'Resolving origin/main commit'

    if (-not (Test-GitAncestor -Ancestor 'origin/main' -Descendant 'HEAD')) {
        Write-BackupLog 'STOPPED: origin/main has commits not safely incorporated into local main; manual review required.'
        throw 'origin/main contains commits not safely incorporated into local main. Manual review is required; nothing was pushed.'
    }

    return @{ Local = $localCommit; Remote = $remoteCommit }
}

try {
    $logDirectory = Split-Path -Parent $LogPath
    if (-not (Test-Path -LiteralPath $logDirectory -PathType Container)) {
        New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    }

    Write-BackupLog 'Backup started.'
    if (-not (Test-Path -LiteralPath $RepoRoot -PathType Container)) {
        throw "Repository directory does not exist: $RepoRoot"
    }
    Set-Location -LiteralPath $RepoRoot

    $actualRoot = Get-GitText -Arguments @('rev-parse', '--show-toplevel') -Operation 'Validating Git repository'
    if ([System.IO.Path]::GetFullPath($actualRoot).TrimEnd('\\') -ne [System.IO.Path]::GetFullPath($RepoRoot).TrimEnd('\\')) {
        throw "Expected repository root '$RepoRoot' but Git reported '$actualRoot'."
    }
    $branch = Get-GitText -Arguments @('branch', '--show-current') -Operation 'Checking current branch'
    if ($branch -ne $ExpectedBranch) {
        Write-BackupLog "STOPPED: expected branch '$ExpectedBranch', found '$branch'."
        throw "Expected branch '$ExpectedBranch', found '$branch'."
    }
    [void](Get-GitText -Arguments @('remote', 'get-url', 'origin') -Operation 'Validating origin remote')
    Write-BackupLog 'Repository validation passed (main branch and origin remote confirmed).'

    [void](Invoke-Git -Arguments @('fetch', 'origin') -Operation 'Fetching origin')
    Write-BackupLog 'Remote fetch completed.'
    $relationship = Assert-RemoteCanBePushed

    # Stage only Code/, Scripts/, and safe root configuration/documentation files.
    # .gitignore still provides a second exclusion layer for generated/runtime files.
    $stageTargets = @(Get-BackupStageTargets)
    [void](Invoke-Git -Arguments (@('add', '-A', '--') + $stageTargets) -Operation 'Staging eligible changes')
    $stagedCount = Assert-SafeStaging

    & git diff --cached --quiet
    $diffExitCode = $LASTEXITCODE
    if ($diffExitCode -eq 0) {
        Write-BackupLog 'No changes detected.'
        if ($relationship.Local -eq $relationship.Remote) {
            Send-BackupTelegram -Status success
            exit 0
        }
        Write-BackupLog 'Local main is ahead of origin/main; pushing existing local commit(s).'
    }
    elseif ($diffExitCode -eq 1) {
        $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
        [void](Invoke-Git -Arguments @('commit', '-m', "Automatic backup $stamp") -Operation 'Creating backup commit')
        $commitHash = Get-GitText -Arguments @('rev-parse', '--short', 'HEAD') -Operation 'Resolving backup commit hash'
        Write-BackupLog "Backup commit created: $commitHash ($stagedCount staged file(s))."
    }
    else {
        Write-BackupLog "ERROR: Unable to determine staged changes (git exit code $diffExitCode)."
        throw "Unable to determine staged changes (git exit code $diffExitCode)."
    }

    # Re-fetch immediately before the non-force push to detect concurrent remote changes.
    [void](Invoke-Git -Arguments @('fetch', 'origin') -Operation 'Re-fetching origin before push')
    Write-BackupLog 'Pre-push remote fetch completed.'
    [void](Assert-RemoteCanBePushed)

    [void](Invoke-Git -Arguments @('push', 'origin', $ExpectedBranch) -Operation 'Pushing backup to origin/main')
    [void](Invoke-Git -Arguments @('fetch', 'origin') -Operation 'Verifying pushed backup')
    $finalLocal = Get-GitText -Arguments @('rev-parse', 'HEAD') -Operation 'Resolving final local commit'
    $finalRemote = Get-GitText -Arguments @('rev-parse', 'origin/main') -Operation 'Resolving final origin/main commit'
    if ($finalLocal -ne $finalRemote) {
        Write-BackupLog 'ERROR: Push returned success but local main and origin/main are not synchronized.'
        throw 'Push returned success but local main and origin/main are not synchronized.'
    }

    Write-BackupLog "Backup completed successfully; origin/main verified at $finalLocal."
    Send-BackupTelegram -Status success -CommitHash $finalLocal
    exit 0
}
catch {
    try { Write-BackupLog "ERROR: $($_.Exception.Message)" } catch { }
    Send-BackupTelegram -Status failure
    exit 1
}
