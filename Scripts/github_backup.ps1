$ErrorActionPreference = "Stop"

$Repo = "C:\Fund_Server"
$Log = "C:\Fund_Server\Logs\github_backup.log"

Set-Location $Repo

function Log($Message) {
    $Time = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$Time | $Message" | Out-File -FilePath $Log -Append -Encoding utf8
}

function Run-Git($Arguments) {
    $Output = & git @Arguments 2>&1
    $ExitCode = $LASTEXITCODE

    $Output | Out-File -FilePath $Log -Append -Encoding utf8

    if ($ExitCode -ne 0) {
        throw "git $($Arguments -join ' ') failed with exit code $ExitCode"
    }

    return $Output
}

try {
    Log "Backup started"

    Run-Git @("fetch","origin") | Out-Null

    $Local = (& git rev-parse HEAD).Trim()
    $Remote = (& git rev-parse origin/main).Trim()

    if ($Local -ne $Remote) {
        Log "STOPPED: local main and origin/main differ. Manual review required."
        exit 1
    }

    & git add .

    & git diff --cached --quiet

    if ($LASTEXITCODE -eq 0) {
        Log "No changes detected"
        exit 0
    }

    $Stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

    Run-Git @("commit","-m","Automatic backup $Stamp") | Out-Null
    Run-Git @("push","origin","main") | Out-Null

    Log "Backup completed successfully"
}
catch {
    Log "ERROR: $($_.Exception.Message)"
    exit 1
}
