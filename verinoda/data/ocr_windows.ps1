# Verinoda: text of images with the OCR engine built into Windows (Windows.Media.Ocr).
# Reads the image paths listed one per line in -List (UTF-8) and writes
# [{"path": ..., "lang": ..., "lines": [...]} | {"path": ..., "error": ...}] as UTF-8 JSON to -Out.
# Local files only: nothing is downloaded, installed or sent anywhere.
param([Parameter(Mandatory = $true)][string]$List, [Parameter(Mandatory = $true)][string]$Out)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $type) {
    $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
    $t.Wait(-1) | Out-Null
    $t.Result
}
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
$results = New-Object System.Collections.ArrayList
$paths = [System.IO.File]::ReadAllLines($List, [System.Text.Encoding]::UTF8)
foreach ($p in $paths) {
    if (-not $p) { continue }
    $stream = $null
    try {
        if ($null -eq $engine) { throw 'no OCR language is installed for this Windows user' }
        $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($p)) ([Windows.Storage.StorageFile])
        $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        if ($bitmap.PixelWidth -gt [Windows.Media.Ocr.OcrEngine]::MaxImageDimension -or
            $bitmap.PixelHeight -gt [Windows.Media.Ocr.OcrEngine]::MaxImageDimension) {
            throw 'larger than the OCR engine reads'
        }
        $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        $lines = @($result.Lines | ForEach-Object { $_.Text })
        [void]$results.Add(@{ path = $p; lang = $engine.RecognizerLanguage.LanguageTag; lines = $lines })
    } catch {
        [void]$results.Add(@{ path = $p; error = $_.Exception.Message })
    } finally {
        if ($null -ne $stream) { $stream.Dispose() }
    }
}
$json = ConvertTo-Json -InputObject @($results) -Depth 4 -Compress
[System.IO.File]::WriteAllText($Out, $json, (New-Object System.Text.UTF8Encoding $false))
