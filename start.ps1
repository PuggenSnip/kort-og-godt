# Starter Kort og Godt - og henter automatisk seneste version fra GitHub foerst,
# saa den lokale installation altid er opdateret (springes over hvis offline).
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

$venvPy = Join-Path $here ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "Programmet er ikke installeret endnu."
    Write-Host "Koer 'Installer Kort og Godt.bat' foerst."
    Read-Host "Tryk Enter for at lukke"; exit 1
}

# --- Auto-opdatering fra GitHub ---
$needPip = $false
try {
    Write-Host "Tjekker efter opdateringer..."
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $url = "https://github.com/PuggenSnip/kort-og-godt/archive/refs/heads/main.zip"
    $tmp = Join-Path $env:TEMP ("kgupd_" + $PID)
    $zip = "$tmp.zip"
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing -TimeoutSec 25
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    $inner = Get-ChildItem $tmp -Directory | Select-Object -First 1
    if ($inner) {
        # Opdater KUN kodefiler. Roer aldrig ved .venv eller secrets.toml.
        $reqNew = Join-Path $inner.FullName "requirements.txt"
        $reqCur = Join-Path $here "requirements.txt"
        if (Test-Path $reqNew) {
            $newHash = (Get-FileHash $reqNew).Hash
            $curHash = if (Test-Path $reqCur) { (Get-FileHash $reqCur).Hash } else { "" }
            if ($newHash -ne $curHash) { $needPip = $true }
        }
        foreach ($f in @("app.py", "scanner.py", "db.py", "requirements.txt",
                         "make_icon.py", "kort_og_godt.ico", "kort_og_godt.png")) {
            $s = Join-Path $inner.FullName $f
            if (Test-Path $s) { Copy-Item $s (Join-Path $here $f) -Force }
        }
        $cfgNew = Join-Path $inner.FullName ".streamlit\config.toml"
        if (Test-Path $cfgNew) {
            $cfgDir = Join-Path $here ".streamlit"
            if (-not (Test-Path $cfgDir)) { New-Item -ItemType Directory $cfgDir | Out-Null }
            Copy-Item $cfgNew (Join-Path $cfgDir "config.toml") -Force
        }
    }
} catch {
    Write-Host "Kunne ikke tjekke for opdateringer (offline?). Starter alligevel."
}

# Installer nye pakker, hvis kravene er aendret
if ($needPip) {
    Write-Host "Opdaterer programmets pakker..."
    & $venvPy -m pip install -r (Join-Path $here "requirements.txt") `
        --quiet --disable-pip-version-check 2>&1 | Out-Null
}

# --- Start programmet ---
Write-Host "Starter Kort og Godt..."
& $venvPy -m streamlit run (Join-Path $here "app.py") `
    --server.headless=false --browser.gatherUsageStats=false
