@echo off
rem =====================================================================================
rem Lightmind launcher for Windows. Keep this file pure ASCII with CRLF line ends.
rem
rem It lives in system, the technical folder of the tree. The folder above it is the tree
rem (LIGHTMIND_HOME). Everything this script builds or downloads stays inside system.
rem
rem   system\lm ARGS    start Lightmind; builds the runtime on the first start
rem   system\lm path    add this folder to your user Path, so that lm works in any terminal
rem
rem The runtime (Python and lightmind with its terminal UI) is rebuilt when it is missing or
rem when the tree was moved. A start where nothing changed does not run uv at all.
rem Overrides: LM_UV names another uv program, LM_PYTHON another Python version (3.12).
rem =====================================================================================
setlocal EnableExtensions DisableDelayedExpansion

set "LM_SYS=%~dp0"
if "%LM_SYS:~-1%"=="\" set "LM_SYS=%LM_SYS:~0,-1%"
for %%I in ("%LM_SYS%\..") do set "LM_HOME=%%~fI"
if /i "%~1"=="path" goto :path

set "LM_ARCH=x86_64"
if /i "%PROCESSOR_ARCHITECTURE%"=="ARM64" set "LM_ARCH=arm64"
if /i "%PROCESSOR_ARCHITEW6432%"=="ARM64" set "LM_ARCH=arm64"
if not defined LM_PYTHON set "LM_PYTHON=3.12"
set "LM_BIN=%LM_SYS%\bin"
set "LM_RT=%LM_SYS%\runtime-windows-%LM_ARCH%"
rem The marker is a small file in the runtime folder that holds the tree path (in the console code page,
rem which is also how it is read back); a moved tree finds another path in it and rebuilds.
set "LM_MARK=%LM_RT%\lm-root.txt"

rem The portable environment: the same values as Home.runtime_env() and its siblings. system\bin goes
rem LAST on PATH: a file dropped there must never shadow a system program (git, bash, python). This
rem script calls its uv by the full path, so nothing depends on the position.
set "LIGHTMIND_HOME=%LM_HOME%"
set "UV_CACHE_DIR=%LM_SYS%\cache"
set "UV_PYTHON_INSTALL_DIR=%LM_SYS%\python"
set "UV_PYTHON_INSTALL_BIN=0"
set "UV_PYTHON_INSTALL_REGISTRY=0"
set "UV_TOOL_DIR=%LM_SYS%\uv-tools-windows-%LM_ARCH%"
set "UV_TOOL_BIN_DIR=%LM_BIN%"
set "PLAYWRIGHT_BROWSERS_PATH=%LM_SYS%\browsers"
set "PYTHONHOME="
set "PYTHONPATH="
set "PATH=%PATH%;%LM_BIN%"

set "LM_UV_AUTO=1"
if defined LM_UV set "LM_UV_AUTO="
if not defined LM_UV set "LM_UV=%LM_BIN%\uv.exe"
rem CALL breaks on a path with , ; = & ^ so it is used only when LM_UV is a batch file.
set "LM_RUN="
if /i "%LM_UV:~-4%"==".cmd" set "LM_RUN=call"
if /i "%LM_UV:~-4%"==".bat" set "LM_RUN=call"

rem Fast path: runtime and marker are there. No uv call, only a look at the files.
if not exist "%LM_RT%\Scripts\lm.exe" goto :build
if not exist "%LM_MARK%" goto :build
set "LM_SEEN="
set /p LM_SEEN=<"%LM_MARK%"
if /i not "%LM_SEEN%"=="%LM_HOME%" goto :build
if defined LM_UV_AUTO if not exist "%LM_UV%" call :get_uv
goto :run

:run
"%LM_RT%\Scripts\lm.exe" %*
exit /b %ERRORLEVEL%

:build
1>&2 echo lm: building the runtime in system\runtime-windows-%LM_ARCH% (first start, or the tree was moved)
if defined LM_UV_AUTO if not exist "%LM_UV%" call :get_uv
if not exist "%LM_UV%" goto :no_uv
call :package
if %ERRORLEVEL% NEQ 0 goto :fail
if exist "%LM_RT%" rmdir /s /q "%LM_RT%"
if exist "%LM_RT%" goto :locked
%LM_RUN% "%LM_UV%" python install %LM_PYTHON% 1>&2
if %ERRORLEVEL% NEQ 0 goto :fail
%LM_RUN% "%LM_UV%" venv --python %LM_PYTHON% --managed-python "%LM_RT%" 1>&2
if %ERRORLEVEL% NEQ 0 goto :fail
rem A wheel kept from an earlier build is tried from the cache first, quietly, so a moved tree needs no network.
if not defined LM_KEPT goto :install_online
%LM_RUN% "%LM_UV%" pip install --offline --python "%LM_RT%\Scripts\python.exe" "%LM_PKG%" >nul 2>&1
if %ERRORLEVEL% EQU 0 goto :installed
:install_online
%LM_RUN% "%LM_UV%" pip install --python "%LM_RT%\Scripts\python.exe" "%LM_PKG%" 1>&2
if %ERRORLEVEL% NEQ 0 goto :fail
:installed
<nul set /p "=%LM_HOME%" >"%LM_MARK%"
if not exist "%LM_MARK%" goto :fail
1>&2 echo lm: runtime ready.
goto :run

:package
rem Sets LM_PKG, the argument for uv pip install, and LM_KEPT when it is a wheel kept in system\wheels.
set "LM_PKG="
set "LM_KEPT="
set "LM_WHEELS=%LM_SYS%\wheels"
for /f "delims=" %%F in ('dir /b /o-d "%LM_WHEELS%\lightmind-*.whl" 2^>nul') do if not defined LM_PKG set "LM_PKG=%LM_WHEELS%\%%F[tui]"
if defined LM_PKG set "LM_KEPT=1"
if defined LM_PKG exit /b 0
rem No kept wheel: use what init recorded, one line: KIND SPEC with KIND folder, wheel or requirement.
set "LM_KIND="
set "LM_SPEC="
set "LM_REC=%LM_SYS%\state\install-source.txt"
if not exist "%LM_REC%" goto :no_source
rem install-source.txt is UTF-8 (a path with umlauts): PowerShell decodes it and hands it to for /f in the
rem console code page, the same one every other path in this script is in.
for /f "tokens=1,* delims= " %%A in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Content -Raw -Encoding UTF8 -LiteralPath $env:LM_REC" 2^>nul') do if not defined LM_KIND set "LM_KIND=%%A" & set "LM_SPEC=%%B"
if /i "%LM_KIND%"=="requirement" goto :pkg_requirement
if /i "%LM_KIND%"=="wheel" goto :pkg_wheel
if /i "%LM_KIND%"=="folder" goto :pkg_folder
goto :no_source

:pkg_requirement
set "LM_PKG=%LM_SPEC%"
exit /b 0

:pkg_wheel
if not exist "%LM_SPEC%" goto :src_missing
if not exist "%LM_WHEELS%" mkdir "%LM_WHEELS%"
copy /y "%LM_SPEC%" "%LM_WHEELS%\" >nul
for %%W in ("%LM_SPEC%") do set "LM_PKG=%LM_WHEELS%\%%~nxW[tui]"
exit /b 0

:pkg_folder
if not exist "%LM_SPEC%\pyproject.toml" goto :src_missing
if not exist "%LM_WHEELS%" mkdir "%LM_WHEELS%"
del /q "%LM_WHEELS%\lightmind-*.whl" 2>nul
%LM_RUN% "%LM_UV%" build --wheel --out-dir "%LM_WHEELS%" "%LM_SPEC%" 1>&2
if %ERRORLEVEL% NEQ 0 exit /b 1
for /f "delims=" %%F in ('dir /b /o-d "%LM_WHEELS%\lightmind-*.whl" 2^>nul') do if not defined LM_PKG set "LM_PKG=%LM_WHEELS%\%%F[tui]"
if not defined LM_PKG exit /b 1
exit /b 0

:get_uv
rem uv into system\bin: a copy of the uv found on PATH, else the official installer, which then
rem touches neither PATH nor any profile (UV_UNMANAGED_INSTALL: no receipt either).
if not exist "%LM_BIN%" mkdir "%LM_BIN%"
set "LM_FOUND="
for /f "delims=" %%P in ('where $PATH:uv.exe 2^>nul') do if not defined LM_FOUND set "LM_FOUND=%%P"
if not defined LM_FOUND goto :get_uv_download
copy /y "%LM_FOUND%" "%LM_BIN%\uv.exe" >nul
for %%D in ("%LM_FOUND%") do if exist "%%~dpDuvx.exe" copy /y "%%~dpDuvx.exe" "%LM_BIN%\uvx.exe" >nul
"%LM_BIN%\uv.exe" --version >nul 2>&1
if %ERRORLEVEL% EQU 0 exit /b 0
del /q "%LM_BIN%\uv.exe" "%LM_BIN%\uvx.exe" 2>nul
:get_uv_download
1>&2 echo lm: no uv found, downloading it into system\bin
set "UV_UNMANAGED_INSTALL=%LM_BIN%"
set "UV_INSTALL_DIR=%LM_BIN%"
set "UV_NO_MODIFY_PATH=1"
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = 'Tls12'; irm https://astral.sh/uv/install.ps1 | iex" 1>&2
set "UV_UNMANAGED_INSTALL="
set "UV_INSTALL_DIR="
set "UV_NO_MODIFY_PATH="
if exist "%LM_BIN%\uv.exe" exit /b 0
exit /b 1

:path
rem Add system to the USER Path (registry, HKCU Environment): no duplicates, the value type is kept,
rem nothing outside that one value is written. Not setx, which cuts long values. LM_PATH_REGKEY
rem names another key below HKCU; the tests use it so the real Path stays untouched.
set "LM_PATH_DIR=%LM_SYS%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d = $env:LM_PATH_DIR.TrimEnd('\'); $sub = 'Environment'; if ($env:LM_PATH_REGKEY) { $sub = $env:LM_PATH_REGKEY }; $k = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey($sub); $raw = [string]$k.GetValue('Path', '', 'DoNotExpandEnvironmentNames'); $have = @($raw -split ';' | Where-Object { $_ -ne '' }); foreach ($p in $have) { if ([Environment]::ExpandEnvironmentVariables($p).TrimEnd('\') -ieq $d) { Write-Host ('lm: ' + $d + ' is already on your user Path'); exit 0 } }; $kind = 'ExpandString'; try { $kind = $k.GetValueKind('Path') } catch { }; $k.SetValue('Path', (($have + $d) -join ';'), $kind); if ($sub -eq 'Environment') { [Environment]::SetEnvironmentVariable('LM_PATH_NOTIFY', $null, 'User') }; Write-Host ('lm: added ' + $d + ' to your user Path. Open a new terminal and type lm')"
exit /b %ERRORLEVEL%

:no_uv
1>&2 echo lm: uv is not available and could not be downloaded. Check the network, or put uv.exe into system\bin.
exit /b 1

:no_source
1>&2 echo lm: system\state\install-source.txt is missing or unreadable, so the launcher does not know where lightmind comes from.
1>&2 echo lm: run lm update from an installed lm, or write a line like  requirement lightmind[tui]  into that file.
exit /b 1

:src_missing
1>&2 echo lm: the lightmind source named in system\state\install-source.txt does not exist on this machine.
1>&2 echo lm: fix that line, or put a lightmind wheel into system\wheels.
exit /b 1

:locked
1>&2 echo lm: cannot remove the old runtime. Close every other lm window and try again.
exit /b 1

:fail
1>&2 echo lm: the runtime could not be built. Fix the problem named above and start lm again.
exit /b 1
