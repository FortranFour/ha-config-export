# =============================================================================
#  cloud_offsite_ha_backups.ps1
#
#  Encrypts each Home Assistant config-export generation into a password
#  protected .7z and places it in the OneDrive folder. OneDrive's own client
#  does the uploading, retrying and bandwidth management.
#
#  Generations are immutable once written, so a file is only ever encrypted
#  once. Each run handles just what is new — normally a few seconds.
#
#  NOTHING UNENCRYPTED LEAVES THIS PC. The exports contain secrets.yaml, auth
#  tokens, ADB keys and camera credentials, so the plain .tar.gz files are
#  never placed in a synced folder.
#
#  Setup, including creating the key file, is in SETUP_cloud_offsite.md.
# =============================================================================

# ---- settings ---------------------------------------------------------------
$Source   = 'D:\Backups\HomeAssistant'
$CloudDir = Join-Path $env:USERPROFILE 'OneDrive\HA-Config-Backups'
$KeyFile  = Join-Path $env:USERPROFILE '.ha_backup_key'
$SevenZip = 'C:\Program Files\7-Zip\7z.exe'
$Hosts    = @('config_export')
$Tiers    = @('daily', 'weekly', 'monthly', 'yearly')
$KeepLogs = 30
# -----------------------------------------------------------------------------

$LogDir = Join-Path $CloudDir '_logs'
$Log    = Join-Path $LogDir ("offsite_{0:yyyy-MM-dd_HHmm}.log" -f (Get-Date))

function Write-Log {
    param([string]$Message)
    $line = "{0:yyyy-MM-dd HH:mm:ss}  {1}" -f (Get-Date), $Message
    Write-Host $line
    Add-Content -Path $Log -Value $line
}

# ---- preflight --------------------------------------------------------------
foreach ($dir in @($CloudDir, $LogDir)) {
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
}

Write-Log "=================================================="
Write-Log "Offsite encrypt run starting"

if (-not (Test-Path $SevenZip)) {
    Write-Log "FATAL: 7-Zip not found at $SevenZip"
    exit 2
}
if (-not (Test-Path $Source)) {
    Write-Log "FATAL: source $Source not found - has the PC copy run yet?"
    exit 2
}
if (-not (Test-Path $KeyFile)) {
    Write-Log "FATAL: key file $KeyFile not found - see SETUP_cloud_offsite.md step 2"
    exit 2
}

# The key file holds the passphrase encrypted with Windows DPAPI, tied to this
# user on this machine. Copying it elsewhere renders it useless, which is the
# point: an attacker with the file alone cannot read it.
try {
    $secure    = Get-Content $KeyFile | ConvertTo-SecureString
    $bstr      = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    $Password  = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
} catch {
    Write-Log "FATAL: could not read key file - was it created by this user on this PC?"
    exit 2
}

# ---- encrypt anything new ---------------------------------------------------
$created = 0
$skipped = 0
$failed  = 0

foreach ($hostName in $Hosts) {
    foreach ($tier in $Tiers) {
        $srcTier = Join-Path $Source "$hostName\$tier"
        if (-not (Test-Path $srcTier)) { continue }

        $dstTier = Join-Path $CloudDir "$hostName\$tier"
        if (-not (Test-Path $dstTier)) { New-Item -ItemType Directory -Path $dstTier -Force | Out-Null }

        foreach ($file in Get-ChildItem -Path $srcTier -Filter 'ha-config-*.tar.gz' -File) {
            $target = Join-Path $dstTier ($file.BaseName -replace '\.tar$', '') 
            $target = "$target.7z"

            if (Test-Path $target) { $skipped++; continue }

            # -mx=0 stores rather than recompresses: the tar.gz is already
            # compressed, so this is much faster for the same result.
            # -mhe=on encrypts the archive headers, hiding the file names inside.
            $args = @('a', '-t7z', '-mx=0', '-mhe=on', "-p$Password", '-bso0', '-bsp0',
                      $target, $file.FullName)
            & $SevenZip @args | Out-Null

            if ($LASTEXITCODE -eq 0 -and (Test-Path $target)) {
                $created++
                Write-Log ("encrypted {0}\{1}\{2}" -f $hostName, $tier, $file.Name)
            } else {
                $failed++
                Write-Log ("FAILED   {0}\{1}\{2}  (7-Zip exit {3})" -f $hostName, $tier, $file.Name, $LASTEXITCODE)
                if (Test-Path $target) { Remove-Item $target -Force }
            }
        }
    }
}

$Password = $null
[GC]::Collect()

# ---- prune cloud copies whose source generation is long gone ----------------
# Additive by default: nothing is deleted. Uncomment to keep the cloud folder
# in step with the PC copy instead.
#
# foreach ($hostName in $Hosts) {
#     foreach ($tier in $Tiers) {
#         $srcTier = Join-Path $Source "$hostName\$tier"
#         $dstTier = Join-Path $CloudDir "$hostName\$tier"
#         if (-not (Test-Path $dstTier)) { continue }
#         foreach ($enc in Get-ChildItem -Path $dstTier -Filter '*.7z' -File) {
#             $origin = Join-Path $srcTier ($enc.BaseName + '.tar.gz')
#             if (-not (Test-Path $origin)) {
#                 Remove-Item $enc.FullName -Force
#                 Write-Log ("pruned   {0}\{1}\{2}" -f $hostName, $tier, $enc.Name)
#             }
#         }
#     }
# }

# ---- tidy logs --------------------------------------------------------------
Get-ChildItem -Path $LogDir -Filter 'offsite_*.log' -File |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$KeepLogs) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

$cloudSize = (Get-ChildItem -Path $CloudDir -Recurse -File -Filter '*.7z' |
              Measure-Object -Property Length -Sum).Sum / 1MB

Write-Log ("Done: {0} new, {1} already present, {2} failed. Cloud folder holds {3:N1} MB." `
           -f $created, $skipped, $failed, $cloudSize)

if ($failed -gt 0) { exit 1 } else { exit 0 }
