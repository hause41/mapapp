"""
データベースマイグレーションスクリプト
既存のテーブルに新しいカラムを追加
"""
import sqlite3
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "mapapp.db")

def migrate():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 既存のカラムを確認
    cursor.execute("PRAGMA table_info(users)")
    columns = [col[1] for col in cursor.fetchall()]

    # 新しいカラムを追加
    new_columns = [
        ("stripe_customer_id", "VARCHAR(255)"),
        ("stripe_subscription_id", "VARCHAR(255)"),
        ("subscription_status", "VARCHAR(50)"),
    ]

    for col_name, col_type in new_columns:
        if col_name not in columns:
            print(f"Adding column: {col_name}")
            cursor.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}")
        else:
            print(f"Column already exists: {col_name}")

    conn.commit()
    conn.close()
    print("Migration completed!")

if __name__ == "__main__":
    migrate()
