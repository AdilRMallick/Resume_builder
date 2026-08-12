param()

$ErrorActionPreference = "Stop"

$version = "0.17.0"
$archiveName = "tectonic-$version-x86_64-pc-windows-msvc.zip"
$expectedSha256 = "F61CE51F0B0ADE1015B7DE7EF368541C5424E9756ECBD0D7AF97D6D48030845F"
$repositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$toolsRoot = Join-Path $repositoryRoot ".tools"
$installRoot = Join-Path $toolsRoot "tectonic"
$archivePath = Join-Path $toolsRoot $archiveName
$downloadUrl = "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%400.17.0/$archiveName"

New-Item -ItemType Directory -Path $toolsRoot -Force | Out-Null
New-Item -ItemType Directory -Path $installRoot -Force | Out-Null

Write-Host "Downloading Tectonic $version from the official GitHub release..."
Invoke-WebRequest -Uri $downloadUrl -OutFile $archivePath

$actualSha256 = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash
if ($actualSha256 -ne $expectedSha256) {
    throw "Tectonic archive checksum did not match; refusing to install it."
}

Expand-Archive -LiteralPath $archivePath -DestinationPath $installRoot -Force
Remove-Item -LiteralPath $archivePath -Force

$executable = Join-Path $installRoot "tectonic.exe"
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "The Tectonic archive did not contain tectonic.exe."
}

& $executable --version
Write-Host "Installed locally at $executable"
Write-Host "Restart JME to enable exact Jake-template PDF rendering."
