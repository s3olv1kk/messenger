from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File, Request, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import sqlite3
import json
import datetime
import asyncio
import os
import uuid
import httpx
from typing import Dict, List, Optional

app = FastAPI()

# ===== НАСТРОЙКИ =====
TELEGRAM_BOT_TOKEN = "8650988930:AAHk5gbchTiE5_zhgR0l5mE6yEGA5CxVUGI"
ADMIN_TELEGRAM_ID = 1637635130

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs("avatars", exist_ok=True)
os.makedirs("voice", exist_ok=True)

# ========== БАЗА ДАННЫХ ==========
def init_db():
    conn = sqlite3.connect("messenger.db", timeout=10)
    c = conn.cursor()
    
    c.execute("""CREATE TABLE IF NOT EXISTS pending_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        first_name TEXT,
        last_name TEXT,
        password TEXT,
        status TEXT DEFAULT 'pending',
        created_at TEXT
    )""")
    
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        first_name TEXT,
        last_name TEXT,
        password TEXT,
        avatar_url TEXT,
        theme TEXT DEFAULT 'dark',
        wallpaper_url TEXT,
        custom_theme TEXT
    )""")
    
    c.execute("""CREATE TABLE IF NOT EXISTS chats (
        id TEXT PRIMARY KEY,
        name TEXT,
        is_group INTEGER DEFAULT 0,
        created_by INTEGER
    )""")
    
    c.execute("""CREATE TABLE IF NOT EXISTS chat_members (
        chat_id TEXT,
        user_id INTEGER,
        FOREIGN KEY (chat_id) REFERENCES chats(id)
    )""")
    
    c.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT,
        sender_id INTEGER,
        sender_name TEXT,
        text TEXT,
        file_url TEXT,
        file_type TEXT,
        timestamp TEXT
    )""")
    
    c.execute("""CREATE TABLE IF NOT EXISTS stickers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        url TEXT,
        name TEXT
    )""")
    
    conn.commit()
    conn.close()

init_db()

# ========== ОТПРАВКА В TELEGRAM ==========
async def send_telegram_message(text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": ADMIN_TELEGRAM_ID, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient() as client:
        await client.post(url, json=payload)

async def notify_admin_new_request(user_id, first_name, last_name):
    text = f"🆕 <b>Новая заявка!</b>\n\n👤 <b>{first_name} {last_name}</b>\n🆔 ID: {user_id}"
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Одобрить", "callback_data": f"approve_{user_id}"},
            {"text": "❌ Отклонить", "callback_data": f"reject_{user_id}"}
        ]]
    }
    await send_telegram_message(text, json.dumps(keyboard))

# ========== МОДЕЛИ ==========
class PendingUser(BaseModel):
    first_name: str
    last_name: str
    password: str

class LoginRequest(BaseModel):
    user_id: int
    password: str

class CreateChat(BaseModel):
    name: str
    user_ids: List[int]

class ThemeUpdate(BaseModel):
    theme: Optional[str] = None
    wallpaper_url: Optional[str] = None
    custom_theme: Optional[str] = None

# ========== WebSocket МЕНЕДЖЕР ==========
class ConnectionManager:
    def __init__(self):
        self.active: Dict[int, WebSocket] = {}

    async def connect(self, user_id: int, websocket: WebSocket):
        await websocket.accept()
        self.active[user_id] = websocket

    def disconnect(self, user_id: int):
        if user_id in self.active:
            del self.active[user_id]

    async def broadcast_to_chat(self, message: dict, chat_id: str, sender_id: int = None):
        conn = sqlite3.connect("messenger.db", timeout=10)
        c = conn.cursor()
        c.execute("SELECT user_id FROM chat_members WHERE chat_id = ?", (chat_id,))
        members = [r[0] for r in c.fetchall()]
        conn.close()
        
        for user_id in members:
            if user_id != sender_id and user_id in self.active:
                await self.active[user_id].send_json(message)

manager = ConnectionManager()

# ========== API ==========

@app.post("/api/apply")
async def apply(user: PendingUser):
    conn = sqlite3.connect("messenger.db", timeout=10)
    c = conn.cursor()
    c.execute("INSERT INTO pending_users (first_name, last_name, password, created_at) VALUES (?, ?, ?, ?)",
              (user.first_name, user.last_name, user.password, datetime.datetime.now().isoformat()))
    conn.commit()
    user_id = c.lastrowid
    conn.close()
    
    await notify_admin_new_request(user_id, user.first_name, user.last_name)
    return {"status": "pending", "user_id": user_id}

@app.post("/api/login")
def login(data: LoginRequest):
    conn = sqlite3.connect("messenger.db", timeout=10)
    c = conn.cursor()
    c.execute("SELECT id, first_name, last_name, avatar_url, theme, wallpaper_url, custom_theme FROM users WHERE id = ? AND password = ?",
              (data.user_id, data.password))
    user = c.fetchone()
    conn.close()
    if user:
        return {"status": "ok", "user": {"id": user[0], "first_name": user[1], "last_name": user[2], "avatar_url": user[3], "theme": user[4], "wallpaper_url": user[5], "custom_theme": user[6]}}
    raise HTTPException(status_code=403, detail="Неверный ID или пароль")

@app.post("/api/reset_password/{user_id}")
def reset_password(user_id: int, new_password: str = Query(...)):
    conn = sqlite3.connect("messenger.db", timeout=10)
    c = conn.cursor()
    c.execute("UPDATE users SET password = ? WHERE id = ?", (new_password, user_id))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.get("/api/check_status/{user_id}")
def check_status(user_id: int):
    conn = sqlite3.connect("messenger.db", timeout=10)
    c = conn.cursor()
    
    c.execute("SELECT status FROM pending_users WHERE id = ?", (user_id,))
    row = c.fetchone()
    if row:
        if row[0] == "approved":
            c.execute("SELECT id, first_name, last_name, avatar_url, theme, wallpaper_url, custom_theme FROM users WHERE id = ?", (user_id,))
            user = c.fetchone()
            conn.close()
            if user:
                return {"status": "approved", "user": {"id": user[0], "first_name": user[1], "last_name": user[2], "avatar_url": user[3], "theme": user[4], "wallpaper_url": user[5], "custom_theme": user[6]}}
        conn.close()
        return {"status": row[0]}
    
    c.execute("SELECT id, first_name, last_name, avatar_url, theme, wallpaper_url, custom_theme FROM users WHERE id = ?", (user_id,))
    user = c.fetchone()
    conn.close()
    if user:
        return {"status": "approved", "user": {"id": user[0], "first_name": user[1], "last_name": user[2], "avatar_url": user[3], "theme": user[4], "wallpaper_url": user[5], "custom_theme": user[6]}}
    
    return {"status": "unknown"}

@app.post("/api/approve/{user_id}")
def approve_user(user_id: int):
    conn = sqlite3.connect("messenger.db", timeout=10)
    c = conn.cursor()
    
    c.execute("SELECT * FROM pending_users WHERE id = ? AND status = 'pending'", (user_id,))
    user = c.fetchone()
    if not user:
        conn.close()
        raise HTTPException(status_code=404)
    
    c.execute("UPDATE pending_users SET status = 'approved' WHERE id = ?", (user_id,))
    c.execute("INSERT INTO users (id, first_name, last_name, password) VALUES (?, ?, ?, ?)",
              (user[0], user[1], user[2], user[3]))
    
    chat_id = str(uuid.uuid4())[:8]
    c.execute("INSERT INTO chats (id, name, is_group, created_by) VALUES (?, ?, 0, ?)",
              (chat_id, f"{user[1]} {user[2]}", ADMIN_TELEGRAM_ID))
    c.execute("INSERT OR IGNORE INTO chat_members (chat_id, user_id) VALUES (?, ?)", (chat_id, ADMIN_TELEGRAM_ID))
    c.execute("INSERT OR IGNORE INTO chat_members (chat_id, user_id) VALUES (?, ?)", (chat_id, user_id))
    
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.post("/api/reject/{user_id}")
def reject_user(user_id: int):
    conn = sqlite3.connect("messenger.db", timeout=10)
    c = conn.cursor()
    c.execute("UPDATE pending_users SET status = 'rejected' WHERE id = ? AND status = 'pending'", (user_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.get("/api/users")
def get_users():
    conn = sqlite3.connect("messenger.db", timeout=10)
    c = conn.cursor()
    c.execute("SELECT id, first_name, last_name, avatar_url FROM users")
    users = [{"id": r[0], "first_name": r[1], "last_name": r[2], "avatar_url": r[3]} for r in c.fetchall()]
    conn.close()
    return {"users": users}

@app.get("/api/me/{user_id}")
def get_me(user_id: int):
    conn = sqlite3.connect("messenger.db", timeout=10)
    c = conn.cursor()
    c.execute("SELECT id, first_name, last_name, avatar_url, theme, wallpaper_url, custom_theme FROM users WHERE id = ?", (user_id,))
    user = c.fetchone()
    conn.close()
    if user:
        return {"user": {"id": user[0], "first_name": user[1], "last_name": user[2], "avatar_url": user[3], "theme": user[4], "wallpaper_url": user[5], "custom_theme": user[6]}}
    raise HTTPException(status_code=404)

@app.post("/api/update_theme/{user_id}")
def update_theme(user_id: int, data: ThemeUpdate):
    conn = sqlite3.connect("messenger.db", timeout=10)
    c = conn.cursor()
    if data.theme:
        c.execute("UPDATE users SET theme = ? WHERE id = ?", (data.theme, user_id))
    if data.wallpaper_url is not None:
        c.execute("UPDATE users SET wallpaper_url = ? WHERE id = ?", (data.wallpaper_url, user_id))
    if data.custom_theme:
        c.execute("UPDATE users SET custom_theme = ? WHERE id = ?", (data.custom_theme, user_id))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.post("/api/upload_avatar/{user_id}")
async def upload_avatar(user_id: int, file: UploadFile = File(...)):
    ext = file.filename.split(".")[-1] if "." in file.filename else "jpg"
    filename = f"avatar_{user_id}.{ext}"
    filepath = os.path.join("avatars", filename)
    
    with open(filepath, "wb") as f:
        content = await file.read()
        f.write(content)
    
    url = f"/avatars/{filename}"
    conn = sqlite3.connect("messenger.db", timeout=10)
    c = conn.cursor()
    c.execute("UPDATE users SET avatar_url = ? WHERE id = ?", (url, user_id))
    conn.commit()
    conn.close()
    return {"url": url}

@app.post("/api/create_chat")
def create_chat(data: CreateChat):
    chat_id = str(uuid.uuid4())[:8]
    conn = sqlite3.connect("messenger.db", timeout=10)
    c = conn.cursor()
    c.execute("INSERT INTO chats (id, name, is_group, created_by) VALUES (?, ?, ?, ?)",
              (chat_id, data.name, 1 if len(data.user_ids) > 2 else 0, data.user_ids[0]))
    for uid in data.user_ids:
        c.execute("INSERT OR IGNORE INTO chat_members (chat_id, user_id) VALUES (?, ?)", (chat_id, uid))
    conn.commit()
    conn.close()
    return {"chat_id": chat_id, "name": data.name}

@app.get("/api/my_chats/{user_id}")
def my_chats(user_id: int):
    conn = sqlite3.connect("messenger.db", timeout=10)
    c = conn.cursor()
    c.execute("""SELECT c.id, c.name, c.is_group FROM chats c 
                 JOIN chat_members cm ON c.id = cm.chat_id WHERE cm.user_id = ?""", (user_id,))
    chats = [{"id": r[0], "name": r[1], "is_group": r[2]} for r in c.fetchall()]
    conn.close()
    return {"chats": chats}

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    ext = file.filename.split(".")[-1] if "." in file.filename else "file"
    filename = f"{uuid.uuid4()}.{ext}"
    filepath = os.path.join(UPLOAD_DIR, filename)
    
    with open(filepath, "wb") as f:
        content = await file.read()
        f.write(content)
    
    file_type = "image" if ext.lower() in ["jpg","jpeg","png","gif","webp"] else "video" if ext.lower() in ["mp4","mov","webm"] else "voice" if ext.lower() in ["mp3","wav","ogg","webm","m4a"] else "file"
    return {"url": f"/uploads/{filename}", "type": file_type}

@app.post("/api/upload_voice")
async def upload_voice(file: UploadFile = File(...)):
    filename = f"{uuid.uuid4()}.webm"
    filepath = os.path.join("voice", filename)
    
    with open(filepath, "wb") as f:
        content = await file.read()
        f.write(content)
    
    return {"url": f"/voice/{filename}", "type": "voice"}

@app.get("/api/messages/{chat_id}")
def get_messages(chat_id: str, limit: int = 50):
    conn = sqlite3.connect("messenger.db", timeout=10)
    c = conn.cursor()
    c.execute("""SELECT sender_id, sender_name, text, file_url, file_type, timestamp 
                 FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?""", (chat_id, limit))
    messages = [{"sender_id":r[0],"sender_name":r[1],"text":r[2],"file_url":r[3],"file_type":r[4],"timestamp":r[5]} for r in c.fetchall()]
    conn.close()
    return {"messages": list(reversed(messages))}

@app.get("/api/pending")
def get_pending():
    conn = sqlite3.connect("messenger.db", timeout=10)
    c = conn.cursor()
    c.execute("SELECT id, first_name, last_name, created_at FROM pending_users WHERE status = 'pending'")
    pending = [{"id":r[0],"first_name":r[1],"last_name":r[2],"created_at":r[3]} for r in c.fetchall()]
    conn.close()
    return {"pending": pending}

# ========== Telegram Webhook ==========
@app.post("/api/telegram-webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    
    if "callback_query" in data:
        callback = data["callback_query"]
        data_cb = callback["data"]
        chat_id = callback["message"]["chat"]["id"]
        msg_id = callback["message"]["message_id"]
        
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
        
        if data_cb.startswith("approve_"):
            uid = int(data_cb.split("_")[1])
            try:
                approve_user(uid)
                await httpx.AsyncClient().post(url, json={"chat_id":chat_id,"message_id":msg_id,"text":f"✅ Заявка #{uid} одобрена!"})
            except:
                await httpx.AsyncClient().post(url, json={"chat_id":chat_id,"message_id":msg_id,"text":"❌ Ошибка"})
        elif data_cb.startswith("reject_"):
            uid = int(data_cb.split("_")[1])
            reject_user(uid)
            await httpx.AsyncClient().post(url, json={"chat_id":chat_id,"message_id":msg_id,"text":f"❌ Заявка #{uid} отклонена"})
    
    return {"status":"ok"}

# ========== WebSocket ==========
@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: int):
    await manager.connect(user_id, websocket)
    try:
        while True:
            data = await websocket.receive_json()
            
            if data["type"] == "message":
                msg = {
                    "type": "new_message",
                    "chat_id": data["chat_id"],
                    "sender_id": user_id,
                    "sender_name": data["sender_name"],
                    "text": data.get("text",""),
                    "file_url": data.get("file_url"),
                    "file_type": data.get("file_type"),
                    "timestamp": datetime.datetime.now().isoformat()
                }
                
                conn = sqlite3.connect("messenger.db", timeout=10)
                c = conn.cursor()
                c.execute("INSERT INTO messages (chat_id,sender_id,sender_name,text,file_url,file_type,timestamp) VALUES (?,?,?,?,?,?,?)",
                         (msg["chat_id"],user_id,msg["sender_name"],msg["text"],msg["file_url"],msg["file_type"],msg["timestamp"]))
                conn.commit()
                conn.close()
                
                await manager.broadcast_to_chat(msg, data["chat_id"], user_id)
                
    except WebSocketDisconnect:
        manager.disconnect(user_id)

# ========== СТАТИКА ==========
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")
app.mount("/avatars", StaticFiles(directory="avatars"), name="avatars")
app.mount("/voice", StaticFiles(directory="voice"), name="voice")
app.mount("/static", StaticFiles(directory="static", html=True), name="static")

@app.get("/")
def root():
    return FileResponse("static/index.html")

# ========== WEBHOOK ПРИ СТАРТЕ ==========
@app.on_event("startup")
async def set_webhook():
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "https://nora-uugb.onrender.com")
    webhook_url = f"{render_url}/api/telegram-webhook"
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={"url": webhook_url})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)