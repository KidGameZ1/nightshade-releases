$ErrorActionPreference = 'Stop'
# Match the current Windows SDK headers with a pinned official LLVM build.
# This is a runner tool only; no compiler is bundled in the core applications.
$installer = Join-Path $env:RUNNER_TEMP 'LLVM-23.1.6-win64.msi'
$url = 'https://github.com/llvm/llvm-project/releases/download/llvmorg-23.1.6/LLVM-23.1.6-win64.msi'
$expected = '81f7eb520ca698eb2d5cd8bc4b8f24f7add9ed66ed40a6f722a6e6f060737c00'
Invoke-WebRequest -Uri $url -OutFile $installer
$actual = (Get-FileHash -Algorithm SHA256 $installer).Hash.ToLowerInvariant()
if ($actual -ne $expected) { throw 'Official LLVM installer checksum differs.' }
$result = Start-Process msiexec.exe -Wait -PassThru -ArgumentList @('/i', ('"' + $installer + '"'), '/qn', '/norestart')
if ($result.ExitCode -notin @(0,3010)) { throw "LLVM installation failed: $($result.ExitCode)" }
Remove-Item -LiteralPath $installer
& 'C:\Program Files\LLVM\bin\clang++.exe' --version
if ($LASTEXITCODE -ne 0) { throw 'Installed LLVM compiler could not start.' }
