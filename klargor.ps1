# Klargoer Kort og Godt til dine venner.
# Du (der delte programmet) koerer denne EN gang: indsaet den delte
# forbindelse, saa laver den en faerdig ZIP, dine venner bare kan installere.
$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

Write-Host ""
Write-Host "===============================================" -ForegroundColor DarkYellow
Write-Host "   Klargoer Kort og Godt til dine venner" -ForegroundColor Yellow
Write-Host "===============================================" -ForegroundColor DarkYellow
Write-Host ""
Write-Host "Indsaet den DELTE forbindelse (DATABASE_URL) - praecis den samme,"
Write-Host "du brugte i Streamlit's Secrets. (Hoejreklik i vinduet = indsaet.)"
Write-Host ""
$dburl = (Read-Host "DATABASE_URL").Trim()
if ($dburl.Length -lt 10 -or $dburl -notmatch "^postgres") {
    Write-Host "Det ligner ikke en gyldig forbindelse - afbryder." -ForegroundColor Red
    Read-Host "Tryk Enter for at lukke"; exit 1
}

# Gem forbindelsen (bruges ogsaa af din egen lokale installation)
$sdir = Join-Path $here ".streamlit"
if (-not (Test-Path $sdir)) { New-Item -ItemType Directory -Path $sdir | Out-Null }
$content = "DATABASE_URL = `"$dburl`"`r`n"
[System.IO.File]::WriteAllText((Join-Path $sdir "secrets.toml"),
    $content, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "Forbindelse gemt." -ForegroundColor Green

# Byg en ren pakke til vennerne (med forbindelsen, uden dine udviklingsfiler)
Write-Host "Bygger pakke til dine venner..."
$stage = Join-Path $env:TEMP ("kgfriend_" + $PID)
$dest = Join-Path $stage "Kort og Godt"
New-Item -ItemType Directory -Path (Join-Path $dest ".streamlit") -Force | Out-Null
$include = @("app.py", "scanner.py", "db.py", "watchlist.json",
             "collection.json", "requirements.txt", "kort_og_godt.ico",
             "kort_og_godt.png", "make_icon.py", "Start Kort og Godt.bat",
             "start.ps1", "Installer Kort og Godt.bat", "installer.ps1",
             "LAES MIG FOERST.txt", "Kort og Godt - Brugervejledning.pdf")
foreach ($f in $include) {
    $p = Join-Path $here $f
    if (Test-Path $p) { Copy-Item $p (Join-Path $dest $f) }
}
Copy-Item (Join-Path $here ".streamlit\config.toml") `
    (Join-Path $dest ".streamlit\config.toml") -ErrorAction SilentlyContinue
Copy-Item (Join-Path $sdir "secrets.toml") (Join-Path $dest ".streamlit\secrets.toml")

# Laeg ZIP'en paa skrivebordet (ikke i programmappen)
$zip = Join-Path ([Environment]::GetFolderPath("Desktop")) "Kort og Godt - til venner.zip"
Compress-Archive -Path $dest -DestinationPath $zip -Force

Write-Host ""
Write-Host "===============================================" -ForegroundColor DarkYellow
Write-Host "   Faerdig!" -ForegroundColor Green
Write-Host "===============================================" -ForegroundColor DarkYellow
Write-Host "Der ligger nu en fil paa dit skrivebord:"
Write-Host "   Kort og Godt - til venner.zip" -ForegroundColor Cyan
Write-Host ""
Write-Host "Send den til dine venner. De skal BLOT pakke den ud og dobbeltklikke"
Write-Host "'Installer Kort og Godt.bat' - resten (inkl. Python) sker automatisk."
Write-Host ""
Write-Host "OBS: Filen indeholder databaseadgangen - del den kun med dine venner."
Write-Host ""
Read-Host "Tryk Enter for at lukke"
