import bcrypt
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from database import User
import os

# セッション用シークレットキー（本番では環境変数から取得）
SECRET_KEY = os.environ.get("SECRET_KEY", "your-secret-key-change-in-production")
SESSION_COOKIE_NAME = "session"
SESSION_MAX_AGE = 60 * 60 * 24 * 7  # 7日間
# 本番環境かどうか
IS_PRODUCTION = os.environ.get("ENVIRONMENT", "development") == "production"

serializer = URLSafeTimedSerializer(SECRET_KEY)


def hash_password(password: str) -> str:
    """パスワードをハッシュ化"""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')


def verify_password(password: str, hashed: str) -> bool:
    """パスワードを検証"""
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))


def create_session_token(user_id: int) -> str:
    """セッショントークンを作成"""
    return serializer.dumps({"user_id": user_id})


def verify_session_token(token: str) -> dict | None:
    """セッショントークンを検証"""
    try:
        data = serializer.loads(token, max_age=SESSION_MAX_AGE)
        return data
    except (BadSignature, SignatureExpired):
        return None


def get_current_user(request: Request, db: Session) -> User | None:
    """現在のログインユーザーを取得"""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None

    data = verify_session_token(token)
    if not data:
        return None

    user = db.query(User).filter(User.id == data["user_id"]).first()
    return user


def create_user(db: Session, email: str, password: str, company_name: str = None) -> User:
    """新規ユーザーを作成"""
    # メールアドレスの重複チェック
    existing = db.query(User).filter(User.email == email).first()
    if existing:
        raise ValueError("このメールアドレスは既に登録されています")

    user = User(
        email=email,
        password_hash=hash_password(password),
        company_name=company_name,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate_user(db: Session, email: str, password: str) -> User | None:
    """ユーザー認証"""
    user = db.query(User).filter(User.email == email).first()
    if not user:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def set_session_cookie(response, user_id: int):
    """セッションCookieを設定"""
    token = create_session_token(user_id)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=IS_PRODUCTION  # 本番環境ではHTTPS必須
    )
    return response


def clear_session_cookie(response):
    """セッションCookieを削除"""
    response.delete_cookie(key=SESSION_COOKIE_NAME)
    return response
