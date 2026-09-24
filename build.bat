@echo off
rem ============================================================================
rem  一键打包成 exe（双击本文件即可）
rem
rem  产物：dist\wechat-triage-hud\wechat-triage-hud.exe（整个文件夹一起发给别人）
rem  说明：onedir 打包，第一次启动不用解包，双击就能用；数据（out\ 与 .env）
rem        写在 exe 旁边，不是临时目录（见 wechat_triage_hud\paths.py）。
rem ============================================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo [1/6] 检查 Python 与依赖
python -c "import sys; assert sys.version_info >= (3,10), sys.version" || (echo   X 需要 Python 3.10+ & pause & exit /b 1)
python -c "import PyInstaller" 2>nul || (echo   X 缺少 PyInstaller，先跑：pip install pyinstaller & pause & exit /b 1)
python -c "import PySide6, rapidocr_onnxruntime, mss, cv2, win32gui" 2>nul || (echo   X 缺少运行依赖，先跑：pip install -r requirements.txt & pause & exit /b 1)
echo   OK

echo.
echo [2/6] 生成图标（有 Pillow 就生成，没有就跳过）
python -c "from PIL import Image; Image.open(r'wechat_triage_hud\assets\ball.png').save(r'wechat_triage_hud\assets\ball.ico', sizes=[(16,16),(32,32),(48,48),(64,64)])" 2>nul && echo   OK || echo   （跳过：没装 Pillow 或图不存在，exe 用默认图标）

echo.
echo [3/6] 清理旧产物
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
echo   OK

echo.
echo [4/6] 打包（3-5 分钟，PySide6 + OCR 模型比较大）
python -m PyInstaller --noconfirm --clean packaging\wechat-triage-hud.spec || (echo   X 打包失败，见上面的报错 & pause & exit /b 1)
echo   OK

echo.
echo [5/6] 放一份配置模板与使用说明到产物目录
copy /y ".env.example" "dist\wechat-triage-hud\.env.example" >nul
copy /y "packaging\使用说明.txt" "dist\wechat-triage-hud\使用说明.txt" >nul
echo   OK

echo.
echo [6/6] 完成
for /f %%A in ('powershell -NoProfile -Command "(Get-ChildItem -Recurse dist\wechat-triage-hud | Measure-Object -Property Length -Sum).Sum/1MB"') do set SIZE=%%A
echo   产物：%CD%\dist\wechat-triage-hud\
echo   exe ：%CD%\dist\wechat-triage-hud\wechat-triage-hud.exe
echo   体积：约 %SIZE% MB（含 PySide6 与离线 OCR 模型，属正常）
echo.
echo   发给别人时：整个 dist\wechat-triage-hud 文件夹一起拷，外加让他自己填 .env
echo.
pause
endlocal
