#!/usr/bin/env bash
set -e

echo "========================================="
echo " 動画自動制作工場 (slide-video) セットアップ"
echo "========================================="

# 1. Homebrewの確認と必要パッケージのインストール
if ! command -v brew &> /dev/null; then
    echo "Homebrewが見つかりません。インストールしてください。"
    exit 1
fi

echo ">> 必要なシステムパッケージをインストールします..."
brew install ffmpeg poppler

# 2. Node.js (HyperFrames用) の確認
if ! command -v npm &> /dev/null; then
    echo ">> Node.js (npm) をインストールします..."
    brew install node
fi

# 3. HyperFramesの初期化
echo ">> HyperFrames をセットアップします..."
if [ ! -d "hyperframes-app" ]; then
    npx hyperframes init hyperframes-app --example blank
    cd hyperframes-app
    npm install
    cd ..
else
    echo "HyperFramesは既にセットアップされています。"
fi

# 4. Python仮想環境の構築
echo ">> Python 仮想環境を構築します..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

source venv/bin/activate

echo ">> Python ライブラリをインストールします..."
pip install --upgrade pip
pip install -r requirements.txt

echo "========================================="
echo " セットアップ完了！"
echo " .env ファイルを作成し、APIキーを設定してください。"
echo "========================================="
