# Grasshopper collector - one-step runner (Windows PowerShell 5+)
# Updates the scripts from GitHub, collects everything (resumes automatically),
# analyzes, and opens the report. Safe to run again any time.
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$dir  = Join-Path $HOME 'Downloads\Grasshopper_Collector_v6\Grasshopper_Collector_v6'
$out  = Join-Path $HOME 'Documents\GrasshopperOutput'
$base = 'https://raw.githubusercontent.com/hawk7227/askai-18/grasshopper-collector'

New-Item -ItemType Directory -Force $dir | Out-Null
foreach ($f in 'collector_v6.py', 'analyze_needs.py', 'check_run.py') {
    Invoke-WebRequest "$base/$f" -OutFile (Join-Path $dir $f) -UseBasicParsing
}
Set-Location $dir
$ver = (Select-String -Path collector_v6.py -Pattern 'LAST_HEADER = None' -Quiet)
if (-not $ver) { Write-Host 'Update failed: collector is not v6.3. Check the internet connection and run again.' -ForegroundColor Red; exit 1 }
if (-not (Test-Path (Join-Path $out 'calibration.json'))) {
    Write-Host 'No calibration found. Run calibration first:' -ForegroundColor Yellow
    python collector_v6.py calibrate --output $out
}

Write-Host ''
Write-Host 'Scripts updated (v6.3). Collecting now - keep your hands off the mouse.' -ForegroundColor Green
Write-Host 'To stop: press Ctrl+C here. To resume later: run the same one-line command again.' -ForegroundColor Green
Write-Host ''
python collector_v6.py collect --output $out --all --history-scrolls 60
$code = $LASTEXITCODE

Write-Host ''
Write-Host 'Analyzing saved conversations...' -ForegroundColor Green
python analyze_needs.py --output $out
$report = Join-Path $out 'playbook\needs_report.html'
if (Test-Path $report) { Start-Process $report }

if ($code -ne 0) {
    Write-Host ''
    Write-Host "Collection stopped before the end (code $code). Run the same one-line command again to resume." -ForegroundColor Yellow
} else {
    Write-Host ''
    Write-Host 'Collection complete. Report opened.' -ForegroundColor Green
}
