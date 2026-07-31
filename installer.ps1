# Kort og Godt - installationsscript (dansk). Koeres via "Installer Kort og Godt.bat".
# Installerer ALT automatisk: Python (hvis noedvendigt), programmets pakker,
# og opretter en genvej. Brugeren skal intet andet goere.
$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

Write-Host ""
Write-Host "===============================================" -ForegroundColor DarkYellow
Write-Host "   Kort og Godt - Installation" -ForegroundColor Yellow
Write-Host "===============================================" -ForegroundColor DarkYellow
Write-Host "Dette kan tage nogle minutter foerste gang. Laen dig tilbage."
Write-Host ""

function Find-Python {
    $cands = @()
    $g = Get-Command python -ErrorAction SilentlyContinue
    if ($g) { $cands += $g.Source }
    $pyl = Get-Command py -ErrorAction SilentlyContinue
    if ($pyl) {
        foreach ($v in @("3.12", "3.13", "3.11")) {
            try {
                $e = & py -$v -c "import sys;print(sys.executable)" 2>$null
                if ($e) { $cands += $e.Trim() }
            } catch {}
        }
    }
    $base = Join-Path $env:LOCALAPPDATA "Programs\Python"
    if (Test-Path $base) {
        foreach ($d in Get-ChildItem $base -Directory -ErrorAction SilentlyContinue) {
            $e = Join-Path $d.FullName "python.exe"
            if (Test-Path $e) { $cands += $e }
        }
    }
    foreach ($e in ($cands | Select-Object -Unique)) {
        try {
            $v = & $e -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
            if ($v -match '^3\.(1[1-9]|[2-9]\d)') { return $e }
        } catch {}
    }
    return $null
}

# 1) Find eller installer Python
$py = Find-Python
if (-not $py) {
    Write-Host "Python blev ikke fundet - installerer det automatisk (kun for dig," -ForegroundColor Yellow
    Write-Host "ingen administrator noedvendig)..." -ForegroundColor Yellow
    $installed = $false
    $wg = Get-Command winget -ErrorAction SilentlyContinue
    if ($wg) {
        try {
            winget install -e --id Python.Python.3.12 --scope user --silent `
                --accept-source-agreements --accept-package-agreements 2>&1 | Out-Null
            $py = Find-Python
            if ($py) { $installed = $true }
        } catch {}
    }
    if (-not $installed) {
        try {
            $url = "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe"
            $tmp = Join-Path $env:TEMP "kg-python-setup.exe"
            Write-Host "Henter Python..."
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing
            Write-Host "Installerer Python..."
            Start-Process -FilePath $tmp -Wait -ArgumentList `
                "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_pip=1", "Include_test=0"
            $py = Find-Python
        } catch {
            Write-Host "Kunne ikke installere Python automatisk (maaske pga. internet)." -ForegroundColor Red
            Write-Host "Hent Python paa https://www.python.org/downloads/ (saet flueben i"
            Write-Host "'Add python.exe to PATH') og koer denne installer igen."
            Read-Host "Tryk Enter for at lukke"; exit 1
        }
    }
}
if (-not $py) {
    Write-Host "Python blev installeret, men skal lige aktiveres." -ForegroundColor DarkYellow
    Write-Host "Genstart computeren og koer 'Installer Kort og Godt.bat' igen."
    Read-Host "Tryk Enter for at lukke"; exit 1
}
Write-Host "Python er klar." -ForegroundColor Green

# 2) Virtuelt miljoe
Write-Host "Opretter programmets miljoe..."
& $py -m venv .venv
$venvPy = Join-Path $here ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "Kunne ikke oprette miljoeet. Proev at koere installeren igen." -ForegroundColor Red
    Read-Host "Tryk Enter for at lukke"; exit 1
}

# 3) Installer pakker
Write-Host "Henter og installerer programmets pakker (kan tage et par minutter)..."
& $venvPy -m pip install --upgrade pip 2>&1 | Out-Null
& $venvPy -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "Der opstod en fejl under installation af pakkerne." -ForegroundColor Red
    Read-Host "Tryk Enter for at lukke"; exit 1
}

# 4) Delt forbindelse (er lagt ind paa forhaand af den, der delte pakken)
$secretsFile = Join-Path $here ".streamlit\secrets.toml"
if (Test-Path $secretsFile) {
    Write-Host "Forbundet til de delte data." -ForegroundColor Green
} else {
    Write-Host "BEMAERK: Ingen delt forbindelse i pakken - programmet koerer med" -ForegroundColor DarkYellow
    Write-Host "lokale data. (Den, der delte pakken, kan lave en forbundet version.)"
}

# 5) Skrivebordsgenvej
Write-Host "Opretter genvej paa skrivebordet..."
try {
    $desktop = [Environment]::GetFolderPath("Desktop")
    $lnk = Join-Path $desktop "Kort og Godt.lnk"
    $sh = New-Object -ComObject WScript.Shell
    $s = $sh.CreateShortcut($lnk)
    $s.TargetPath = Join-Path $here "Start Kort og Godt.bat"
    $s.WorkingDirectory = $here
    $ico = Join-Path $here "kort_og_godt.ico"
    if (Test-Path $ico) { $s.IconLocation = "$ico,0" }
    $s.Description = "Start Kort og Godt"
    $s.WindowStyle = 7
    $s.Save()
    Write-Host "Genvej oprettet." -ForegroundColor Green
} catch {
    Write-Host "Kunne ikke oprette genvejen (ikke kritisk - brug 'Start Kort og Godt.bat')." -ForegroundColor DarkYellow
}

Write-Host ""
Write-Host "===============================================" -ForegroundColor DarkYellow
Write-Host "   Faerdig - Kort og Godt er installeret!" -ForegroundColor Green
Write-Host "===============================================" -ForegroundColor DarkYellow
Write-Host ""
Write-Host "Start programmet via genvejen 'Kort og Godt' paa skrivebordet."
Write-Host ""
Read-Host "Tryk Enter for at lukke"
