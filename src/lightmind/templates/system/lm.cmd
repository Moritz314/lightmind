@echo off
rem Keep this file pure ASCII with CRLF line ends. Do not edit the padding block below.
rem
rem Why the jump and the padding: cmd.exe does not read a batch file once, it reopens the file by NAME after
rem every command and goes on at the byte offset where the last command ended. A window that is still
rem running an OLD copy of this launcher (waiting for lm.exe) therefore reads the NEW file from the old offset
rem when lm.exe ends. The old launchers continue at byte 1190, 2859, 2972, 3365 or 3403 (right behind their
rem lm.exe line, which was followed by "exit /b %ERRORLEVEL%"). The block of lines below covers all of these
rem offsets with leading blanks followed by the same exit, so an old window leaves with the exit code of lm.exe
rem whatever offset it comes back to. Keep every old offset inside the block (tests/test_launcher.py checks it).
rem The new launcher itself never reads on after it started lm.exe, see :go.
goto :lm_main
                                                               exit /b %ERRORLEVEL%
                                                               exit /b %ERRORLEVEL%
                                                               exit /b %ERRORLEVEL%
                                                               exit /b %ERRORLEVEL%
                                                               exit /b %ERRORLEVEL%
                                                               exit /b %ERRORLEVEL%
                                                               exit /b %ERRORLEVEL%
                                                               exit /b %ERRORLEVEL%
                                                               exit /b %ERRORLEVEL%
                                                               exit /b %ERRORLEVEL%
                                                               exit /b %ERRORLEVEL%
                                                               exit /b %ERRORLEVEL%
                                                               exit /b %ERRORLEVEL%
                                                               exit /b %ERRORLEVEL%
                                                               exit /b %ERRORLEVEL%
                                                               exit /b %ERRORLEVEL%
                                                               exit /b %ERRORLEVEL%
                                                               exit /b %ERRORLEVEL%
                                                               exit /b %ERRORLEVEL%
                                                               exit /b %ERRORLEVEL%
                                                               exit /b %ERRORLEVEL%
                                                               exit /b %ERRORLEVEL%
                                                               exit /b %ERRORLEVEL%
                                                               exit /b %ERRORLEVEL%
                                                               exit /b %ERRORLEVEL%
                                                               exit /b %ERRORLEVEL%
                                                               exit /b %ERRORLEVEL%
                                                               exit /b %ERRORLEVEL%
                                                               exit /b %ERRORLEVEL%
                                                               exit /b %ERRORLEVEL%
:lm_main
rem =====================================================================================
rem Lightmind launcher for Windows.
rem
rem It lives in system, the technical folder of the tree. The folder above it is the tree
rem (LIGHTMIND_HOME). Everything this script builds or downloads stays inside system.
rem
rem   system\lm ARGS    start Lightmind; builds the runtime on the first start
rem   system\lm build   only prepare the runtime (build it when a new version is pending), start nothing
rem   system\lm path    add this folder to your user Path, so that lm works in any terminal
rem
rem Runtimes (Python and lightmind with its terminal UI) live side by side: system\runtimes\windows-ARCH\rN is one
rem build, the one-line file system\runtimes\windows-ARCH.current names the runtime that new starts use. A build
rem goes into a folder of its own and the pointer is switched only when it stands complete, so a window that
rem still runs an older runtime is never disturbed; it stays on its version until it ends. A runtime folder that
rem nobody uses any more (a running program cannot be opened for writing: that is the proof for its lm.exe and
rem python.exe) is removed at the next start. A new version is pending when system\state\runtime-stamp.txt (written by lm update) differs
rem from lm-stamp.txt inside the runtime, or when the tree was moved (lm-root.txt). A start where nothing changed
rem does not run uv at all. The runtime of earlier launchers, system\runtime-windows-ARCH, is run as long as it
rem is current and is removed once a new runtime exists and nobody uses it.
rem Overrides: LM_UV names another uv program, LM_PYTHON another Python version (3.12), LM_LOCK_WAIT how many
rem seconds a start waits for a build in another window when it has no runtime to start (300).
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
if not defined LM_LOCK_WAIT set "LM_LOCK_WAIT=300"
set "LM_BIN=%LM_SYS%\bin"
set "LM_RTS=%LM_SYS%\runtimes\windows-%LM_ARCH%"
set "LM_PTR=%LM_RTS%.current"
set "LM_LEGACY=%LM_SYS%\runtime-windows-%LM_ARCH%"
set "LM_STAMPF=%LM_SYS%\state\runtime-stamp.txt"
set "LM_LOCK=%LM_SYS%\build.lock"
set "LM_ONLY="
if /i "%~1"=="build" if "%~2"=="" set "LM_ONLY=1"

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

set "LM_RETRY="
set "LM_WAITED=0"

:resolve
rem Decide which runtime runs. :look sets LM_RT (the runtime to start, if there is a usable one) and LM_FRESH (it
rem is current: built from what the tree wants now). A fresh runtime is started without a look at uv.
call :look
if defined LM_ONLY goto :only
if not defined LM_FRESH goto :need_build
if defined LM_UV_AUTO if not exist "%LM_UV%" call :get_uv
call :tidy_soon
goto :run

:run
if exist "%LM_RT%\Scripts\lm.exe" goto :go
rem The runtime named a moment ago was removed or replaced by another window: look once more.
if defined LM_RETRY goto :gone
set "LM_RETRY=1"
goto :resolve

:go
rem The exit is on the SAME line as the call: cmd would otherwise reopen this file after lm.exe ended and read on
rem at the old offset, in whatever a later lm update has put there. "call exit /b %%ERRORLEVEL%%" hands the exit
rem code of lm.exe on (a bare "exit /b" would end with code 0).
"%LM_RT%\Scripts\lm.exe" %* & call exit /b %%ERRORLEVEL%%

:gone
1>&2 echo lm: the runtime vanished while it was started. Start lm again.
exit /b 1

:only
if not defined LM_FRESH goto :need_build
1>&2 echo lm: the runtime is up to date.
exit /b 0

:need_build
rem One build at a time: build.lock is a file held open (handle 9) while :rebuild runs, so Windows frees it
rem when the window dies, even after Ctrl+C. The quiet probe first tells a busy lock from a free one without the
rem error message of cmd. If the file cannot be opened, :rebuild never ran (LM_RAN unset).
set "LM_PROBE="
(9>>"%LM_LOCK%" call :probe) 2>nul
if not defined LM_PROBE goto :busy
set "LM_RAN="
9>"%LM_LOCK%" call :rebuild
set "LM_RC=%ERRORLEVEL%"
if not defined LM_RAN goto :busy
if "%LM_RC%"=="0" goto :built
goto :build_failed

:probe
set "LM_PROBE=1"
exit /b 0

:built
if defined LM_ONLY exit /b 0
goto :run

:busy
rem Another window is building. With a runtime to start, start it and say so; without one (or for "build") wait.
if not defined LM_ONLY if defined LM_RT 1>&2 echo lm: a new version is being built in another lm window right now. This window starts the current one.
if not defined LM_ONLY if defined LM_RT goto :run
if %LM_WAITED% EQU 0 1>&2 echo lm: another lm window is building the runtime, waiting for it
set /a LM_WAITED+=1
if %LM_WAITED% GTR %LM_LOCK_WAIT% goto :wait_over
ping -n 2 127.0.0.1 >nul
goto :resolve

:wait_over
1>&2 echo lm: the build lock system\build.lock is held for too long. Remove it if no build is running, then start lm again.
exit /b 1

:build_failed
if defined LM_ONLY 1>&2 echo lm: the new runtime could not be built. Fix the problem named above and run lm again.
if defined LM_ONLY exit /b 1
if not defined LM_RT 1>&2 echo lm: the runtime could not be built. Fix the problem named above and start lm again.
if not defined LM_RT exit /b 1
1>&2 echo lm: the new runtime could not be built, this window starts the current one. The next start tries again.
goto :run

:look
rem Looks at the files, starts nothing. LM_ID: the runtime named by the pointer. LM_WANT: the stamp the tree wants
rem (empty when lm update never asked for anything). A runtime of the pointer is usable when lm.exe and the marker
rem of THIS tree are there, fresh when its stamp is the wanted one. The marker is written last by the build.
set "LM_RT="
set "LM_FRESH="
set "LM_ID="
set "LM_WANT="
set "LM_HAVE="
if exist "%LM_STAMPF%" set /p LM_WANT=<"%LM_STAMPF%"
if exist "%LM_PTR%" set /p LM_ID=<"%LM_PTR%"
if not defined LM_ID goto :look_legacy
if not "%LM_ID:\=%"=="%LM_ID%" set "LM_ID="
if not defined LM_ID goto :look_legacy
if not "%LM_ID:.=%"=="%LM_ID%" set "LM_ID="
if not defined LM_ID goto :look_legacy
if not exist "%LM_RTS%\%LM_ID%\Scripts\lm.exe" goto :look_legacy
set "LM_SEEN="
if exist "%LM_RTS%\%LM_ID%\lm-root.txt" set /p LM_SEEN=<"%LM_RTS%\%LM_ID%\lm-root.txt"
if /i not "%LM_SEEN%"=="%LM_HOME%" goto :look_legacy
set "LM_RT=%LM_RTS%\%LM_ID%"
if exist "%LM_RT%\lm-stamp.txt" set /p LM_HAVE=<"%LM_RT%\lm-stamp.txt"
if "%LM_HAVE%"=="%LM_WANT%" set "LM_FRESH=1"
exit /b 0

:look_legacy
rem The runtime of earlier launchers. Without a marker an old lm update has outdated it: it may run, but it is
rem never fresh. With the marker of this tree it is fresh as long as lm update asked for nothing. Another marker
rem means a moved tree: not usable.
if not exist "%LM_LEGACY%\Scripts\lm.exe" exit /b 0
if exist "%LM_LEGACY%\lm-root.txt" goto :look_legacy_marked
set "LM_RT=%LM_LEGACY%"
exit /b 0
:look_legacy_marked
set "LM_SEEN="
set /p LM_SEEN=<"%LM_LEGACY%\lm-root.txt"
if /i not "%LM_SEEN%"=="%LM_HOME%" exit /b 0
set "LM_RT=%LM_LEGACY%"
if not defined LM_WANT set "LM_FRESH=1"
exit /b 0

:tidy_soon
rem Cheap look: is there any runtime folder besides the one that runs? Only then the lock is tried and the
rem folders are probed; a busy lock means a build is running, which tidies up itself at its end.
set "LM_JUNK="
for /d %%D in ("%LM_RTS%\*") do if /i not "%%~fD"=="%LM_RT%" set "LM_JUNK=1"
if exist "%LM_LEGACY%" if /i not "%LM_LEGACY%"=="%LM_RT%" set "LM_JUNK=1"
if defined LM_JUNK call :tidy_locked
exit /b 0

:tidy_locked
(9>"%LM_LOCK%" call :tidy) 2>nul
exit /b 0

:tidy
rem Removes every runtime folder that is not the one that runs, not the one the pointer names and not in use (see
rem :drop); the lock is held, so no build is half way. Silent.
for /d %%D in ("%LM_RTS%\*") do call :drop "%%~fD"
if exist "%LM_LEGACY%" call :drop "%LM_LEGACY%"
exit /b 0

:drop
rem Folder %1. It is in use when its lm.exe or python.exe runs: a running program cannot be opened for writing, and
rem the append below fails then (renaming the folder is NO proof: Windows allows it while programs run inside).
rem Nothing is written; an append of nothing changes neither content nor date.
if /i "%~1"=="%LM_RT%" exit /b 0
if /i "%~nx1"=="%LM_ID%" exit /b 0
set "LM_BUSY="
for %%E in (lm.exe python.exe pythonw.exe) do if exist "%~1\Scripts\%%E" call :probe_exe "%~1\Scripts\%%E"
if defined LM_BUSY exit /b 0
rmdir /s /q "%~1" 2>nul
exit /b 0

:probe_exe
set "LM_OPENED="
(>>"%~1" call :opened) 2>nul
if not defined LM_OPENED set "LM_BUSY=1"
exit /b 0

:opened
set "LM_OPENED=1"
exit /b 0

:rebuild
rem Runs with the lock held. Builds the next runtime in a folder of its own, switches the pointer when it stands
rem and tidies up. Exit codes: 0 a fresh runtime is there, 1 the build failed (nothing else was touched).
set "LM_RAN=1"
call :look
if defined LM_FRESH exit /b 0
call :next_id
1>&2 echo lm: building runtime %LM_NEWID% in system\runtimes\windows-%LM_ARCH% (first start, a new version or a moved tree)
if defined LM_UV_AUTO if not exist "%LM_UV%" call :get_uv
if not exist "%LM_UV%" goto :rb_no_uv
call :package
if %ERRORLEVEL% NEQ 0 exit /b 1
%LM_RUN% "%LM_UV%" python install %LM_PYTHON% 1>&2
if %ERRORLEVEL% NEQ 0 exit /b 1
if not exist "%LM_RTS%" mkdir "%LM_RTS%"
%LM_RUN% "%LM_UV%" venv --python %LM_PYTHON% --managed-python "%LM_NEWRT%" 1>&2
if %ERRORLEVEL% NEQ 0 goto :rb_undo
rem A wheel kept from an earlier build is tried from the cache first, quietly, so a moved tree needs no network.
if not defined LM_KEPT goto :install_online
%LM_RUN% "%LM_UV%" pip install --offline --python "%LM_NEWRT%\Scripts\python.exe" "%LM_PKG%" >nul 2>&1
if %ERRORLEVEL% EQU 0 goto :installed
:install_online
%LM_RUN% "%LM_UV%" pip install --python "%LM_NEWRT%\Scripts\python.exe" "%LM_PKG%" 1>&2
if %ERRORLEVEL% NEQ 0 goto :rb_undo
:installed
rem Stamp first, marker last (the runtime counts as complete with its marker), then the pointer: written to a file
rem of its own and moved over the old one, which is one step for every reader.
if defined LM_WANT <nul set /p "=%LM_WANT%" >"%LM_NEWRT%\lm-stamp.txt"
if not defined LM_WANT type nul >"%LM_NEWRT%\lm-stamp.txt"
<nul set /p "=%LM_HOME%" >"%LM_NEWRT%\lm-root.txt"
if not exist "%LM_NEWRT%\lm-root.txt" goto :rb_undo
if not exist "%LM_NEWRT%\Scripts\lm.exe" goto :rb_undo
<nul set /p "=%LM_NEWID%" >"%LM_PTR%.tmp"
move /y "%LM_PTR%.tmp" "%LM_PTR%" >nul 2>&1
call :look
if /i not "%LM_RT%"=="%LM_NEWRT%" goto :rb_undo
call :tidy
1>&2 echo lm: runtime %LM_NEWID% ready.
exit /b 0

:rb_no_uv
1>&2 echo lm: uv is not available and could not be downloaded. Check the network, or put uv.exe into system\bin.
exit /b 1

:rb_undo
rem The build failed: drop the partial folder. The pointer was not touched, so the current runtime stays.
del "%LM_PTR%.tmp" 2>nul
if exist "%LM_NEWRT%" rmdir /s /q "%LM_NEWRT%" 2>nul
call :look
exit /b 1

:next_id
rem The next free runtime name rN: one more than the highest number in use.
set "LM_N=0"
for /d %%D in ("%LM_RTS%\r*") do call :id_scan "%%~nxD"
set /a LM_N+=1
set "LM_NEWID=r%LM_N%"
set "LM_NEWRT=%LM_RTS%\r%LM_N%"
exit /b 0

:id_scan
set "LM_NM=%~1"
if not "%LM_NM:.=%"=="%LM_NM%" exit /b 0
set "LM_NUM=%LM_NM:~1%"
set "LM_BAD="
for /f "delims=0123456789" %%X in ("%LM_NUM%") do set "LM_BAD=1"
if defined LM_BAD exit /b 0
if not defined LM_NUM exit /b 0
if %LM_NUM% GTR %LM_N% set "LM_N=%LM_NUM%"
exit /b 0

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

:no_source
1>&2 echo lm: system\state\install-source.txt is missing or unreadable, so the launcher does not know where lightmind comes from.
1>&2 echo lm: run lm update from an installed lm, or write a line like  requirement lightmind[tui]  into that file.
exit /b 1

:src_missing
1>&2 echo lm: the lightmind source named in system\state\install-source.txt does not exist on this machine.
1>&2 echo lm: fix that line, or put a lightmind wheel into system\wheels.
exit /b 1
