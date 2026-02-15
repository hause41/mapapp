import os
import re
import base64
import textwrap
from io import BytesIO
from enum import Enum
from urllib.parse import quote, urlparse, parse_qs
from datetime import datetime

import requests
from fastapi import FastAPI, Form, HTTPException, Request, Depends
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError
import qrcode
from sqlalchemy.orm import Session

from database import init_db, get_db, User, UsageLog
from auth import (
    get_current_user, create_user, authenticate_user,
    set_session_cookie, clear_session_cookie
)
from stripe_handler import (
    create_checkout_session, create_portal_session,
    handle_checkout_completed, handle_subscription_updated,
    handle_subscription_deleted, verify_webhook_signature,
    PLAN_INFO, STRIPE_PRICE_IDS
)

# 定数
DPI = 300
MM_PER_INCH = 25.4
A4_WIDTH_MM = 297
A4_HEIGHT_MM = 210
DEFAULT_MARGIN_MM = 10.0

def mm_to_px(mm):
    return round(mm * DPI / MM_PER_INCH)

A4_WIDTH_PX = mm_to_px(A4_WIDTH_MM)
A4_HEIGHT_PX = mm_to_px(A4_HEIGHT_MM)

# APIキー（環境変数から取得、なければデフォルト値）
API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")
if not API_KEY:
    raise RuntimeError("GOOGLE_MAPS_API_KEY environment variable is required")

# フォントパス
def get_font_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "msgothic.ttc")

# プラン定義
class Plan(str, Enum):
    DEMO = "demo"
    LITE = "lite"
    STANDARD = "standard"

PLAN_CONFIG = {
    Plan.DEMO: {"map_count": 2, "watermark": True, "monthly_limit": 5},
    Plan.LITE: {"map_count": 2, "watermark": False, "monthly_limit": 20},
    Plan.STANDARD: {"map_count": 2, "watermark": False, "monthly_limit": 50},
}

# プラン表示名
PLAN_NAMES = {
    "demo": "DEMO",
    "lite": "ライト",
    "standard": "スタンダード",
}

# プラン別月間上限
PLAN_LIMITS = {
    "demo": 5,
    "lite": 20,
    "standard": 50,
}

# ベースディレクトリ
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def get_monthly_usage_count(db: Session, user_id: int) -> int:
    """今月の使用回数を取得"""
    now = datetime.utcnow()
    first_day_of_month = datetime(now.year, now.month, 1)
    count = db.query(UsageLog).filter(
        UsageLog.user_id == user_id,
        UsageLog.created_at >= first_day_of_month
    ).count()
    return count


def get_remaining_count(db: Session, user: User) -> int:
    """残り回数を取得"""
    limit = PLAN_LIMITS.get(user.plan, 5)
    used = get_monthly_usage_count(db, user.id)
    return max(0, limit - used)


def can_generate_pdf(db: Session, user: User) -> bool:
    """PDF生成可能かどうかをチェック"""
    return get_remaining_count(db, user) > 0


# FastAPIアプリ
app = FastAPI(title="地図PDF作成サービス")

# 静的ファイルとテンプレート
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# 起動時にデータベース初期化
@app.on_event("startup")
def startup_event():
    init_db()

# ファイル名サニタイズ
INVALID_FILENAME_CHARS = r'[<>:"/\\|?*\x00-\x1f]'

def sanitize_filename_component(value, fallback, max_length=60):
    if value is None:
        value = ""
    value = value.strip()
    value = re.sub(INVALID_FILENAME_CHARS, "_", value)
    value = value.rstrip(". ")
    if not value:
        value = fallback
    if len(value) > max_length:
        value = value[:max_length].rstrip(". ")
    return value

def build_output_filename(property_name, address):
    name_part = sanitize_filename_component(property_name, "property")
    address_part = sanitize_filename_component(address, "address")
    base = f"{name_part}_{address_part}"
    if len(base) > 120:
        base = base[:120].rstrip(". ")
    return f"{base}.pdf"


def extract_coords_from_url(url_text: str):
    """展開済みGoogle Maps URLから緯度・経度を抽出"""
    # @35.6812,139.7671 パターン
    at_match = re.search(r'@(-?\d+\.?\d*),(-?\d+\.?\d*)', url_text)
    if at_match:
        return float(at_match.group(1)), float(at_match.group(2))

    # /maps/search/35.168752,+136.921041 パターン（短縮URL展開後）
    search_match = re.search(r'/maps/search/(-?\d+\.?\d*),\+?(-?\d+\.?\d*)', url_text)
    if search_match:
        return float(search_match.group(1)), float(search_match.group(2))

    # /maps/dir/35.6812,139.7671 パターン
    dir_match = re.search(r'/maps/dir/(-?\d+\.?\d*),\+?(-?\d+\.?\d*)', url_text)
    if dir_match:
        return float(dir_match.group(1)), float(dir_match.group(2))

    # ?q=35.6812,139.7671 パターン
    parsed = urlparse(url_text)
    params = parse_qs(parsed.query)
    q_val = params.get("q", [None])[0]
    if q_val:
        coord_match = re.match(r'^(-?\d+\.?\d*),\s*(-?\d+\.?\d*)$', q_val)
        if coord_match:
            return float(coord_match.group(1)), float(coord_match.group(2))

    # ll=35.6812,139.7671 パターン
    ll_val = params.get("ll", [None])[0]
    if ll_val:
        coord_match = re.match(r'^(-?\d+\.?\d*),\s*(-?\d+\.?\d*)$', ll_val)
        if coord_match:
            return float(coord_match.group(1)), float(coord_match.group(2))

    # /place/35.6812,139.7671 パターン
    place_match = re.search(r'/place/(-?\d+\.?\d*),(-?\d+\.?\d*)', url_text)
    if place_match:
        return float(place_match.group(1)), float(place_match.group(2))

    # URLパス内の座標パターン（/data=...!3d35.6812!4d139.7671）
    data_lat = re.search(r'!3d(-?\d+\.?\d*)', url_text)
    data_lng = re.search(r'!4d(-?\d+\.?\d*)', url_text)
    if data_lat and data_lng:
        return float(data_lat.group(1)), float(data_lng.group(1))

    # URLパス全体から座標っぽい数値ペアを探す（最終手段）
    fallback_match = re.search(r'(-?\d{1,3}\.\d{3,}),\s?\+?(-?\d{1,3}\.\d{3,})', url_text)
    if fallback_match:
        lat = float(fallback_match.group(1))
        lng = float(fallback_match.group(2))
        if -90 <= lat <= 90 and -180 <= lng <= 180:
            return lat, lng

    return None


def expand_short_url(url_text: str) -> str:
    """短縮URLを展開"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    resp = requests.get(url_text, headers=headers, allow_redirects=True, timeout=15)
    print(f"[DEBUG] 短縮URL展開結果: {resp.url}")
    return resp.url


def parse_google_maps_url(url_text: str):
    """Google Maps URLから緯度・経度を抽出（短縮URL対応）"""
    url_text = url_text.strip()
    if not url_text.startswith("http"):
        return None

    # 短縮URLの場合は先に展開
    if "goo.gl" in url_text or "maps.app" in url_text:
        try:
            url_text = expand_short_url(url_text)
        except Exception as e:
            print(f"[DEBUG] 短縮URL展開失敗: {e}")
            return None

    # 展開後のURLからパース
    result = extract_coords_from_url(url_text)
    if result:
        print(f"[DEBUG] 座標抽出成功: {result}")
        return result

    # URLに座標が無い場合、qパラメータの住所をGeocoding APIで解決
    parsed = urlparse(url_text)
    params = parse_qs(parsed.query)
    q_val = params.get("q", [None])[0]
    if q_val and not re.match(r'^-?\d+\.?\d*,', q_val):
        print(f"[DEBUG] qパラメータの住所をGeocoding: {q_val}")
        try:
            geocode_resp = requests.get(
                "https://maps.googleapis.com/maps/api/geocode/json",
                params={"address": q_val, "key": API_KEY, "language": "ja"},
                timeout=10,
            )
            geocode_data = geocode_resp.json()
            if geocode_data.get("status") == "OK":
                loc = geocode_data["results"][0]["geometry"]["location"]
                print(f"[DEBUG] Geocoding成功: {loc['lat']}, {loc['lng']}")
                return loc["lat"], loc["lng"]
        except Exception as e:
            print(f"[DEBUG] Geocoding失敗: {e}")

    print(f"[DEBUG] 座標抽出失敗 URL: {url_text}")
    return None


def resolve_coordinates(coordinates_text, address_text):
    """座標または住所から緯度・経度を取得"""
    coordinates = (coordinates_text or "").strip()
    if coordinates:
        # Google Maps URLの場合
        if coordinates.startswith("http"):
            parsed = parse_google_maps_url(coordinates)
            if parsed:
                return parsed
            raise HTTPException(status_code=400, detail="Google Maps URLから座標を取得できませんでした。")

        try:
            latitude, longitude = map(float, coordinates.split(","))
            return latitude, longitude
        except ValueError:
            raise HTTPException(status_code=400, detail="無効な座標が入力されました。")

    address = (address_text or "").strip()
    if not address:
        raise HTTPException(status_code=400, detail="住所または座標を入力してください。")

    geocode_endpoint = "https://maps.googleapis.com/maps/api/geocode/json"
    geocode_params = {
        "address": address,
        "key": API_KEY,
        "language": "ja",
    }

    try:
        geocode_response = requests.get(geocode_endpoint, params=geocode_params, timeout=10)
        geocode_response.raise_for_status()
        geocode_data = geocode_response.json()
    except requests.RequestException as e:
        raise HTTPException(status_code=500, detail=f"住所からの位置情報取得に失敗しました: {e}")

    status = geocode_data.get("status")
    if status != "OK":
        error_message = geocode_data.get("error_message", "")
        if error_message:
            raise HTTPException(status_code=400, detail=f"住所からの位置情報取得に失敗しました: {error_message}")
        raise HTTPException(status_code=400, detail=f"住所から位置情報が取得できませんでした。({status})")

    results = geocode_data.get("results") or []
    if not results:
        raise HTTPException(status_code=400, detail="住所から位置情報が取得できませんでした。")

    location = results[0].get("geometry", {}).get("location") or {}
    latitude = location.get("lat")
    longitude = location.get("lng")
    if latitude is None or longitude is None:
        raise HTTPException(status_code=400, detail="住所から位置情報が取得できませんでした。")

    return latitude, longitude


def fetch_map_image(latitude, longitude, zoom):
    """Google Static Maps APIから地図画像を取得"""
    map_params = {
        "center": f"{latitude},{longitude}",
        "zoom": zoom,
        "size": "400x800",
        "scale": 2,
        "format": "png32",
        "maptype": "roadmap",
        "markers": f"color:red|{latitude},{longitude}",
        "key": API_KEY,
        "language": "ja",
    }
    static_map_endpoint = "https://maps.googleapis.com/maps/api/staticmap"

    try:
        map_response = requests.get(static_map_endpoint, params=map_params, timeout=30)
        map_response.raise_for_status()
        map_image = Image.open(BytesIO(map_response.content))
    except requests.RequestException as e:
        raise HTTPException(status_code=500, detail=f"地図画像の取得に失敗しました (zoom {zoom}): {e}")
    except (UnidentifiedImageError, OSError) as e:
        raise HTTPException(status_code=500, detail=f"地図画像の読み込みに失敗しました (zoom {zoom}): {e}")

    # RGBA → RGB変換
    if map_image.mode in ("RGBA", "LA"):
        bg = Image.new("RGBA", map_image.size, (255, 255, 255, 255))
        bg.paste(map_image, (0, 0), map_image)
        map_image = bg.convert("RGB")
    else:
        map_image = map_image.convert("RGB")

    return map_image


def add_watermark(image, text="DEMO"):
    """画像に透かしを追加"""
    draw = ImageDraw.Draw(image)

    try:
        font = ImageFont.truetype(get_font_path(), 200)
    except:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]

    x = (image.width - text_width) // 2
    y = (image.height - text_height) // 2

    draw.text((x, y), text, font=font, fill=(255, 0, 0, 128))

    return image


def generate_pdf(data: dict, plan: Plan) -> BytesIO:
    """PDF生成メイン処理"""
    config = PLAN_CONFIG[plan]

    latitude, longitude = resolve_coordinates(data["coordinates"], data["address"])

    remarks = textwrap.fill(data["remarks"] or "", width=15, subsequent_indent="     ")
    vehicle_type_text = data["vehicle_type"]

    # 地図取得
    if config["map_count"] == 1:
        map_images = [fetch_map_image(latitude, longitude, 17)]
    else:
        map_images = [
            fetch_map_image(latitude, longitude, 14),
            fetch_map_image(latitude, longitude, 17),
        ]

    margin_px = mm_to_px(DEFAULT_MARGIN_MM)

    if len(map_images) == 1:
        combined_image = map_images[0]
    else:
        separator_w = 6
        combined_width = map_images[0].width + separator_w + map_images[1].width
        combined_height = max(img.height for img in map_images)

        combined_image = Image.new("RGB", (combined_width, combined_height), "white")
        combined_image.paste(map_images[0], (0, 0))
        combined_image.paste(map_images[1], (map_images[0].width + separator_w, 0))

        draw_sep = ImageDraw.Draw(combined_image)
        x0 = map_images[0].width
        draw_sep.rectangle((x0, 0, x0 + separator_w - 1, combined_height), fill="black")

    final_width, final_height = A4_WIDTH_PX, A4_HEIGHT_PX
    final_canvas = Image.new("RGB", (final_width, final_height), "white")

    usable_width = final_width - margin_px * 2
    usable_height = final_height - margin_px * 2

    scale_factor = min(
        usable_width / combined_image.width,
        usable_height / combined_image.height,
    )
    new_width = max(1, int(round(combined_image.width * scale_factor)))
    new_height = max(1, int(round(combined_image.height * scale_factor)))
    map_resized = combined_image.resize((new_width, new_height), Image.LANCZOS)

    paste_x = margin_px + (usable_width - new_width) // 2
    paste_y = margin_px + (usable_height - new_height) // 2
    final_canvas.paste(map_resized, (paste_x, paste_y))

    draw = ImageDraw.Draw(final_canvas)
    try:
        font = ImageFont.truetype(get_font_path(), 42)
    except:
        font = ImageFont.load_default()

    # 住所が無い場合は座標を表示
    if data['address']:
        location_line = f"〒{data['address']}"
    else:
        location_line = f"座標: {latitude},{longitude}"

    text_lines = [location_line]
    if data['customer']:
        text_lines.append(f"得意先: {data['customer']}")
    if data['property_name']:
        text_lines.append(f"物件名: {data['property_name']}")
    text_lines.append(f"車種: {vehicle_type_text}")
    if remarks:
        text_lines.append(f"備考: {remarks}")

    text = "\n".join(text_lines)

    padding = 16
    text_bbox = draw.multiline_textbbox((0, 0), text, font=font, spacing=6)
    text_width = text_bbox[2] - text_bbox[0]
    text_height = text_bbox[3] - text_bbox[1]

    text_offset_px = 10
    text_box_x = paste_x + text_offset_px
    text_box_y = paste_y + text_offset_px

    rect_coords = (
        text_box_x,
        text_box_y,
        text_box_x + text_width + padding,
        text_box_y + text_height + padding,
    )
    draw.rectangle(rect_coords, fill="white", outline="black", width=2)
    draw.multiline_text(
        (text_box_x + padding // 2, text_box_y + padding // 2),
        text,
        font=font,
        fill="black",
        spacing=6,
    )

    # QRコードを右下に配置
    maps_url = f"https://www.google.com/maps?q={latitude},{longitude}"
    qr = qrcode.QRCode(version=1, box_size=6, border=2)
    qr.add_data(maps_url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    qr_size = mm_to_px(25)  # 25mm四方
    qr_img = qr_img.resize((qr_size, qr_size), Image.LANCZOS)

    qr_x = final_width - margin_px - qr_size
    qr_y = final_height - margin_px - qr_size

    # QRコード背景（白）
    draw.rectangle(
        (qr_x - 4, qr_y - 4, qr_x + qr_size + 4, qr_y + qr_size + 4),
        fill="white", outline="black", width=1
    )
    final_canvas.paste(qr_img, (qr_x, qr_y))

    if config["watermark"]:
        final_canvas = add_watermark(final_canvas)

    pdf_buffer = BytesIO()
    pdf_info = {"Title": f"{data['property_name']} {data['address']}"}
    final_canvas.save(pdf_buffer, "PDF", resolution=300.0, pdfinfo=pdf_info)
    pdf_buffer.seek(0)

    return pdf_buffer


# ========== 認証関連ルート ==========

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """ログインページ"""
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    """ログイン処理"""
    user = authenticate_user(db, email, password)
    if not user:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "メールアドレスまたはパスワードが正しくありません"
        })

    response = RedirectResponse(url="/", status_code=303)
    set_session_cookie(response, user.id)
    return response


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    """登録ページ"""
    return templates.TemplateResponse("register.html", {"request": request})


@app.post("/register", response_class=HTMLResponse)
async def register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    company_name: str = Form(""),
    db: Session = Depends(get_db)
):
    """登録処理"""
    # バリデーション
    if len(password) < 6:
        return templates.TemplateResponse("register.html", {
            "request": request,
            "error": "パスワードは6文字以上で入力してください"
        })

    if password != password_confirm:
        return templates.TemplateResponse("register.html", {
            "request": request,
            "error": "パスワードが一致しません"
        })

    try:
        user = create_user(db, email, password, company_name)
    except ValueError as e:
        return templates.TemplateResponse("register.html", {
            "request": request,
            "error": str(e)
        })

    response = RedirectResponse(url="/", status_code=303)
    set_session_cookie(response, user.id)
    return response


@app.get("/logout")
async def logout():
    """ログアウト"""
    response = RedirectResponse(url="/login", status_code=303)
    clear_session_cookie(response)
    return response


# ========== ランディング・法的ページ ==========

@app.get("/lp", response_class=HTMLResponse)
async def landing_page(request: Request):
    """ランディングページ"""
    return templates.TemplateResponse("landing.html", {"request": request})


@app.get("/terms", response_class=HTMLResponse)
async def terms_page(request: Request):
    """利用規約"""
    return templates.TemplateResponse("terms.html", {"request": request})


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request):
    """プライバシーポリシー"""
    return templates.TemplateResponse("privacy.html", {"request": request})


@app.get("/legal", response_class=HTMLResponse)
async def legal_page(request: Request):
    """特定商取引法に基づく表記"""
    return templates.TemplateResponse("legal.html", {"request": request})


# ========== メインルート ==========

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, db: Session = Depends(get_db)):
    """メインページ"""
    user = get_current_user(request, db)

    # 残り回数と上限を計算
    remaining = 0
    limit = 0
    if user:
        remaining = get_remaining_count(db, user)
        limit = PLAN_LIMITS.get(user.plan, 5)

    context = {
        "request": request,
        "user": user,
        "plan_name": PLAN_NAMES.get(user.plan, "DEMO") if user else "DEMO",
        "remaining": remaining,
        "limit": limit,
    }
    return templates.TemplateResponse("index.html", context)


@app.get("/get-coordinates")
async def get_coordinates_api(address: str = "", coordinates: str = ""):
    """座標取得API（地図表示用）"""
    try:
        lat, lng = resolve_coordinates(coordinates, address)
        return {"lat": lat, "lng": lng}
    except HTTPException as e:
        return {"error": e.detail}
    except Exception as e:
        return {"error": str(e)}


@app.get("/parse-maps-url")
async def parse_maps_url_api(url: str = ""):
    """Google Maps URLから座標を抽出するAPI"""
    if not url:
        return {"error": "URLを入力してください"}

    result = parse_google_maps_url(url)
    if result:
        return {"lat": result[0], "lng": result[1]}

    return {"error": "URLから座標を抽出できませんでした"}


@app.get("/generate-qrcode")
async def generate_qrcode_api(lat: float = 0, lng: float = 0):
    """座標からGoogle Maps QRコードを生成"""
    if lat == 0 and lng == 0:
        return {"error": "座標を指定してください"}

    maps_url = f"https://www.google.com/maps?q={lat},{lng}"

    qr = qrcode.QRCode(version=1, box_size=8, border=2)
    qr.add_data(maps_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

    return {"qrcode": f"data:image/png;base64,{b64}", "maps_url": maps_url}


@app.post("/generate-pdf")
async def generate_pdf_endpoint(
    request: Request,
    address: str = Form(""),
    coordinates: str = Form(""),
    customer: str = Form(""),
    property_name: str = Form(""),
    vehicle_type: str = Form("車種指定なし"),
    remarks: str = Form(""),
    db: Session = Depends(get_db)
):
    """PDF生成API"""
    user = get_current_user(request, db)

    # ログイン必須
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    # 回数制限チェック
    if not can_generate_pdf(db, user):
        # 回数上限に達した場合、エラーページを表示
        remaining = get_remaining_count(db, user)
        limit = PLAN_LIMITS.get(user.plan, 5)
        return templates.TemplateResponse("limit_reached.html", {
            "request": request,
            "user": user,
            "plan_name": PLAN_NAMES.get(user.plan, "DEMO"),
            "limit": limit,
        })

    # ユーザーのプランを使用
    plan = user.plan

    # プラン検証
    try:
        plan_enum = Plan(plan.lower())
    except ValueError:
        plan_enum = Plan.DEMO

    data = {
        "address": address.strip(),
        "coordinates": coordinates.strip(),
        "customer": customer.strip(),
        "property_name": property_name.strip(),
        "vehicle_type": vehicle_type,
        "remarks": remarks.strip()[:200],
    }

    # PDF生成
    pdf_buffer = generate_pdf(data, plan_enum)

    # 使用ログ記録（ログインユーザーのみ）
    if user:
        usage_log = UsageLog(user_id=user.id)
        db.add(usage_log)
        db.commit()

    # ファイル名生成
    address_for_filename = data["address"] or data["coordinates"]
    filename = build_output_filename(data["property_name"], address_for_filename)
    filename_encoded = quote(filename, safe='')

    return StreamingResponse(
        pdf_buffer,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename_encoded}"
        }
    )


# ========== Stripe関連ルート ==========

@app.get("/pricing", response_class=HTMLResponse)
async def pricing_page(request: Request, db: Session = Depends(get_db)):
    """料金プランページ"""
    user = get_current_user(request, db)

    context = {
        "request": request,
        "user": user,
        "current_plan_name": PLAN_NAMES.get(user.plan, "DEMO") if user else "DEMO",
        "plans": PLAN_INFO,
    }
    return templates.TemplateResponse("pricing.html", context)


@app.get("/checkout")
async def checkout(
    request: Request,
    plan: str,
    db: Session = Depends(get_db)
):
    """Stripeチェックアウトへリダイレクト"""
    user = get_current_user(request, db)

    if not user:
        return RedirectResponse(url="/login", status_code=303)

    if plan not in STRIPE_PRICE_IDS:
        return RedirectResponse(url="/pricing", status_code=303)

    # 現在のプランと同じ、または下位プランへの変更は不可
    plan_order = {"demo": 0, "lite": 1, "standard": 2}
    if plan_order.get(plan, 0) <= plan_order.get(user.plan, 0):
        return RedirectResponse(url="/pricing", status_code=303)

    # ベースURL取得
    base_url = str(request.base_url).rstrip("/")
    success_url = f"{base_url}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{base_url}/checkout/cancel"

    try:
        checkout_url = create_checkout_session(
            db, user, plan, success_url, cancel_url
        )
        return RedirectResponse(url=checkout_url, status_code=303)
    except Exception as e:
        # エラー時は料金ページに戻る
        return RedirectResponse(url="/pricing", status_code=303)


@app.get("/checkout/success", response_class=HTMLResponse)
async def checkout_success(request: Request, db: Session = Depends(get_db)):
    """決済成功ページ"""
    user = get_current_user(request, db)

    if not user:
        return RedirectResponse(url="/login", status_code=303)

    # 最新のユーザー情報を取得（Webhookで更新されている可能性）
    db.refresh(user)

    limit = PLAN_LIMITS.get(user.plan, 5)

    context = {
        "request": request,
        "user": user,
        "plan_name": PLAN_NAMES.get(user.plan, "DEMO"),
        "limit": limit,
    }
    return templates.TemplateResponse("checkout_success.html", context)


@app.get("/checkout/cancel", response_class=HTMLResponse)
async def checkout_cancel(request: Request, db: Session = Depends(get_db)):
    """決済キャンセルページ"""
    user = get_current_user(request, db)

    context = {
        "request": request,
        "user": user,
    }
    return templates.TemplateResponse("checkout_cancel.html", context)


@app.get("/billing-portal")
async def billing_portal(request: Request, db: Session = Depends(get_db)):
    """Stripeカスタマーポータルへリダイレクト"""
    user = get_current_user(request, db)

    if not user:
        return RedirectResponse(url="/login", status_code=303)

    if not user.stripe_customer_id:
        return RedirectResponse(url="/pricing", status_code=303)

    base_url = str(request.base_url).rstrip("/")
    return_url = f"{base_url}/pricing"

    try:
        portal_url = create_portal_session(db, user, return_url)
        return RedirectResponse(url=portal_url, status_code=303)
    except Exception as e:
        return RedirectResponse(url="/pricing", status_code=303)


@app.post("/webhook/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    """Stripe Webhook処理"""
    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")

    try:
        event = verify_webhook_signature(payload, signature)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})

    event_type = event.get("type")
    data = event.get("data", {}).get("object", {})

    # イベント種別に応じた処理
    if event_type == "checkout.session.completed":
        handle_checkout_completed(db, data)
    elif event_type == "customer.subscription.updated":
        handle_subscription_updated(db, data)
    elif event_type == "customer.subscription.deleted":
        handle_subscription_deleted(db, data)

    return JSONResponse(content={"status": "success"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
