@echo off
setlocal
chcp 65001 >nul

echo =========================================
echo  動画自動制作工場 (slide-video) セットアップ
echo =========================================

cd /d "%~dp0"

echo.
echo ^>^> HyperFrames をセットアップします...
IF NOT EXIST "hyperframes-app" (
    call npx hyperframes init hyperframes-app --example blank
    cd hyperframes-app
    call npm install
    cd ..
) ELSE (
    echo HyperFramesは既にセットアップされています。
)

echo.
echo ^>^> Python 仮想環境を構築します...
IF NOT EXIST "venv" (
    python -m venv venv
)

call venv\Scripts\activate.bat

echo.
echo ^>^> Python ライブラリをインストールします...
python -m pip install --upgrade pip
pip install -r requirements.txt

echo.
echo =========================================
echo  セットアップ完了！
echo  .env ファイルを作成し、APIキーを設定してください。
echo  (※ 事前に Node.js, Python, FFmpeg, Poppler がインストールされている必要があります)
echo =========================================
endlocal
