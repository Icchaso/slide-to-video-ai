@echo off
setlocal
chcp 65001 >nul

echo =========================================
echo  動画自動制作プロセスを開始します...
echo =========================================

cd /d "%~dp0"

IF NOT EXIST "venv\Scripts\activate.bat" (
    echo エラー: 仮想環境が見つかりません。先に setup.bat を実行してください。
    exit /b 1
)

call venv\Scripts\activate.bat

python src\main.py %*

echo =========================================
echo  処理が完了しました。output\ フォルダを確認してください。
echo =========================================
endlocal
