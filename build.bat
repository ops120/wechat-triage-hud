@echo off
rem ============================================================================
rem  One-click build: package the HUD into a standalone exe folder.
rem
rem  Output : dist\wechat-triage-hud\wechat-triage-hud.exe
rem           (ship the whole folder; data goes to out\ and .env NEXT TO the exe,
rem            not into a temp dir -- see wechat_triage_hud\paths.py)
rem  Optional: build.bat --zip   also produce a distributable zip that
rem           EXCLUDES .env and out\ (so your key and chat logs never ship)
rem
rem  This script also does three things that are easy to forget:
rem    1) keeps your existing dist\...\.env  (a rebuild used to wipe your key)
rem    2) keeps your existing dist\...\out\  (audit log / settings / history)
rem    3) runs --selftest-ocr and FAILS LOUDLY if OCR is broken
rem       (testing "the exe starts" is not enough -- that is how a broken
rem        rapidocr submodule once slipped through to a user)
rem
rem  NOTE: this file is intentionally ASCII-only. cmd.exe parses .bat using the
rem  console codepage; non-ASCII bytes in comments get mis-parsed on GBK systems.
rem ============================================================================
setlocal enabledelayedexpansion
chcp 65001 >nul
cd /d "%~dp0"

rem ---- optional: build inside a pip venv instead of the active environment ----
rem   build.bat --venv  -> creates .venv-build, installs deps there, builds with it.
rem   Why: PyInstaller officially assumes a pip env; the PySide6 wheels carry their own
rem   DLLs, so the conda-specific DLL collection in the .spec becomes unnecessary.
rem   (Either environment works -- the script just uses whatever "python" resolves to.)
set "VENV_DIR=%CD%\.venv-build"
if /i "%~1"=="--venv" (
    if not exist "%VENV_DIR%\Scripts\python.exe" (
        echo.
        echo [venv] Creating .venv-build and installing dependencies ^(first run: several minutes^)
        python -m venv "%VENV_DIR%"
        "%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade pip -q
        "%VENV_DIR%\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller
        if errorlevel 1 ( echo   X venv setup failed & pause & exit /b 1 )
    ) else (
        echo.
        echo [venv] Reusing existing .venv-build
    )
    set "PATH=%VENV_DIR%\Scripts;%PATH%"
)

echo.
echo [1/7] Checking Python and dependencies
where python >nul 2>nul
if errorlevel 1 (
    echo   X python not found - install Python 3.10+ and tick "Add to PATH"
    pause & exit /b 1
)
python -c "import sys; assert sys.version_info >= (3,10), sys.version"
if errorlevel 1 ( echo   X Python 3.10+ required & pause & exit /b 1 )
python -c "import PyInstaller" 2>nul
if errorlevel 1 ( echo   X PyInstaller missing - run: pip install pyinstaller & pause & exit /b 1 )
python -c "import PySide6, rapidocr_onnxruntime, mss, cv2, win32gui, numpy" 2>nul
if errorlevel 1 ( echo   X runtime deps missing - run: pip install -r requirements.txt & pause & exit /b 1 )
python -c "import sys; print('  env: ' + sys.prefix + '  [' + sys.version.split()[0] + ', ' + ('conda' if 'conda' in sys.prefix.lower() else 'venv/system') + ']')"
python -c "import sys; sys.exit(1 if 'conda' in sys.prefix.lower() else 0)"
if not errorlevel 1 (
    echo   ! conda environment detected: conda keeps Qt/PySide6/libffi DLLs in Library\bin.
    echo     The .spec already collects those by name; if you still hit a DLL ImportError,
    echo     rebuild with:  build.bat --venv
)
echo   OK

echo.
echo [2/7] Generating icon (needs Pillow; skipped if absent)
rem 用 goto 而不是 if(...)else(...)：块内的 echo 文本里带未转义的 ")" 会提前闭合语句块，
rem cmd 会报 "was unexpected at this time"（这个是真踩到的）。
python -c "from PIL import Image; Image.open(r'wechat_triage_hud\assets\ball.png').save(r'wechat_triage_hud\assets\ball.ico', sizes=[(16,16),(32,32),(48,48),(64,64)])" 2>nul
if errorlevel 1 goto icon_skip
echo   OK
goto icon_done
:icon_skip
echo   skipped - no Pillow or no source png, exe will use the default icon
:icon_done

echo.
echo [3/7] Backing up dist\.env and dist\out (a full rebuild wipes dist)
set "ENV_BAK="
set "OUT_BAK="
if exist "dist\wechat-triage-hud\.env" (
    set "ENV_BAK=%TEMP%\wth_env_bak"
    copy /y "dist\wechat-triage-hud\.env" "!ENV_BAK!" >nul
    echo   kept your .env ^(restored after the build^)
) else (
    echo   (no .env in dist - skipped)
)
if exist "dist\wechat-triage-hud\out" (
    set "OUT_BAK=%TEMP%\wth_out_bak"
    if exist "!OUT_BAK!" rmdir /s /q "!OUT_BAK!"
    xcopy /e /i /q /y "dist\wechat-triage-hud\out" "!OUT_BAK!" >nul
    echo   kept dist\out\ ^(audit log / group meta / settings / history^)
) else (
    echo   (no out\ in dist - skipped)
)

echo.
echo [4/7] Cleaning previous build
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
echo   OK

echo.
echo [5/7] Building (3-5 min: PySide6 + offline OCR model are big)
python -m PyInstaller --noconfirm --clean packaging\wechat-triage-hud.spec
if errorlevel 1 ( echo   X build failed, see the errors above & pause & exit /b 1 )
echo   OK

echo.
echo [6/7] Shipping template + user guide, restoring .env / out
copy /y ".env.example" "dist\wechat-triage-hud\.env.example" >nul
copy /y "packaging\guide.txt" "dist\wechat-triage-hud\guide.txt" >nul
if defined ENV_BAK (
    copy /y "!ENV_BAK!" "dist\wechat-triage-hud\.env" >nul
    del "!ENV_BAK!" >nul 2>nul
    echo   restored .env  ^(DELETE it before shipping dist to anyone else^)
) else (
    echo   tip: rename .env.example to .env and put your key in, to use this exe
)
if defined OUT_BAK (
    xcopy /e /i /q /y "!OUT_BAK!" "dist\wechat-triage-hud\out" >nul
    rmdir /s /q "!OUT_BAK!"
    echo   restored dist\out\  ^(your packaged-app data^)
)

echo.
echo [7/7] Self-check: can the packaged exe actually run OCR?
if exist "dist\wechat-triage-hud\out\selftest_ocr.txt" del /q "dist\wechat-triage-hud\out\selftest_ocr.txt"
"dist\wechat-triage-hud\wechat-triage-hud.exe" --selftest-ocr
if errorlevel 1 (
    echo   X OCR self-check FAILED - do NOT ship this build. Details:
    type "dist\wechat-triage-hud\out\selftest_ocr.txt" 2>nul
    pause & exit /b 1
)
type "dist\wechat-triage-hud\out\selftest_ocr.txt"
echo   OK

if /i "%~1"=="--zip" (
    echo.
    echo [extra] Zipping ^(excluding .env and out\^)
    powershell -NoProfile -Command "$src='dist\wechat-triage-hud'; $dst='dist\wechat-triage-hud-win.zip'; Get-ChildItem -Force $src | Where-Object { $_.Name -notin @('.env','out') } | Compress-Archive -DestinationPath $dst -Force; Write-Host ('  ' + (Resolve-Path $dst))"
)

echo.
echo ============================ DONE ============================
for /f %%A in ('powershell -NoProfile -Command "[math]::Round((Get-ChildItem -Recurse dist\wechat-triage-hud | Measure-Object -Property Length -Sum).Sum/1MB)"') do set SIZE=%%A
echo   folder : %CD%\dist\wechat-triage-hud\
echo   exe    : %CD%\dist\wechat-triage-hud\wechat-triage-hud.exe
echo   size   : ~!SIZE! MB
echo.
echo   To ship: the whole dist\wechat-triage-hud folder (or the --zip archive).
echo   Reminder: the shipped copy must NOT contain .env (that is your key).
echo.
pause
endlocal
