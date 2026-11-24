import os
import re
import random
import threading
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_sqlalchemy import SQLAlchemy
import requests

try:
    import cloudinary
    import cloudinary.uploader
    HAS_CLOUDINARY = True
except ImportError:
    HAS_CLOUDINARY = False

app = Flask(__name__)

# Database Setup - SQLite fallback ke saath (Render restart hone par bhi crash nahi karega)
db_path = os.environ.get("DATABASE_URL", "sqlite:///chat_app.db")
if db_path.startswith("postgres://"):
    db_path = db_path.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_path
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Media upload folder
UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Supabase REST API Config (naya project ref ya env se automatic)
SUPABASE_REST_URL = os.environ.get(
    "SUPABASE_REST_URL",
    "https://odgacsgunlkkvgyqknmm.supabase.co/rest/v1"
).rstrip("/")
SUPABASE_API_KEY = (
    os.environ.get("SUPABASE_SECRET_KEY")
    or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("SUPABASE_ANON_KEY")
    or os.environ.get("SUPABASE_PUBLISHABLE_KEY")
    or ""
).strip()

# Cloudinary Config (Environment Variables se lega)
if HAS_CLOUDINARY:
    if os.environ.get("CLOUDINARY_URL"):
        cloudinary.config(cloudinary_url=os.environ.get("CLOUDINARY_URL"), secure=True)
    elif os.environ.get("CLOUDINARY_CLOUD_NAME"):
        cloudinary.config(
            cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"),
            api_key=os.environ.get("CLOUDINARY_API_KEY"),
            api_secret=os.environ.get("CLOUDINARY_API_SECRET"),
            secure=True
        )

# Telegram Bot Config (Environment Variables se override ho sakta hai)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8970811998:AAG1pkIq7zWS1fZlnjN6vTMBviZ4xWKKtOc")
TELEGRAM_BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "SarkarUltronBot")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "7743339724")

# Thread-safe Call Signaling Store
CALL_LOCK = threading.Lock()
CALL_SIGNALS = {}   # target_user_id_lower -> list of signal dicts
ACTIVE_CALLS = {}   # call_id -> call metadata dict


def _supabase_headers():
    if not SUPABASE_API_KEY:
        return None
    return {
        "apikey": SUPABASE_API_KEY,
        "Authorization": f"Bearer {SUPABASE_API_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=representation"
    }


def sync_user_to_supabase(user_dict):
    headers = _supabase_headers()
    if not headers:
        return False
    try:
        payload = {
            "user_id": user_dict.get("user_id"),
            "phone": user_dict.get("phone") or None,
            "device_id": user_dict.get("device_id") or None,
            "name": user_dict.get("name"),
            "avatar_url": user_dict.get("avatar_url") or None,
            "is_online": bool(user_dict.get("is_online", True)),
            "is_blocked": bool(user_dict.get("is_blocked", False)),
            "block_type": user_dict.get("block_type", "none")
        }
        resp = requests.post(
            f"{SUPABASE_REST_URL}/users?on_conflict=user_id",
            headers=headers,
            json=payload,
            timeout=5
        )
        return resp.status_code in (200, 201, 204)
    except Exception:
        return False


def sync_message_to_supabase(msg_dict):
    headers = _supabase_headers()
    if not headers:
        return False
    try:
        payload = {
            "sender_id": msg_dict.get("sender_id"),
            "receiver_id": msg_dict.get("receiver_id"),
            "message": msg_dict.get("message", ""),
            "media_url": msg_dict.get("media_url") or None,
            "msg_type": msg_dict.get("msg_type", "text"),
            "latitude": msg_dict.get("latitude"),
            "longitude": msg_dict.get("longitude"),
            "location_label": msg_dict.get("location_label") or None,
            "is_read": bool(msg_dict.get("is_read", False))
        }
        resp = requests.post(
            f"{SUPABASE_REST_URL}/messages",
            headers=headers,
            json=payload,
            timeout=5
        )
        return resp.status_code in (200, 201, 204)
    except Exception:
        return False


def normalize_phone(raw_phone):
    digits = re.sub(r"\D", "", str(raw_phone or ""))
    if len(digits) == 10:
        return f"+91{digits}"
    if len(digits) == 12 and digits.startswith("91"):
        return f"+{digits}"
    if len(digits) == 11 and digits.startswith("0"):
        return f"+91{digits[1:]}"
    if digits:
        return f"+{digits}"
    return ""


def send_telegram_message(chat_id, text, reply_markup=None):
    if not chat_id or str(chat_id) == "YOUR_ADMIN_CHAT_ID":
        return False
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=10
        )
        return resp.status_code in (200, 201)
    except Exception:
        return False


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(50), unique=True, nullable=False)
    phone = db.Column(db.String(20), unique=True, nullable=True)
    device_id = db.Column(db.String(120), unique=True, nullable=True)
    name = db.Column(db.String(100), nullable=False)
    avatar_url = db.Column(db.String(255), nullable=True)
    is_online = db.Column(db.Boolean, default=True)
    last_login = db.Column(db.DateTime, default=db.func.current_timestamp())
    is_blocked = db.Column(db.Boolean, default=False)
    block_type = db.Column(db.String(20), default="none")  # permanent / temporary


class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.String(50), nullable=False)
    receiver_id = db.Column(db.String(50), nullable=False)
    message = db.Column(db.Text, nullable=True)
    media_url = db.Column(db.String(500), nullable=True)
    msg_type = db.Column(db.String(30), default="text")
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)
    location_label = db.Column(db.String(255), nullable=True)
    timestamp = db.Column(db.DateTime, default=db.func.current_timestamp())
    is_read = db.Column(db.Boolean, default=False)


with app.app_context():
    db.create_all()


def generate_unique_user_id():
    while True:
        code = f"u_{random.randint(100000, 999999)}"
        if not User.query.filter_by(user_id=code).first():
            return code


def serialize_user(u):
    return {
        "id": u.id,
        "user_id": u.user_id,
        "userId": u.user_id,
        "phone": u.phone or "",
        "device_id": u.device_id or "",
        "deviceId": u.device_id or "",
        "name": u.name,
        "display_name": u.name,
        "displayName": u.name,
        "avatar_url": u.avatar_url or "",
        "avatarUrl": u.avatar_url or "",
        "is_online": bool(u.is_online),
        "isOnline": bool(u.is_online),
        "last_login": str(u.last_login) if u.last_login else "",
        "lastLogin": str(u.last_login) if u.last_login else "",
        "is_blocked": bool(u.is_blocked),
        "isBlocked": bool(u.is_blocked),
        "block_type": u.block_type
    }


def serialize_message(m):
    safe_text = m.message if m.message is not None else ""
    safe_media = m.media_url if m.media_url is not None else ""
    return {
        "id": str(m.id),
        "sender_id": m.sender_id,
        "senderId": m.sender_id,
        "receiver_id": m.receiver_id,
        "receiverId": m.receiver_id,
        "message": safe_text,
        "text": safe_text,
        "content": safe_text,
        "media_url": safe_media,
        "mediaUrl": safe_media,
        "msg_type": m.msg_type or "text",
        "type": m.msg_type or "text",
        "latitude": m.latitude,
        "longitude": m.longitude,
        "location_label": m.location_label or "",
        "locationLabel": m.location_label or "",
        "timestamp": str(m.timestamp) if m.timestamp else "",
        "is_read": bool(m.is_read),
        "isRead": bool(m.is_read)
    }


def _process_direct_login(data):
    raw_name = str(
        data.get('name') or data.get('username') or data.get('display_name') or data.get('displayName') or ''
    ).strip()
    raw_uid = str(
        data.get('user_id') or data.get('userId') or data.get('id') or ''
    ).strip()
    raw_phone = str(data.get('phone') or data.get('mobile') or '').strip()
    phone = normalize_phone(raw_phone) if raw_phone else None
    device_id = str(data.get('device_id') or data.get('deviceId') or '').strip() or None
    avatar_url = str(data.get('avatar_url') or data.get('avatarUrl') or data.get('photo_url') or '').strip()

    if not raw_name and not raw_uid and not phone:
        return jsonify({"error": "कृपया यूजरनेम या यूजर ID दर्ज करें।"}), 400

    # Single-device lock
    if device_id:
        device_owner = User.query.filter_by(device_id=device_id).first()
        if device_owner:
            same_account = (
                (raw_uid and device_owner.user_id.lower() == raw_uid.lower()) or
                (phone and device_owner.phone == phone) or
                (not raw_uid and not phone and raw_name and device_owner.name.lower() == raw_name.lower())
            )
            if not same_account:
                return jsonify({
                    "error": "इस डिवाइस पर पहले से एक अकाउंट लॉगिन है। एक डिवाइस में केवल एक ही अकाउंट चल सकता है।"
                }), 403

    user = None
    if raw_uid:
        user = User.query.filter(db.func.lower(User.user_id) == raw_uid.lower()).first()
    if not user and phone:
        user = User.query.filter_by(phone=phone).first()

    if user:
        if user.is_blocked:
            return jsonify({"error": "You are blocked by the administrator."}), 403
        if user.device_id and device_id and user.device_id != device_id:
            return jsonify({
                "error": "यह यूजर ID पहले से किसी अन्य डिवाइस से लॉक है।"
            }), 403
        if raw_name:
            user.name = raw_name
        if phone and not user.phone:
            user.phone = phone
        if device_id and not user.device_id:
            user.device_id = device_id
        if avatar_url:
            user.avatar_url = avatar_url
        user.is_online = True
        user.last_login = datetime.utcnow()
        db.session.commit()
    else:
        uid_to_use = raw_uid if raw_uid else generate_unique_user_id()
        existing_uid = User.query.filter(db.func.lower(User.user_id) == uid_to_use.lower()).first()
        if existing_uid:
            if existing_uid.is_blocked:
                return jsonify({"error": "You are blocked by the administrator."}), 403
            if existing_uid.device_id and device_id and existing_uid.device_id != device_id:
                return jsonify({
                    "error": "यह यूजर ID पहले से किसी अन्य डिवाइस से लॉक है।"
                }), 403
            user = existing_uid
            if raw_name:
                user.name = raw_name
            if phone and not user.phone:
                user.phone = phone
            if device_id and not user.device_id:
                user.device_id = device_id
            if avatar_url:
                user.avatar_url = avatar_url
            user.is_online = True
            user.last_login = datetime.utcnow()
            db.session.commit()
        else:
            final_name = raw_name if raw_name else uid_to_use
            user = User(
                user_id=uid_to_use,
                phone=phone,
                device_id=device_id,
                name=final_name,
                avatar_url=avatar_url or None,
                is_online=True,
                last_login=datetime.utcnow()
            )
            db.session.add(user)
            db.session.commit()

            send_telegram_message(
                ADMIN_CHAT_ID,
                f"👤 *नया यूजर लॉगिन हुआ!*\nनाम: {user.name}\n🆔 आईडी: `{user.user_id}`"
            )

    user_dict = serialize_user(user)
    sync_user_to_supabase(user_dict)

    return jsonify({
        "status": "logged_in",
        "verified": True,
        "user": user_dict,
        "user_id": user.user_id,
        "userId": user.user_id,
        "name": user.name,
        "phone": user.phone or "",
        "avatar_url": user.avatar_url or ""
    }), 200


@app.route('/', methods=['GET'])
def home():
    base_url = request.host_url.rstrip("/")
    return jsonify({
        "status": "running",
        "message": "Hello Sir Backend is Live and Running successfully!",
        "server_url": base_url,
        "supabase_rest": SUPABASE_REST_URL,
        "supabase_synced": bool(SUPABASE_API_KEY),
        "cloudinary_enabled": HAS_CLOUDINARY and bool(
            os.environ.get("CLOUDINARY_URL") or os.environ.get("CLOUDINARY_CLOUD_NAME")
        )
    }), 200


@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({
        "status": "healthy",
        "message": "Backend is running fine",
        "server_url": request.host_url.rstrip("/")
    }), 200


@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    return _process_direct_login(request.get_json(silent=True) or {})


@app.route('/api/users/register', methods=['POST'])
def register_user():
    data = request.get_json(silent=True) or {}
    if not data.get('name') and not data.get('username') and not data.get('display_name') and not data.get('displayName') and data:
        keys = list(data.keys())
        if len(keys) > 1 and not data.get('user_id') and not data.get('userId'):
            data = dict(data)
            data['user_id'] = data.get(keys[0])
            data['name'] = data.get(keys[1])
    return _process_direct_login(data)


@app.route('/api/users/profile', methods=['POST', 'PUT'])
def update_profile():
    data = request.get_json(silent=True) or {}
    uid = str(data.get('user_id') or data.get('userId') or '').strip()
    phone = normalize_phone(str(data.get('phone') or '').strip())
    name = data.get('name')
    avatar_url = data.get('avatar_url') or data.get('avatarUrl')

    user = None
    if uid:
        user = User.query.filter(db.func.lower(User.user_id) == uid.lower()).first()
    if not user and phone:
        user = User.query.filter_by(phone=phone).first()

    if not user:
        return jsonify({"error": "यूजर नहीं मिला।"}), 404

    if name is not None:
        clean_name = str(name).strip()
        if not clean_name:
            return jsonify({"error": "नाम खाली नहीं हो सकता।"}), 400
        user.name = clean_name

    if avatar_url is not None:
        user.avatar_url = str(avatar_url).strip() or None

    db.session.commit()
    user_dict = serialize_user(user)
    sync_user_to_supabase(user_dict)

    return jsonify({
        "status": "updated",
        "user": user_dict
    }), 200


@app.route('/api/users/find', methods=['GET'])
def find_user():
    raw_q = str(
        request.args.get('q') or
        request.args.get('query') or
        request.args.get('phone') or
        request.args.get('user_id') or
        request.args.get('userId') or
        request.args.get('id') or
        ''
    ).strip()

    if not raw_q:
        users = User.query.order_by(User.last_login.desc()).all()
        users_list = [serialize_user(u) for u in users]
        return jsonify({"found": len(users_list) > 0, "users": users_list}), 200

    q_lower = raw_q.lower()
    digits_only = re.sub(r"\D", "", raw_q)
    norm_phone = normalize_phone(raw_q)

    matched = []
    for u in User.query.order_by(User.last_login.desc()).all():
        uid_str = str(u.user_id or "")
        name_str = str(u.name or "")
        phone_str = str(u.phone or "")
        u_digits = re.sub(r"\D", "", phone_str)
        uid_digits = re.sub(r"\D", "", uid_str)

        if (
            q_lower in uid_str.lower() or
            q_lower in name_str.lower() or
            (norm_phone and phone_str == norm_phone) or
            (digits_only and u_digits and (digits_only in u_digits or u_digits.endswith(digits_only))) or
            (digits_only and uid_digits and (digits_only in uid_digits or uid_digits.endswith(digits_only)))
        ):
            matched.append(serialize_user(u))

    if not matched:
        return jsonify({"found": False, "users": [], "error": "इस नाम/नंबर/आईडी से कोई यूजर नहीं मिला।"}), 404

    return jsonify({
        "found": True,
        "user": matched[0],
        "users": matched
    }), 200


@app.route('/api/users/search', methods=['GET'])
def search_users():
    return find_user()


@app.route('/api/users', methods=['GET'])
def get_user_directory():
    users = User.query.order_by(User.last_login.desc()).all()
    users_list = [serialize_user(u) for u in users]
    return jsonify({"users": users_list, "count": len(users_list)})


@app.route('/api/webhooks/admin-event', methods=['POST'])
def admin_webhook_event():
    return jsonify({"status": "success"})


@app.route('/api/messages', methods=['POST'])
def send_message():
    data = request.get_json(silent=True) or {}
    sender = str(data.get('sender_id') or data.get('senderId') or '').strip()
    receiver = str(data.get('receiver_id') or data.get('receiverId') or '').strip()
    msg_text = str(
        data.get('message') if data.get('message') is not None else
        (data.get('text') if data.get('text') is not None else (data.get('content') or ''))
    )
    media = str(data.get('media_url') or data.get('mediaUrl') or '').strip() or None
    msg_type = str(data.get('msg_type') or data.get('type') or ('image' if media else 'text')).strip()
    lat = data.get('latitude')
    lng = data.get('longitude')
    loc_label = str(data.get('location_label') or data.get('locationLabel') or '').strip() or None

    if not sender or not receiver:
        return jsonify({"error": "sender_id और receiver_id जरूरी हैं।"}), 400

    user_check = User.query.filter(db.func.lower(User.user_id) == sender.lower()).first()
    if user_check and user_check.is_blocked:
        return jsonify({"error": "You are blocked by the administrator."}), 403

    new_msg = Message(
        sender_id=sender,
        receiver_id=receiver,
        message=msg_text,
        media_url=media,
        msg_type=msg_type,
        latitude=float(lat) if lat is not None and str(lat).strip() != "" else None,
        longitude=float(lng) if lng is not None and str(lng).strip() != "" else None,
        location_label=loc_label
    )
    db.session.add(new_msg)
    db.session.commit()

    msg_dict = serialize_message(new_msg)
    sync_message_to_supabase(msg_dict)

    if ADMIN_CHAT_ID != "YOUR_ADMIN_CHAT_ID":
        preview_text = msg_text or loc_label or media or "(मीडिया/लोकेशन)"
        tg_text = f"📩 *नया मैसेज आया:*\n👤 यूजर आईडी: `{sender}` ➔ `{receiver}`\n💬 मैसेज: {preview_text}"
        send_telegram_message(
            ADMIN_CHAT_ID,
            tg_text,
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "🚫 Permanent Block", "callback_data": f"perm_block_{sender}"},
                        {"text": "⏱️ Temp Block", "callback_data": f"temp_block_{sender}"}
                    ],
                    [
                        {"text": "✅ Unblock", "callback_data": f"unblock_{sender}"}
                    ]
                ]
            }
        )

    return jsonify(msg_dict), 200


@app.route('/api/messages/<peer_id>', methods=['GET'])
def get_messages(peer_id):
    current_user_id = str(request.args.get('userId') or request.args.get('user_id') or '').strip()
    if current_user_id:
        messages = Message.query.filter(
            ((Message.sender_id == current_user_id) & (Message.receiver_id == peer_id)) |
            ((Message.sender_id == peer_id) & (Message.receiver_id == current_user_id))
        ).order_by(Message.id.asc()).all()
    else:
        messages = Message.query.filter(
            (Message.sender_id == peer_id) | (Message.receiver_id == peer_id)
        ).order_by(Message.id.asc()).all()

    msgs_list = [serialize_message(m) for m in messages]
    return jsonify({"messages": msgs_list}), 200


@app.route('/api/messages/<message_id>', methods=['DELETE'])
def delete_message(message_id):
    msg = Message.query.get(message_id)
    if msg:
        db.session.delete(msg)
        db.session.commit()
        return jsonify({"status": "success"})
    return jsonify({"error": "Message not found"}), 404


@app.route('/api/messages/<peer_id>/read', methods=['POST'])
def mark_conversation_read(peer_id):
    data = request.get_json(silent=True) or {}
    reader_id = str(data.get('user_id') or data.get('userId') or request.args.get('userId') or '').strip()
    query = Message.query.filter(Message.sender_id == peer_id)
    if reader_id:
        query = query.filter(Message.receiver_id == reader_id)
    query.update({"is_read": True}, synchronize_session=False)
    db.session.commit()
    return jsonify({"status": "success"})


@app.route('/uploads/<path:filename>', methods=['GET'])
def serve_upload(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


@app.route('/api/media/upload', methods=['POST'])
def upload_media():
    base_url = request.host_url.rstrip("/")

    if request.files:
        f = (
            request.files.get('file')
            or request.files.get('media')
            or request.files.get('image')
            or next(iter(request.files.values()), None)
        )
        if f and f.filename:
            safe_name = f"{int(datetime.utcnow().timestamp() * 1000)}_{re.sub(r'[^A-Za-z0-9._-]', '_', f.filename)}"
            save_path = os.path.join(UPLOAD_FOLDER, safe_name)
            f.save(save_path)

            ext = os.path.splitext(safe_name)[1].lower()
            m_type = "video" if ext in (".mp4", ".mov", ".mkv", ".webm", ".3gp", ".avi") else "image"

            # Cloudinary configured ho to wahan bhi upload karke secure_url lein
            cloud_url = None
            if HAS_CLOUDINARY and (os.environ.get("CLOUDINARY_URL") or os.environ.get("CLOUDINARY_CLOUD_NAME")):
                try:
                    up_res = cloudinary.uploader.upload(
                        save_path,
                        folder="hello_sir/media",
                        resource_type="auto"
                    )
                    cloud_url = up_res.get("secure_url")
                except Exception:
                    cloud_url = None

            final_url = cloud_url or f"{base_url}/uploads/{safe_name}"
            return jsonify({
                "media_url": final_url,
                "mediaUrl": final_url,
                "url": final_url,
                "type": m_type
            }), 200

    data = request.get_json(silent=True) or {}
    raw_url = str(data.get('media_url') or data.get('mediaUrl') or data.get('url') or '').strip()
    if raw_url.startswith("http://") or raw_url.startswith("https://") or raw_url.startswith("/uploads/"):
        final_url = f"{base_url}{raw_url}" if raw_url.startswith("/") else raw_url
        return jsonify({
            "media_url": final_url,
            "mediaUrl": final_url,
            "url": final_url,
            "type": str(data.get('type') or "image")
        }), 200

    return jsonify({"error": "मान्य मीडिया फ़ाइल या URL प्राप्त नहीं हुआ।"}), 400


@app.route('/api/webrtc/ice-servers', methods=['GET'])
def get_ice_servers():
    return jsonify({
        "ice_servers": [
            {"urls": "stun:stun.l.google.com:19302"},
            {"urls": "stun:stun1.l.google.com:19302"},
            {"urls": "stun:stun2.l.google.com:19302"}
        ]
    }), 200


@app.route('/api/calls/signal', methods=['POST'])
def push_call_signal():
    data = request.get_json(silent=True) or {}
    to_user = str(
        data.get('to') or data.get('to_user_id') or data.get('toUserId') or
        data.get('receiver_id') or data.get('receiverId') or data.get('target_user_id') or ''
    ).strip()
    from_user = str(
        data.get('from') or data.get('from_user_id') or data.get('fromUserId') or
        data.get('sender_id') or data.get('senderId') or data.get('caller_id') or ''
    ).strip()
    sig_type = str(
        data.get('type') or data.get('event') or data.get('action') or data.get('signal_type') or 'offer'
    ).strip()
    call_id = str(data.get('call_id') or data.get('callId') or f"call_{from_user}_{to_user}").strip()
    is_video = bool(data.get('is_video') if data.get('is_video') is not None else data.get('video', False))

    if not to_user:
        return jsonify({"error": "to_user_id जरूरी है।"}), 400

    caller_row = User.query.filter(db.func.lower(User.user_id) == from_user.lower()).first() if from_user else None
    caller_name = str(
        data.get('caller_name') or data.get('callerName') or data.get('from_name') or
        (caller_row.name if caller_row else "") or from_user
    ).strip()

    payload = dict(data)
    payload["type"] = sig_type
    payload["signal_type"] = sig_type
    payload["from"] = from_user
    payload["from_user_id"] = from_user
    payload["fromUserId"] = from_user
    payload["caller_id"] = from_user
    payload["callerName"] = caller_name
    payload["caller_name"] = caller_name
    payload["to"] = to_user
    payload["to_user_id"] = to_user
    payload["toUserId"] = to_user
    payload["receiver_id"] = to_user
    payload["call_id"] = call_id
    payload["callId"] = call_id
    payload["is_video"] = is_video
    payload["video"] = is_video
    payload["timestamp"] = datetime.utcnow().isoformat()

    with CALL_LOCK:
        target_key = to_user.lower()
        CALL_SIGNALS.setdefault(target_key, []).append(payload)

        if sig_type.lower() in ("offer", "call_invite", "incoming_call", "ringing"):
            ACTIVE_CALLS[call_id] = {
                "call_id": call_id,
                "from_user_id": from_user,
                "caller_name": caller_name,
                "to_user_id": to_user,
                "is_video": is_video,
                "status": "ringing",
                "updated_at": payload["timestamp"]
            }
        elif sig_type.lower() in ("answer", "accept", "accepted"):
            if call_id in ACTIVE_CALLS:
                ACTIVE_CALLS[call_id]["status"] = "connected"
                ACTIVE_CALLS[call_id]["updated_at"] = payload["timestamp"]
        elif sig_type.lower() in ("hangup", "end", "ended", "decline", "declined", "reject", "rejected", "busy", "cancel"):
            ACTIVE_CALLS.pop(call_id, None)

    return jsonify({
        "status": "queued",
        "call_id": call_id,
        "signal": payload
    }), 200


@app.route('/api/calls/poll', methods=['GET'])
def poll_call_signals():
    uid = str(
        request.args.get('user_id') or request.args.get('userId') or request.args.get('id') or ''
    ).strip()
    if not uid:
        return jsonify({"signals": [], "calls": [], "incoming_call": None}), 200

    uid_lower = uid.lower()
    with CALL_LOCK:
        signals = CALL_SIGNALS.pop(uid_lower, [])
        incoming = None
        for call in ACTIVE_CALLS.values():
            if str(call.get("to_user_id") or "").lower() == uid_lower and call.get("status") == "ringing":
                incoming = call
                break

    return jsonify({
        "signals": signals,
        "calls": signals,
        "incoming_call": incoming
    }), 200


@app.route('/api/calls/active', methods=['GET'])
def active_call_status():
    uid = str(
        request.args.get('user_id') or request.args.get('userId') or ''
    ).strip().lower()
    with CALL_LOCK:
        matching = [
            c for c in ACTIVE_CALLS.values()
            if (not uid or str(c.get("to_user_id") or "").lower() == uid or str(c.get("from_user_id") or "").lower() == uid)
        ]
    return jsonify({"calls": matching, "incoming_call": matching[0] if matching else None}), 200


@app.route('/telegram_webhook', methods=['POST'])
def telegram_webhook():
    update = request.get_json(silent=True) or {}
    if "callback_query" in update:
        callback = update["callback_query"]
        data = callback["data"]
        chat_id = callback["message"]["chat"]["id"]

        parts = data.split("_")
        if len(parts) >= 3:
            action = parts[0] + "_" + parts[1]
            target_user = "_".join(parts[2:])

            user = User.query.filter_by(user_id=target_user).first()
            if user:
                if action == "perm_block":
                    user.is_blocked = True
                    user.block_type = "permanent"
                    msg = f"🚫 यूजर {target_user} को परमानेंट ब्लॉक कर दिया गया है।"
                elif action == "temp_block":
                    user.is_blocked = True
                    user.block_type = "temporary"
                    msg = f"⏱️ यूजर {target_user} को कुछ समय के लिए ब्लॉक कर दिया गया है।"
                elif action == "unblock":
                    user.is_blocked = False
                    user.block_type = "none"
                    msg = f"✅ यूजर {target_user} को अनब्लॉक कर दिया गया है।"
                db.session.commit()
                sync_user_to_supabase(serialize_user(user))
            else:
                msg = "यूजर नहीं मिला।"

            send_telegram_message(chat_id, msg)

    elif "message" in update:
        text = (update["message"].get("text") or "").strip()
        chat_id = str(update["message"]["chat"]["id"])

        if text.startswith("/start"):
            send_telegram_message(
                chat_id,
                "👋 *Hello Sir* एडमिन/नोटिफिकेशन बॉट तैयार है। लॉगिन सीधे ऐप में Username और User ID से किया जा सकता है।"
            )

        elif text.startswith("/history"):
            parts = text.split(" ", 1)
            if len(parts) > 1:
                target_id = parts[1].strip()
                messages = Message.query.filter(
                    (Message.sender_id == target_id) | (Message.receiver_id == target_id)
                ).order_by(Message.id.asc()).all()

                if messages:
                    history_text = f"📜 *यूजर {target_id} की चैट हिस्ट्री:*\n\n"
                    for m in messages:
                        history_text += f"[{m.timestamp}] {m.sender_id} ➔ {m.receiver_id}: {m.message or m.media_url or m.location_label or ''}\n"
                else:
                    history_text = f"इस यूजर ({target_id}) की कोई चैट हिस्ट्री नहीं मिली।"

                send_telegram_message(chat_id, history_text)

    return jsonify({"status": "ok"})


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
