import os
import re
import base64
import textwrap
from io import BytesIO
from urllib.parse import quote, urlparse, parse_qs

import requests
from fastapi import FastAPI, Form, HTTPException, Request, Depends
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
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

# 螳壽焚
DPI = 300
MM_PER_INCH = 25.4
A4_WIDTH_MM = 297
A4_HEIGHT_MM = 210
DEFAULT_MARGIN_MM = 10.0

def mm_to_px(mm):
    return round(mm * DPI / MM_PER_INCH)

A4_WIDTH_PX = mm_to_px(A4_WIDTH_MM)
A4_HEIGHT_PX = mm_to_px(A4_HEIGHT_MM)

# API繧ｭ繝ｼ・育腸蠅・､画焚縺九ｉ蜿門ｾ励√↑縺代ｌ縺ｰ繝・ヵ繧ｩ繝ｫ繝亥､・・
API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")
if not API_KEY:
    raise RuntimeError("GOOGLE_MAPS_API_KEY environment variable is required")

# 繝輔か繝ｳ繝医ヱ繧ｹ
def get_font_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "msgothic.ttc")

# 繝吶・繧ｹ繝・ぅ繝ｬ繧ｯ繝医Μ
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# FastAPI繧｢繝励Μ
app = FastAPI(title="蝨ｰ蝗ｳPDF菴懈・繧ｵ繝ｼ繝薙せ")

# 髱咏噪繝輔ぃ繧､繝ｫ縺ｨ繝・Φ繝励Ξ繝ｼ繝・app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# 襍ｷ蜍墓凾縺ｫ繝・・繧ｿ繝吶・繧ｹ蛻晄悄蛹・@app.on_event("startup")
def startup_event():
    init_db()

# 繝輔ぃ繧､繝ｫ蜷阪し繝九ち繧､繧ｺ
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
    """螻暮幕貂医∩Google Maps URL縺九ｉ邱ｯ蠎ｦ繝ｻ邨悟ｺｦ繧呈歓蜃ｺ"""
    # @35.6812,139.7671 繝代ち繝ｼ繝ｳ
    at_match = re.search(r'@(-?\d+\.?\d*),(-?\d+\.?\d*)', url_text)
    if at_match:
        return float(at_match.group(1)), float(at_match.group(2))

    # /maps/search/35.168752,+136.921041 繝代ち繝ｼ繝ｳ・育洒邵ｮURL螻暮幕蠕鯉ｼ・    search_match = re.search(r'/maps/search/(-?\d+\.?\d*),\+?(-?\d+\.?\d*)', url_text)
    if search_match:
        return float(search_match.group(1)), float(search_match.group(2))

    # /maps/dir/35.6812,139.7671 繝代ち繝ｼ繝ｳ
    dir_match = re.search(r'/maps/dir/(-?\d+\.?\d*),\+?(-?\d+\.?\d*)', url_text)
    if dir_match:
        return float(dir_match.group(1)), float(dir_match.group(2))

    # ?q=35.6812,139.7671 繝代ち繝ｼ繝ｳ
    parsed = urlparse(url_text)
    params = parse_qs(parsed.query)
    q_val = params.get("q", [None])[0]
    if q_val:
        coord_match = re.match(r'^(-?\d+\.?\d*),\s*(-?\d+\.?\d*)$', q_val)
        if coord_match:
            return float(coord_match.group(1)), float(coord_match.group(2))

    # ll=35.6812,139.7671 繝代ち繝ｼ繝ｳ
    ll_val = params.get("ll", [None])[0]
    if ll_val:
        coord_match = re.match(r'^(-?\d+\.?\d*),\s*(-?\d+\.?\d*)$', ll_val)
        if coord_match:
            return float(coord_match.group(1)), float(coord_match.group(2))

    # /place/35.6812,139.7671 繝代ち繝ｼ繝ｳ
    place_match = re.search(r'/place/(-?\d+\.?\d*),(-?\d+\.?\d*)', url_text)
    if place_match:
        return float(place_match.group(1)), float(place_match.group(2))

    # URL繝代せ蜀・・蠎ｧ讓吶ヱ繧ｿ繝ｼ繝ｳ・・data=...!3d35.6812!4d139.7671・・    data_lat = re.search(r'!3d(-?\d+\.?\d*)', url_text)
    data_lng = re.search(r'!4d(-?\d+\.?\d*)', url_text)
    if data_lat and data_lng:
        return float(data_lat.group(1)), float(data_lng.group(1))

    # URL繝代せ蜈ｨ菴薙°繧牙ｺｧ讓吶▲縺ｽ縺・焚蛟､繝壹い繧呈爾縺呻ｼ域怙邨よ焔谿ｵ・・    fallback_match = re.search(r'(-?\d{1,3}\.\d{3,}),\s?\+?(-?\d{1,3}\.\d{3,})', url_text)
    if fallback_match:
        lat = float(fallback_match.group(1))
        lng = float(fallback_match.group(2))
        if -90 <= lat <= 90 and -180 <= lng <= 180:
            return lat, lng

    return None


def expand_short_url(url_text: str) -> str:
    """遏ｭ邵ｮURL繧貞ｱ暮幕"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    resp = requests.get(url_text, headers=headers, allow_redirects=True, timeout=15)
    print(f"[DEBUG] 遏ｭ邵ｮURL螻暮幕邨先棡: {resp.url}")
    return resp.url


def parse_google_maps_url(url_text: str):
    """Google Maps URL縺九ｉ邱ｯ蠎ｦ繝ｻ邨悟ｺｦ繧呈歓蜃ｺ・育洒邵ｮURL蟇ｾ蠢懶ｼ・""
    url_text = url_text.strip()
    if not url_text.startswith("http"):
        return None

    # 遏ｭ邵ｮURL縺ｮ蝣ｴ蜷医・蜈医↓螻暮幕
    if "goo.gl" in url_text or "maps.app" in url_text:
        try:
            url_text = expand_short_url(url_text)
        except Exception as e:
            print(f"[DEBUG] 遏ｭ邵ｮURL螻暮幕螟ｱ謨・ {e}")
            return None

    # 螻暮幕蠕後・URL縺九ｉ繝代・繧ｹ
    result = extract_coords_from_url(url_text)
    if result:
        print(f"[DEBUG] 蠎ｧ讓呎歓蜃ｺ謌仙粥: {result}")
        return result

    # URL縺ｫ蠎ｧ讓吶′辟｡縺・ｴ蜷医〈繝代Λ繝｡繝ｼ繧ｿ縺ｮ菴乗園繧竪eocoding API縺ｧ隗｣豎ｺ
    parsed = urlparse(url_text)
    params = parse_qs(parsed.query)
    q_val = params.get("q", [None])[0]
    if q_val and not re.match(r'^-?\d+\.?\d*,', q_val):
        print(f"[DEBUG] q繝代Λ繝｡繝ｼ繧ｿ縺ｮ菴乗園繧竪eocoding: {q_val}")
        try:
            geocode_resp = requests.get(
                "https://maps.googleapis.com/maps/api/geocode/json",
                params={"address": q_val, "key": API_KEY, "language": "ja"},
                timeout=10,
            )
            geocode_data = geocode_resp.json()
            if geocode_data.get("status") == "OK":
                loc = geocode_data["results"][0]["geometry"]["location"]
                print(f"[DEBUG] Geocoding謌仙粥: {loc['lat']}, {loc['lng']}")
                return loc["lat"], loc["lng"]
        except Exception as e:
            print(f"[DEBUG] Geocoding螟ｱ謨・ {e}")

    print(f"[DEBUG] 蠎ｧ讓呎歓蜃ｺ螟ｱ謨・URL: {url_text}")
    return None


def resolve_coordinates(coordinates_text, address_text):
    """蠎ｧ讓吶∪縺溘・菴乗園縺九ｉ邱ｯ蠎ｦ繝ｻ邨悟ｺｦ繧貞叙蠕・""
    coordinates = (coordinates_text or "").strip()
    if coordinates:
        # Google Maps URL縺ｮ蝣ｴ蜷・        if coordinates.startswith("http"):
            parsed = parse_google_maps_url(coordinates)
            if parsed:
                return parsed
            raise HTTPException(status_code=400, detail="Google Maps URL縺九ｉ蠎ｧ讓吶ｒ蜿門ｾ励〒縺阪∪縺帙ｓ縺ｧ縺励◆縲・)

        try:
            latitude, longitude = map(float, coordinates.split(","))
            return latitude, longitude
        except ValueError:
            raise HTTPException(status_code=400, detail="辟｡蜉ｹ縺ｪ蠎ｧ讓吶′蜈･蜉帙＆繧後∪縺励◆縲・)

    address = (address_text or "").strip()
    if not address:
        raise HTTPException(status_code=400, detail="菴乗園縺ｾ縺溘・蠎ｧ讓吶ｒ蜈･蜉帙＠縺ｦ縺上□縺輔＞縲・)

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
        raise HTTPException(status_code=500, detail=f"菴乗園縺九ｉ縺ｮ菴咲ｽｮ諠・ｱ蜿門ｾ励↓螟ｱ謨励＠縺ｾ縺励◆: {e}")

    status = geocode_data.get("status")
    if status != "OK":
        error_message = geocode_data.get("error_message", "")
        if error_message:
            raise HTTPException(status_code=400, detail=f"菴乗園縺九ｉ縺ｮ菴咲ｽｮ諠・ｱ蜿門ｾ励↓螟ｱ謨励＠縺ｾ縺励◆: {error_message}")
        raise HTTPException(status_code=400, detail=f"菴乗園縺九ｉ菴咲ｽｮ諠・ｱ縺悟叙蠕励〒縺阪∪縺帙ｓ縺ｧ縺励◆縲・{status})")

    results = geocode_data.get("results") or []
    if not results:
        raise HTTPException(status_code=400, detail="菴乗園縺九ｉ菴咲ｽｮ諠・ｱ縺悟叙蠕励〒縺阪∪縺帙ｓ縺ｧ縺励◆縲・)

    location = results[0].get("geometry", {}).get("location") or {}
    latitude = location.get("lat")
    longitude = location.get("lng")
    if latitude is None or longitude is None:
        raise HTTPException(status_code=400, detail="菴乗園縺九ｉ菴咲ｽｮ諠・ｱ縺悟叙蠕励〒縺阪∪縺帙ｓ縺ｧ縺励◆縲・)

    return latitude, longitude


def fetch_map_image(latitude, longitude, zoom):
    """Google Static Maps API縺九ｉ蝨ｰ蝗ｳ逕ｻ蜒上ｒ蜿門ｾ・""
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
        raise HTTPException(status_code=500, detail=f"蝨ｰ蝗ｳ逕ｻ蜒上・蜿門ｾ励↓螟ｱ謨励＠縺ｾ縺励◆ (zoom {zoom}): {e}")
    except (UnidentifiedImageError, OSError) as e:
        raise HTTPException(status_code=500, detail=f"蝨ｰ蝗ｳ逕ｻ蜒上・隱ｭ縺ｿ霎ｼ縺ｿ縺ｫ螟ｱ謨励＠縺ｾ縺励◆ (zoom {zoom}): {e}")

    # RGBA 竊・RGB螟画鋤
    if map_image.mode in ("RGBA", "LA"):
        bg = Image.new("RGBA", map_image.size, (255, 255, 255, 255))
        bg.paste(map_image, (0, 0), map_image)
        map_image = bg.convert("RGB")
    else:
        map_image = map_image.convert("RGB")

    return map_image


def generate_pdf(data: dict) -> BytesIO:
    """PDF逕滓・繝｡繧､繝ｳ蜃ｦ逅・""
    latitude, longitude = resolve_coordinates(data["coordinates"], data["address"])

    remarks = textwrap.fill(data["remarks"] or "", width=15, subsequent_indent="     ")
    vehicle_type_text = data["vehicle_type"]

    # 蝨ｰ蝗ｳ蜿門ｾ暦ｼ亥ｺ・沺zoom14 + 隧ｳ邏ｰzoom17縺ｮ2譫夲ｼ・    map_images = [
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

    # 菴乗園縺檎┌縺・ｴ蜷医・蠎ｧ讓吶ｒ陦ｨ遉ｺ
    if data['address']:
        location_line = f"縲畜data['address']}"
    else:
        location_line = f"蠎ｧ讓・ {latitude},{longitude}"

    text_lines = [location_line]
    if data['customer']:
        text_lines.append(f"蠕玲э蜈・ {data['customer']}")
    if data['property_name']:
        text_lines.append(f"迚ｩ莉ｶ蜷・ {data['property_name']}")
    text_lines.append(f"霆顔ｨｮ: {vehicle_type_text}")
    if remarks:
        text_lines.append(f"蛯呵・ {remarks}")

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

    # QR繧ｳ繝ｼ繝峨ｒ蜿ｳ荳九↓驟咲ｽｮ
    maps_url = f"https://www.google.com/maps?q={latitude},{longitude}"
    qr = qrcode.QRCode(version=1, box_size=6, border=2)
    qr.add_data(maps_url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    qr_size = mm_to_px(25)  # 25mm蝗帶婿
    qr_img = qr_img.resize((qr_size, qr_size), Image.LANCZOS)

    qr_x = final_width - margin_px - qr_size
    qr_y = final_height - margin_px - qr_size

    # QR繧ｳ繝ｼ繝芽レ譎ｯ・育區・・    draw.rectangle(
        (qr_x - 4, qr_y - 4, qr_x + qr_size + 4, qr_y + qr_size + 4),
        fill="white", outline="black", width=1
    )
    final_canvas.paste(qr_img, (qr_x, qr_y))

    pdf_buffer = BytesIO()
    pdf_info = {"Title": f"{data['property_name']} {data['address']}"}
    final_canvas.save(pdf_buffer, "PDF", resolution=300.0, pdfinfo=pdf_info)
    pdf_buffer.seek(0)

    return pdf_buffer


# ========== 隱崎ｨｼ髢｢騾｣繝ｫ繝ｼ繝・==========

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """繝ｭ繧ｰ繧､繝ｳ繝壹・繧ｸ"""
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    """繝ｭ繧ｰ繧､繝ｳ蜃ｦ逅・""
    user = authenticate_user(db, email, password)
    if not user:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "繝｡繝ｼ繝ｫ繧｢繝峨Ξ繧ｹ縺ｾ縺溘・繝代せ繝ｯ繝ｼ繝峨′豁｣縺励￥縺ゅｊ縺ｾ縺帙ｓ"
        })

    response = RedirectResponse(url="/", status_code=303)
    set_session_cookie(response, user.id)
    return response


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    """逋ｻ骭ｲ繝壹・繧ｸ"""
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
    """逋ｻ骭ｲ蜃ｦ逅・""
    # 繝舌Μ繝・・繧ｷ繝ｧ繝ｳ
    if len(password) < 6:
        return templates.TemplateResponse("register.html", {
            "request": request,
            "error": "繝代せ繝ｯ繝ｼ繝峨・6譁・ｭ嶺ｻ･荳翫〒蜈･蜉帙＠縺ｦ縺上□縺輔＞"
        })

    if password != password_confirm:
        return templates.TemplateResponse("register.html", {
            "request": request,
            "error": "繝代せ繝ｯ繝ｼ繝峨′荳閾ｴ縺励∪縺帙ｓ"
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
    """繝ｭ繧ｰ繧｢繧ｦ繝・""
    response = RedirectResponse(url="/login", status_code=303)
    clear_session_cookie(response)
    return response


# ========== 繝｡繧､繝ｳ繝ｫ繝ｼ繝・==========

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, db: Session = Depends(get_db)):
    """繝｡繧､繝ｳ繝壹・繧ｸ"""
    user = get_current_user(request, db)

    context = {
        "request": request,
        "user": user,
    }
    return templates.TemplateResponse("index.html", context)


@app.get("/get-coordinates")
async def get_coordinates_api(address: str = "", coordinates: str = ""):
    """蠎ｧ讓吝叙蠕輸PI・亥慍蝗ｳ陦ｨ遉ｺ逕ｨ・・""
    try:
        lat, lng = resolve_coordinates(coordinates, address)
        return {"lat": lat, "lng": lng}
    except HTTPException as e:
        return {"error": e.detail}
    except Exception as e:
        return {"error": str(e)}


@app.get("/parse-maps-url")
async def parse_maps_url_api(url: str = ""):
    """Google Maps URL縺九ｉ蠎ｧ讓吶ｒ謚ｽ蜃ｺ縺吶ｋAPI"""
    if not url:
        return {"error": "URL繧貞・蜉帙＠縺ｦ縺上□縺輔＞"}

    result = parse_google_maps_url(url)
    if result:
        return {"lat": result[0], "lng": result[1]}

    return {"error": "URL縺九ｉ蠎ｧ讓吶ｒ謚ｽ蜃ｺ縺ｧ縺阪∪縺帙ｓ縺ｧ縺励◆"}


@app.get("/generate-qrcode")
async def generate_qrcode_api(lat: float = 0, lng: float = 0):
    """蠎ｧ讓吶°繧烏oogle Maps QR繧ｳ繝ｼ繝峨ｒ逕滓・"""
    if lat == 0 and lng == 0:
        return {"error": "蠎ｧ讓吶ｒ謖・ｮ壹＠縺ｦ縺上□縺輔＞"}

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
    vehicle_type: str = Form("霆顔ｨｮ謖・ｮ壹↑縺・),
    remarks: str = Form(""),
    db: Session = Depends(get_db)
):
    """PDF逕滓・API"""
    user = get_current_user(request, db)

    # 繝ｭ繧ｰ繧､繝ｳ蠢・・    if not user:
        return RedirectResponse(url="/login", status_code=303)

    data = {
        "address": address.strip(),
        "coordinates": coordinates.strip(),
        "customer": customer.strip(),
        "property_name": property_name.strip(),
        "vehicle_type": vehicle_type,
        "remarks": remarks.strip()[:200],
    }

    # PDF逕滓・
    pdf_buffer = generate_pdf(data)

    # 菴ｿ逕ｨ繝ｭ繧ｰ險倬鹸
    usage_log = UsageLog(user_id=user.id)
    db.add(usage_log)
    db.commit()

    # 繝輔ぃ繧､繝ｫ蜷咲函謌・    address_for_filename = data["address"] or data["coordinates"]
    filename = build_output_filename(data["property_name"], address_for_filename)
    filename_encoded = quote(filename, safe='')

    return StreamingResponse(
        pdf_buffer,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename_encoded}"
        }
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

