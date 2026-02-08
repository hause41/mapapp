# 本番デプロイ手順書

## 1. 事前準備

### 必要なアカウント
- [GitHub](https://github.com) - コード管理
- [Render](https://render.com) - サーバーホスティング（無料枠あり）
- [Stripe](https://stripe.com) - 決済処理

### 環境変数の準備
以下の値を準備してください：

| 変数名 | 説明 | 取得方法 |
|--------|------|----------|
| SECRET_KEY | セッション暗号化キー | `python -c "import secrets; print(secrets.token_hex(32))"` |
| GOOGLE_MAPS_API_KEY | Google Maps API | [Google Cloud Console](https://console.cloud.google.com/) |
| STRIPE_SECRET_KEY | Stripe秘密鍵 | [Stripeダッシュボード](https://dashboard.stripe.com/apikeys) |
| STRIPE_PUBLISHABLE_KEY | Stripe公開鍵 | 同上 |
| STRIPE_WEBHOOK_SECRET | Webhook署名シークレット | Webhook設定後に取得 |
| STRIPE_PRICE_LITE | ライトプラン価格ID | Stripeで商品作成後に取得 |
| STRIPE_PRICE_STANDARD | スタンダードプラン価格ID | 同上 |

---

## 2. GitHubにコードをプッシュ

```bash
cd c:\yahoo_map\webapp

# Gitリポジトリを初期化
git init

# ファイルを追加
git add .

# 初回コミット
git commit -m "Initial commit: Map PDF Service"

# GitHubでリポジトリを作成後、以下を実行
git remote add origin https://github.com/YOUR_USERNAME/map-pdf-service.git
git branch -M main
git push -u origin main
```

---

## 3. Renderでデプロイ

### 3.1 新規Webサービス作成
1. [Render](https://render.com) にログイン
2. 「New +」→「Web Service」をクリック
3. GitHubリポジトリを連携・選択

### 3.2 設定
- **Name**: map-pdf-service
- **Environment**: Python
- **Build Command**: `pip install -r requirements.txt`
- **Start Command**: `gunicorn main:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT`

### 3.3 環境変数を設定
「Environment」タブで以下を追加：

```
ENVIRONMENT=production
SECRET_KEY=（生成した値）
GOOGLE_MAPS_API_KEY=（あなたのAPIキー）
STRIPE_SECRET_KEY=sk_live_...
STRIPE_PUBLISHABLE_KEY=pk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PRICE_LITE=price_...
STRIPE_PRICE_STANDARD=price_...
```

### 3.4 デプロイ
「Create Web Service」をクリックしてデプロイ開始

---

## 4. Stripe Webhook設定

1. [Stripeダッシュボード](https://dashboard.stripe.com/webhooks) を開く
2. 「エンドポイントを追加」をクリック
3. **エンドポイントURL**: `https://YOUR-APP.onrender.com/webhook/stripe`
4. **リッスンするイベント**:
   - `checkout.session.completed`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
5. 作成後、「署名シークレット」をコピーして環境変数に設定

---

## 5. Stripe商品・価格の作成

1. [Stripeダッシュボード](https://dashboard.stripe.com/products) を開く
2. 「商品を追加」をクリック

### ライトプラン
- **商品名**: 地図PDF作成サービス ライトプラン
- **価格**: ¥500 / 月額（定期）
- 作成後、価格IDをコピー → `STRIPE_PRICE_LITE`

### スタンダードプラン
- **商品名**: 地図PDF作成サービス スタンダードプラン
- **価格**: ¥980 / 月額（定期）
- 作成後、価格IDをコピー → `STRIPE_PRICE_STANDARD`

---

## 6. 動作確認

1. デプロイ完了後、`https://YOUR-APP.onrender.com` にアクセス
2. 新規登録・ログインをテスト
3. 地図PDF生成をテスト
4. 料金プランページで決済フローをテスト（テストモードで確認可能）

---

## 7. 本番切り替え

1. Stripeダッシュボードで「テストモード」をオフにする
2. 本番用APIキーを取得して環境変数を更新
3. Webhookも本番環境用に更新

---

## トラブルシューティング

### デプロイが失敗する
- Renderのログを確認
- requirements.txtの依存関係を確認

### 500エラーが出る
- 環境変数が正しく設定されているか確認
- データベースファイルの権限を確認

### Stripeの決済が動かない
- Webhook URLが正しいか確認
- 署名シークレットが正しいか確認
- イベントが正しく設定されているか確認

---

## 料金目安

### Render (無料枠)
- 無料プランで月750時間まで（1アプリなら常時稼働可能）
- スリープ機能あり（15分アイドルでスリープ）
- 有料プランは$7/月〜

### Google Maps API
- 月$200分の無料クレジット
- Static Maps: 1000回あたり$2
- Geocoding: 1000回あたり$5

### Stripe
- 決済手数料: 3.6%（国内カード）
- 月額固定費なし
