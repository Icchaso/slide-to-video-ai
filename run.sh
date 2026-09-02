#!/usr/bin/env bash
# 動画自動制作パイプラインの実行入口（Mac / Linux）
#   ./run.sh                  inbox/ 内の全プロジェクトを処理
#   ./run.sh --project 名前   特定プロジェクトだけ処理
#   ./run.sh --check          環境チェックだけ行う
#   ./run.sh --strict         lint エラーがあればレンダリング前に停止
cd "$(dirname "$0")"

echo "========================================="
echo " 動画自動制作プロセスを開始します..."
echo "========================================="

if [ ! -f "venv/bin/activate" ]; then
    echo "エラー: 仮想環境が見つかりません。先に ./setup.sh を実行してください。"
    exit 2
fi
source venv/bin/activate

python3 src/main.py "$@"
code=$?

echo "========================================="
case $code in
  0) echo " 完了: output/<動画名>/ の final.mp4 / contact_sheet.jpg / build_summary.json を確認してください" ;;
  1) echo " 実行エラー: work/<動画名>/run.log を確認してください" ;;
  2) echo " 環境不足: 上に表示された導入コマンドを実行してから再実行してください" ;;
  3) echo " 品質ゲート FAIL: output/<動画名>/build_summary.json の checks を確認してください" ;;
  *) echo " 終了コード: $code" ;;
esac
echo "========================================="
exit $code
