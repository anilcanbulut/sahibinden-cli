<#
.SYNOPSIS
    Sweep from the machine that can reach sahibinden, then publish the database
    to the machine that does the reading.

.DESCRIPTION
    Fetching and analysis do not have to happen in the same place, and for this
    site they cannot. Cloudflare issues cf_clearance against a specific client
    address, so a session copied to a server in another country is refused on
    arrival — verified, HTTP 403 with cf-mitigated. The browser has to run here.

    Nothing else does. price, listing, panels, history, sql and reparse all read
    data/sahibinden.db and touch the network never, so once the database is on
    the far end, every question can be answered there for free and offline.

    So this script runs the expensive half locally and copies two files up: the
    database, and the filter vocabulary harvest builds from fetched pages.
    Deliberately NOT copied:

      breaker.json  per-machine request budget and cooldown. Shipping it would
                    let one machine's trip silence the other, and would reset a
                    real cooldown by overwriting it with a clean one.
      session.json  cookies, which are bound to this machine's address anyway.
      raw/          hundreds of MB of saved HTML. reparse needs it; it lives
                    where it was fetched.

.PARAMETER Remote
    user@host for scp.

.PARAMETER RemotePath
    Directory on the far end — the repo's data/ directory.

.PARAMETER SkipFetch
    Publish what is already stored without sweeping first. Use when the nightly
    already ran, or when the breaker is cooling down.

.PARAMETER Pages
    Pages per tracked search, passed through to nightly.

.EXAMPLE
    .\tools\publish.ps1
    Sweep, then publish.

.EXAMPLE
    .\tools\publish.ps1 -SkipFetch
    Publish the current database without spending a single request.

.NOTES
    For an unattended scheduled task, scp must not prompt — set up key auth
    first and confirm `ssh <remote> exit` returns without asking for anything.
#>

[CmdletBinding()]
param(
    [string]$Remote     = "trader@91.99.91.241",
    [string]$RemotePath = "sahibinden-cli/data",
    [switch]$SkipFetch,
    [int]$Pages = 0
)

$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
$data = Join-Path $repo "data"
$db   = Join-Path $data "sahibinden.db"
$tax  = Join-Path $data "taxonomy.json"

function Invoke-Cli {
    param([string[]]$CliArgs)
    # Run through sahibinden.py rather than the console script: it works whether
    # or not pip's Scripts directory made it onto PATH.
    & python (Join-Path $repo "sahibinden.py") @CliArgs
    return $LASTEXITCODE
}

Write-Host "repo : $repo"
Write-Host "remote: ${Remote}:${RemotePath}`n"

if (-not $SkipFetch) {
    $nightlyArgs = @("nightly")
    if ($Pages -gt 0) { $nightlyArgs += @("--pages", "$Pages") }

    Write-Host "== sweeping =="
    $code = Invoke-Cli $nightlyArgs

    # Exit 2 is the breaker declining on purpose, not a failure. The database
    # from previous runs is still worth publishing, so carry on rather than
    # aborting and leaving the far end stale.
    if ($code -eq 2) {
        Write-Host "`n  budget spent or cooling down — publishing what is already stored`n"
    } elseif ($code -ne 0) {
        throw "nightly failed with exit code $code"
    }
}

if (-not (Test-Path $db)) { throw "no database at $db — run a search first" }

Write-Host "== publishing =="

# The store runs in WAL mode, so the .db file on its own is not the database —
# recent writes sit in sahibinden.db-wal until something checkpoints them, and
# copying the three files separately can still tear if anything writes between
# them. VACUUM INTO takes a consistent snapshot through SQLite itself and
# writes one self-contained, already-compacted file. That is what travels.
$snap = Join-Path $env:TEMP "sahibinden-publish.db"
if (Test-Path $snap) { Remove-Item $snap -Force }

$snapEscaped = $snap -replace "'", "''"
$py = @"
import sqlite3
db = sqlite3.connect(r'''$db''')
db.execute("VACUUM INTO '$snapEscaped'")
db.close()
"@
$py | & python -
if ($LASTEXITCODE -ne 0) { throw "could not snapshot the database" }

$sizeMb = [math]::Round((Get-Item $snap).Length / 1MB, 2)
Write-Host ("  sahibinden.db  {0} MB (snapshot)" -f $sizeMb)

& scp $snap "${Remote}:${RemotePath}/sahibinden.db"
if ($LASTEXITCODE -ne 0) { throw "scp failed with exit code $LASTEXITCODE" }

if (Test-Path $tax) {
    Write-Host "  taxonomy.json"
    & scp $tax "${Remote}:${RemotePath}/"
    if ($LASTEXITCODE -ne 0) { throw "scp failed with exit code $LASTEXITCODE" }
}

Remove-Item $snap -Force

Write-Host "`nPublished. On the far end, everything is free and offline now:"
Write-Host "  sahibinden-cli price --brand Toyota --model Corolla"
Write-Host "  sahibinden-cli sql `"SELECT brand, model, COUNT(*) FROM listings GROUP BY 1,2`""
