param(
  [Parameter(Mandatory=$true)][string]$Dir,
  [Parameter(Mandatory=$true)][string]$Out
)
# Windows.Media.Ocr bridge (ASCII only so Windows PowerShell 5.1 can parse it).
# Reads every png in $Dir, writes JSON [{name, lines}] to $Out. No extra packages.
# NOTE: Windows.Storage.StorageFile is blocked for console children (UnauthorizedAccess),
# so image bytes are loaded with [System.IO.File]::ReadAllBytes + an in-memory stream.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.InMemoryRandomAccessStream,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.DataWriter,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language,Windows.Foundation,ContentType=WindowsRuntime]

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]

function Await($WinRtTask, $ResultType, $Label) {
  try {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    return $netTask.Result
  } catch {
    $inner = $_.Exception.InnerException
    while ($null -ne $inner.InnerException) { $inner = $inner.InnerException }
    throw ("OCR_STEP_FAIL " + $Label + " :: " + $inner.Message)
  }
}

$engine = $null
foreach ($tag in @('zh-Hans-CN', 'zh-Hans', 'zh-CN')) {
  try {
    $language = New-Object Windows.Globalization.Language $tag
    $created = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
    if ($null -ne $created) { $engine = $created; break }
  } catch {
    $engine = $null
  }
}
if ($null -eq $engine) { throw 'NO_OCR_LANGUAGE' }

$rows = @()
foreach ($f in (Get-ChildItem -Path $Dir -Filter '*.png' -ErrorAction SilentlyContinue | Sort-Object Name)) {
  $bytes = [System.IO.File]::ReadAllBytes($f.FullName)
  $stream = New-Object Windows.Storage.Streams.InMemoryRandomAccessStream
  $writer = New-Object Windows.Storage.Streams.DataWriter $stream
  $writer.WriteBytes($bytes)
  $null = Await ($writer.StoreAsync()) ([uint32]) 'store'
  $writer.DetachStream() | Out-Null
  try {
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder]) 'decoder'
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap]) 'bitmap'
    $bgra = [Windows.Graphics.Imaging.SoftwareBitmap]::Convert($bitmap, [Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8, [Windows.Graphics.Imaging.BitmapAlphaMode]::Ignore)
    $result = Await ($engine.RecognizeAsync($bgra)) ([Windows.Media.Ocr.OcrResult]) 'recognize'
    $lines = @()
    foreach ($line in $result.Lines) {
      $t = $line.Text.Trim()
      if ($t) { $lines += $t }
    }
    $rows += [pscustomobject]@{ name = $f.Name; lines = $lines }
  } finally {
    $stream.Dispose()
  }
}
$rows | ConvertTo-Json -Depth 5 -Compress | Set-Content -Path $Out -Encoding UTF8
"OCR_OK files=" + $rows.Count
