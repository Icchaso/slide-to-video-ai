@echo off
setlocal
chcp 65001 >nul
REM 動画自動制作パイプラインの実行入口（Windows）
REM   run.bat                  inbox\ 内の全プロジェクトを処理
REM   run.bat --project 名前   特定プロジェクトだけ処理
REM   run.bat --check          環境チェックだけ行う

echo =========================================
echo  動画自動制作プロセスを開始します...
echo =========================================

cd /d "%~dp0"

IF NOT EXIST "venv\Scripts\activate.bat" (
    echo エラー: 仮想環境が見つかりません。先に setup.bat を実行してください。
    exit /b 2
)

call venv\Scripts\activate.bat

python src\main.py %*
set CODE=%ERRORLEVEL%

echo =========================================
IF "%CODE%"=="0" echo  完了: output\動画名\ の final.mp4 / contact_sheet.jpg / build_summary.json を確認してください
IF "%CODE%"=="1" echo  実行エラー: work\動画名\run.log を確認してください
IF "%CODE%"=="2" echo  環境不足: 上に表示された導入コマンドを実行してから再実行してください
IF "%CODE%"=="3" echo  品質ゲート FAIL: output\動画名\build_summary.json の checks を確認してください
echo =========================================
endlocal & exit /b %CODE%
