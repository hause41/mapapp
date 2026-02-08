"""
Stripe決済処理モジュール
"""
import os
import stripe
from sqlalchemy.orm import Session
from database import User

# Stripe APIキー（環境変数から取得）
# テストモード用のキーを使用（本番時は本番キーに変更）
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "sk_test_xxxxx")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "pk_test_xxxxx")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "whsec_xxxxx")

stripe.api_key = STRIPE_SECRET_KEY

# プラン別の価格ID（Stripeダッシュボードで作成した価格ID）
STRIPE_PRICE_IDS = {
    "lite": os.environ.get("STRIPE_PRICE_LITE", "price_lite_xxxxx"),
    "standard": os.environ.get("STRIPE_PRICE_STANDARD", "price_standard_xxxxx"),
}

# プラン情報
PLAN_INFO = {
    "demo": {
        "name": "DEMO",
        "price": 0,
        "limit": 5,
        "description": "お試し用の無料プラン",
        "features": ["月5回まで", "透かし入り", "機能制限あり"],
    },
    "lite": {
        "name": "ライト",
        "price": 500,
        "limit": 20,
        "description": "個人事業主向けプラン",
        "features": ["月20回まで", "透かしなし", "全機能利用可能"],
    },
    "standard": {
        "name": "スタンダード",
        "price": 980,
        "limit": 50,
        "description": "チーム・企業向けプラン",
        "features": ["月50回まで", "透かしなし", "全機能利用可能", "優先サポート"],
    },
}


def create_customer(email: str) -> str:
    """Stripe顧客を作成"""
    customer = stripe.Customer.create(email=email)
    return customer.id


def get_or_create_customer(db: Session, user: User) -> str:
    """Stripe顧客IDを取得または作成"""
    if user.stripe_customer_id:
        return user.stripe_customer_id

    customer_id = create_customer(user.email)
    user.stripe_customer_id = customer_id
    db.commit()
    return customer_id


def create_checkout_session(
    db: Session,
    user: User,
    plan: str,
    success_url: str,
    cancel_url: str
) -> str:
    """Stripeチェックアウトセッションを作成"""
    if plan not in STRIPE_PRICE_IDS:
        raise ValueError(f"無効なプラン: {plan}")

    customer_id = get_or_create_customer(db, user)
    price_id = STRIPE_PRICE_IDS[plan]

    checkout_session = stripe.checkout.Session.create(
        customer=customer_id,
        payment_method_types=["card"],
        line_items=[{
            "price": price_id,
            "quantity": 1,
        }],
        mode="subscription",
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={
            "user_id": str(user.id),
            "plan": plan,
        },
        # 日本語ロケール
        locale="ja",
    )

    return checkout_session.url


def create_portal_session(db: Session, user: User, return_url: str) -> str:
    """Stripeカスタマーポータルセッションを作成（サブスク管理用）"""
    customer_id = get_or_create_customer(db, user)

    portal_session = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=return_url,
    )

    return portal_session.url


def handle_checkout_completed(db: Session, session: dict):
    """チェックアウト完了時の処理"""
    customer_id = session.get("customer")
    subscription_id = session.get("subscription")
    metadata = session.get("metadata", {})

    user_id = metadata.get("user_id")
    plan = metadata.get("plan")

    if user_id:
        user = db.query(User).filter(User.id == int(user_id)).first()
        if user:
            user.plan = plan
            user.stripe_subscription_id = subscription_id
            user.subscription_status = "active"
            db.commit()


def handle_subscription_updated(db: Session, subscription: dict):
    """サブスクリプション更新時の処理"""
    subscription_id = subscription.get("id")
    status = subscription.get("status")

    user = db.query(User).filter(User.stripe_subscription_id == subscription_id).first()
    if user:
        user.subscription_status = status

        # キャンセルされた場合はDEMOに戻す
        if status in ("canceled", "unpaid"):
            user.plan = "demo"
            user.stripe_subscription_id = None

        db.commit()


def handle_subscription_deleted(db: Session, subscription: dict):
    """サブスクリプション削除時の処理"""
    subscription_id = subscription.get("id")

    user = db.query(User).filter(User.stripe_subscription_id == subscription_id).first()
    if user:
        user.plan = "demo"
        user.stripe_subscription_id = None
        user.subscription_status = "canceled"
        db.commit()


def verify_webhook_signature(payload: bytes, signature: str) -> dict:
    """Webhookの署名を検証"""
    try:
        event = stripe.Webhook.construct_event(
            payload, signature, STRIPE_WEBHOOK_SECRET
        )
        return event
    except ValueError:
        raise ValueError("Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise ValueError("Invalid signature")
