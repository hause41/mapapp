import os
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Enum as SQLEnum
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import enum

# データベースファイルのパス
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL = f"sqlite:///{os.path.join(BASE_DIR, 'mapapp.db')}"

# SQLAlchemy設定
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# プラン定義
class PlanType(str, enum.Enum):
    DEMO = "demo"
    LITE = "lite"
    STANDARD = "standard"


# ユーザーモデル
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    company_name = Column(String(255), nullable=True)  # 会社名
    plan = Column(String(20), default=PlanType.DEMO.value)
    # Stripe関連
    stripe_customer_id = Column(String(255), nullable=True)  # Stripe顧客ID
    stripe_subscription_id = Column(String(255), nullable=True)  # StripeサブスクリプションID
    subscription_status = Column(String(50), nullable=True)  # active, canceled, past_due等
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# 使用ログモデル（回数カウント用）
class UsageLog(Base):
    __tablename__ = "usage_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


# テーブル作成
def init_db():
    Base.metadata.create_all(bind=engine)


# データベースセッション取得
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
