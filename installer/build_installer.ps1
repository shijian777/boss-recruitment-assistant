$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$distDir = Join-Path $projectRoot "dist"
$packageZip = Join-Path $distDir "BossInviter-Windows-x64.zip"
$outputSetup = Join-Path $distDir "BossInviter-Setup.exe"
$stageDir = Join-Path $projectRoot "build\installer"
$sedPath = Join-Path $stageDir "BossInviter-Setup.sed"
$iexpress = Join-Path $env:WINDIR "System32\iexpress.exe"

if (-not (Test-Path -LiteralPath $packageZip -PathType Leaf)) {
    throw "请先生成 $packageZip"
}
if (-not (Test-Path -LiteralPath $iexpress -PathType Leaf)) {
    throw "当前 Windows 系统未找到 IExpress：$iexpress"
}

if (Test-Path -LiteralPath $stageDir) {
    Remove-Item -LiteralPath $stageDir -Recurse -Force
}
New-Item -ItemType Directory -Path $stageDir -Force | Out-Null
Copy-Item -LiteralPath $packageZip -Destination $stageDir
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "install.ps1") -Destination $stageDir

$sed = @"
[Version]
Class=IEXPRESS
SEDVersion=3

[Options]
PackagePurpose=InstallApp
ShowInstallProgramWindow=0
HideExtractAnimation=1
UseLongFileName=1
InsideCompressed=0
CAB_FixedSize=0
CAB_ResvCodeSigning=0
RebootMode=N
InstallPrompt=%InstallPrompt%
DisplayLicense=
FinishMessage=
TargetName=$outputSetup
FriendlyName=%FriendlyName%
AppLaunched=%AppLaunched%
PostInstallCmd=<None>
AdminQuietInstCmd=
UserQuietInstCmd=
SourceFiles=SourceFiles

[SourceFiles]
SourceFiles0=$stageDir\

[SourceFiles0]
%FILE0%=
%FILE1%=

[Strings]
InstallPrompt=是否安装 BossInviter？
FriendlyName=BossInviter 安装程序
AppLaunched=powershell.exe -NoProfile -ExecutionPolicy Bypass -File install.ps1
FILE0=BossInviter-Windows-x64.zip
FILE1=install.ps1
"@
Set-Content -LiteralPath $sedPath -Value $sed -Encoding Default

Remove-Item -LiteralPath $outputSetup -Force -ErrorAction SilentlyContinue
$builder = Start-Process -FilePath $iexpress -ArgumentList @("/N", "/Q", $sedPath) -PassThru -Wait -WindowStyle Hidden

# IExpress can return before MakeCab finishes writing the output file.
$deadline = [DateTime]::UtcNow.AddMinutes(3)
$outputReady = $false
while ([DateTime]::UtcNow -lt $deadline) {
    if (Test-Path -LiteralPath $outputSetup -PathType Leaf) {
        try {
            $stream = [IO.File]::Open($outputSetup, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None)
            $stream.Dispose()
            $outputReady = $true
            break
        }
        catch [IO.IOException] {
            # MakeCab is still writing; retry shortly.
        }
    }
    Start-Sleep -Milliseconds 500
}
if ($builder.ExitCode -ne 0 -or -not $outputReady) {
    throw "IExpress 生成安装包失败，退出码：$($builder.ExitCode)"
}

Write-Output "安装包已生成：$outputSetup"
