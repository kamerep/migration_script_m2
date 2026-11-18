<#
.SYNOPSIS
  File list of the M2 document archive, for the M2 -> CSP migration (archive inventory).

.DESCRIPTION
  Writes one CSV row per file under the archive folder: Path (from the drive root, e.g.
  NOVA-PH\KP-00031-ENT\KYC\passport.pdf), Length (bytes), Modified (yyyy-MM-dd HH:mm).
  Only names, sizes and dates are read: no file is opened or copied. Put the CSV next to the
  M2 exports; the migration reads it as "Archive inventory" and indexes every file under the
  mandate whose number names its folder (KP-00031-ENT = KP-31-ENT).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File m2_archive_inventory.ps1 -Root 'Z:\NOVA-PH'

.EXAMPLE
  # Several holder folders at once (one CSV)
  powershell -ExecutionPolicy Bypass -File m2_archive_inventory.ps1 -Root 'Z:\' -Out 'W:\M2 Archive inventory.csv'
#>
param(
  [string]$Root = 'Z:\',
  [string]$Out = (Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'M2 Archive inventory.csv')
)
$ErrorActionPreference = 'Stop'
$item = Get-Item -LiteralPath $Root
$drive = $item.PSDrive.Root            # e.g. Z:\
Write-Host "Listing $Root ..."
# Only files inside folders: loose files of the root (e.g. the "Archive (L) - Verknüpfung" shortcut) are skipped,
# and shortcuts (.lnk) are never followed or listed.
# Progress per mandate holder (top folder): a bar in the console, and a line when each holder starts.
$holders = @(Get-ChildItem -LiteralPath $Root -Directory -Force -ErrorAction SilentlyContinue)
$n = 0
$script:count = 0
# Rows go straight into the CSV (not collected first), so the file grows holder by holder and the M2 UI can show
# which holders are done (Overview > Archive inventory).
$holders |
  ForEach-Object {
    $n++
    Write-Progress -Activity "Listing $Root" -Status ("Holder {0} of {1}: {2}" -f $n, $holders.Count, $_.Name) `
                   -PercentComplete ([int](100 * ($n - 1) / [Math]::Max(1, $holders.Count)))
    Write-Host ("[{0}/{1}] {2}" -f $n, $holders.Count, $_.Name)
    Get-ChildItem -LiteralPath $_.FullName -Recurse -File -Force -ErrorAction SilentlyContinue } |
  Where-Object { $_.Name -notin @('Thumbs.db', 'desktop.ini') -and -not $_.Name.StartsWith('~$') -and
                 $_.Extension -ne '.lnk' } |
  Select-Object @{n = 'Path'; e = { $_.FullName.Substring($drive.Length).TrimStart('\') } },
                Length,
                @{n = 'Modified'; e = { $_.LastWriteTime.ToString('yyyy-MM-dd HH:mm') } } |
  ForEach-Object { $script:count++; $_ } |
  Export-Csv -LiteralPath $Out -NoTypeInformation -Encoding UTF8
Write-Progress -Activity "Listing $Root" -Completed
Write-Host ("{0} files in {1} holder folders -> {2}" -f $script:count, $holders.Count, $Out)
