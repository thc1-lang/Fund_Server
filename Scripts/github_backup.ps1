$ErrorActionPreference = "Stop"

$Repo = "C:\Fund_Server"
$Log = "C:\Fund_Server\Logs\github_backup.log"

Set-Location $Repo

function Log($Message) {
    $Time = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$Time | $Message" | Out-File -FilePath $Log -Append -Encoding utf8
}

try {
    Log "Backup started"

    git fetch origin 2>&1 | Out-File -FilePath $Log -Append

    $Local  = git rev-parse HEAD
    $Remote = git rev-parse origin/main

    if ($Local -ne $Remote) {
        Log "STOPPED: local main and origin/main differ. Manual review required."
        exit 1
    }

    git add .

    git diff --cached --quiet

    if ($LASTEXITCODE -eq 0) {
        Log "No changes detected"
        exit 0
    }

    $Stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    git commit -m "Automatic backup $Stamp" 2>&1 | Out-File -FilePath $Log -Append

    git push origin main 2>&1 | Out-File -FilePath $Log -Append

    if ($LASTEXITCODE -ne 0) {
        throw "Git push failed"
    }

    Log "Backup completed successfully"
}
catch {
    Log "ERROR: $($_.Exception.Message)"
    exit 1
}
