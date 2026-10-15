@echo off
REM ============================================================
REM  Flat Studio - SETUP (chay 1 lan, KHONG can quyen admin)
REM  Tu tai Python + Node + Git portable vao %USERPROFILE%\dsds_studio\tools
REM  Can Windows 10 1803+ (co san curl.exe + tar.exe), CPU x64.
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"
set "TOOLS=%USERPROFILE%\dsds_studio\tools"
if not exist "%TOOLS%" mkdir "%TOOLS%"
call "%~dp0_env.bat"

where tar  >nul 2>&1 || echo   !! Thieu tar.exe  - can Windows 10 1803+.
where curl >nul 2>&1 || echo   !! Thieu curl.exe - can Windows 10 1803+.

echo == 1. Python 3 ==
where python >nul 2>&1 && (echo   da co: & python --version) || call :getpython
echo == 2. Node + npm (cho codex) ==
where npm >nul 2>&1 && (echo   da co npm) || call :getnode
echo == 3. Git + Git Bash (cho autoupdate) ==
where bash >nul 2>&1 && (echo   da co bash) || call :getgit
call "%~dp0_env.bat"

echo == 4. pip: aiohttp + thu vien anh ==
python -m pip install "aiohttp>=3.9" pillow numpy opencv-python-headless scipy "openai>=1.0" python-dotenv ^
 || python -m pip install --user "aiohttp>=3.9" pillow numpy opencv-python-headless scipy "openai>=1.0" python-dotenv

echo == 5. codex (npm global) ==
where codex >nul 2>&1 && echo   da co codex || call npm install -g @openai/codex

echo == 6. Gen chinh: chatgpt-imagegen (script python, chay qua python) ==
if exist "%~dp0bin\chatgpt-imagegen" (echo   OK: bin\chatgpt-imagegen) else (echo   !! Thieu bin\chatgpt-imagegen - copy script python vao thu muc bin\)

echo == 7. .env (ARK_API_KEY cho dola-seed) ==
if exist "%~dp0.env" (
  findstr /b /c:"ARK_API_KEY=" "%~dp0.env" >nul 2>&1 && (echo   OK: co ARK_API_KEY) || (echo   !! .env chua co dong ARK_API_KEY=ark-...)
) else (echo   !! Chua co .env - tao file .env voi dong:  ARK_API_KEY=ark-...)

echo == 8. OpenArt CLI (fallback) - tai ban Windows + dang nhap ==
if exist "%~dp0bin\openart.exe" (
  echo   da co bin\openart.exe
) else (
  echo   tai openart.exe ^(Windows^) tu GitHub release...
  if not exist "%~dp0bin" mkdir "%~dp0bin"
  call :dl "https://github.com/OpenArt-AI/cli/releases/download/v0.1.1/openart_0.1.1_windows_amd64.zip" "%TEMP%\dsds_oa.zip" && (
    "%SystemRoot%\System32\tar.exe" -xf "%TEMP%\dsds_oa.zip" -C "%~dp0bin" openart.exe & del "%TEMP%\dsds_oa.zip" 2>nul
  ) || echo   !! tai openart.exe that bai - bo qua fallback OpenArt
)
if exist "%~dp0bin\openart.exe" (
  echo   Mo dang nhap OpenArt trong trinh duyet. Hoan tat roi quay lai day...
  "%~dp0bin\openart.exe" login
  echo   ^(da xong OpenArt^)
)

echo == 9. Dang nhap ChatGPT (codex, gen chinh) ==
if exist "%USERPROFILE%\.codex\auth.json" (
  echo   OK: da dang nhap codex
) else (
  echo   Mo dang nhap ChatGPT/codex trong trinh duyet. Hoan tat roi quay lai...
  where codex >nul 2>&1 && call codex login
)

echo.
echo XONG. Chay:  start.bat
echo.
pause
goto :eof

:getpython
echo   tai Python standalone portable...
call :dl "https://github.com/astral-sh/python-build-standalone/releases/download/20241206/cpython-3.12.8+20241206-x86_64-pc-windows-msvc-install_only.tar.gz" "%TEMP%\dsds_py.tar.gz" || (echo   !! tai Python that bai & goto :eof)
tar -xf "%TEMP%\dsds_py.tar.gz" -C "%TOOLS%" & del "%TEMP%\dsds_py.tar.gz" 2>nul
if exist "%TOOLS%\python\python.exe" copy /y "%TOOLS%\python\python.exe" "%TOOLS%\python\python3.exe" >nul
echo   Python -^> %TOOLS%\python
goto :eof

:getnode
echo   tai Node portable...
call :dl "https://nodejs.org/dist/v22.11.0/node-v22.11.0-win-x64.zip" "%TEMP%\dsds_node.zip" || (echo   !! tai Node that bai & goto :eof)
tar -xf "%TEMP%\dsds_node.zip" -C "%TOOLS%" & del "%TEMP%\dsds_node.zip" 2>nul
if exist "%TOOLS%\node" rmdir /s /q "%TOOLS%\node"
move "%TOOLS%\node-v22.11.0-win-x64" "%TOOLS%\node" >nul
call "%TOOLS%\node\npm.cmd" config set prefix "%TOOLS%\node" >nul 2>&1
echo   Node -^> %TOOLS%\node
goto :eof

:getgit
echo   tai PortableGit...
call :dl "https://github.com/git-for-windows/git/releases/download/v2.47.1.windows.1/PortableGit-2.47.1-64-bit.7z.exe" "%TEMP%\dsds_git.exe" || (echo   !! tai Git that bai & goto :eof)
"%TEMP%\dsds_git.exe" -o"%TOOLS%\git" -y >nul & del "%TEMP%\dsds_git.exe" 2>nul
echo   Git -^> %TOOLS%\git
goto :eof

:dl
curl -fL "%~1" -o "%~2" 2>nul && exit /b 0
powershell -NoProfile -Command "try{[Net.ServicePointManager]::SecurityProtocol='Tls12';Invoke-WebRequest -Uri '%~1' -OutFile '%~2'}catch{exit 1}" && exit /b 0
exit /b 1
