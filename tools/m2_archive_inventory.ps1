<#
.SYNOPSIS
  File list of the M2 document archive, for the M2 -> CSP migration (archive inventory).

.DESCRIPTION
  Writes one CSV row per file under the archive folder: Path (from the drive root, e.g.
  ARVIS-PH\AP-00031-ENT\KYC\passport.pdf), Length (bytes), Modified (yyyy-MM-dd HH:mm).
  Only names, sizes and dates are read: no file is opened or copied. Put the CSV next to the
  M2 exports; the migration reads it as "Archive inventory" and indexes every file under the
  mandate whose number names its folder (AP-00031-ENT = AP-31-ENT).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File m2_archive_inventory.ps1 -Root 'Z:\ARVIS-PH'

.EXAMPLE
  # Several holder folders at once (one CSV)
  powershell -ExecutionPolicy Bypass -File m2_archive_inventory.ps1 -Root 'Z:\' -Out 'W:\M2 Archive inventory.csv'
#>
param(
  [string]$Root = 'Z:\ARVIS-PH',
  [string]$Out = (Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'M2 Archive inventory.csv')
)
$ErrorActionPreference = 'Stop'
$item = Get-Item -LiteralPath $Root
$drive = $item.PSDrive.Root            # e.g. Z:\
Write-Host "Listing $Root ..."
$rows = Get-ChildItem -LiteralPath $Root -Recurse -File -Force -ErrorAction SilentlyContinue |
  Where-Object { $_.Name -notin @('Thumbs.db', 'desktop.ini') -and -not $_.Name.StartsWith('~$') } |
  Select-Object @{n = 'Path'; e = { $_.FullName.Substring($drive.Length).TrimStart('\') } },
                Length,
                @{n = 'Modified'; e = { $_.LastWriteTime.ToString('yyyy-MM-dd HH:mm') } }
$rows | Export-Csv -LiteralPath $Out -NoTypeInformation -Encoding UTF8
$folders = ($rows | ForEach-Object { ($_.Path -split '\\')[0..1] -join '\' } | Sort-Object -Unique).Count
Write-Host ("{0} files in {1} folders -> {2}" -f $rows.Count, $folders, $Out)
