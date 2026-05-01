from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
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

# ===== НАСТРОЙКИ TELEGRAM =====
TELEGRAM_BOT_TOKEN = "8650988930:AAHk5gbchTiE5_zhgR0l5mE6yEGA5CxVUGI"
ADMIN_TELEGRAM_ID = "1637635130"

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ========== БАЗА ДАННЫХ ==========
def init_db():
    conn = sqlite3.connect("messenger.db")
    c = conn.cursor()
    
    # Удаляем старые таблицы
    c.execute("DROP TABLE IF EXISTS pending_users")
    c.execute("DROP TABLE IF EXISTS users")
    c.execute("DROP TABLE IF EXISTS chats")
    c.execute("DROP TABLE IF EXISTS chat_members")
    c.execute("DROP TABLE IF EXISTS messages")
    c.execute("DROP TABLE IF EXISTS stickers")
    
    # Создаём заново
    c.execute("""CREATE TABLE pending_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        first_name TEXT,
        last_name TEXT,
        status TEXT DEFAULT 'pending',
        created_at TEXT
    )""")
    
    c.execute("""CREATE TABLE users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        first_name TEXT,
        last_name TEXT,
        avatar_url TEXT,
        theme TEXT DEFAULT 'dark',
        wallpaper_url TEXT
    )""")
    
    c.execute("""CREATE TABLE chats (
        id TEXT PRIMARY KEY,
        name TEXT,
        is_group INTEGER DEFAULT 0,
        created_by INTEGER
    )""")
    
    c.execute("""CREATE TABLE chat_members (
        chat_id TEXT,
        user_id INTEGER,
        FOREIGN KEY (chat_id) REFERENCES chats(id)
    )""")
    
    c.execute("""CREATE TABLE messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT,
        sender_id INTEGER,
        sender_name TEXT,
        text TEXT,
        file_url TEXT,
        file_type TEXT,
        timestamp TEXT
    )""")
    
    c.execute("""CREATE TABLE stickers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        url TEXT,
        name TEXT
    )""")
    
    c.execute("INSERT INTO users (id, first_name, last_name) VALUES (1, 'Admin', 'Admin')")
    
    conn.commit()
    conn.close()
    
init_db()

# ========== ОТПРАВКА В TELEGRAM ==========
async def send_telegram_message(text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": ADMIN_TELEGRAM_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    
    async with httpx.AsyncClient() as client:
        await client.post(url, json=payload)

async def notify_admin_new_request(user_id, first_name, last_name):
    text = f"🆕 <b>Новая заявка!</b>\n\n👤 <b>{first_name} {last_name}</b>\n🆔 ID: {user_id}"
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Одобрить", "callback_data": f"approve_{user_id}"},
                {"text": "❌ Отклонить", "callback_data": f"reject_{user_id}"}
            ]
        ]
    }
    await send_telegram_message(text, json.dumps(keyboard))

# ========== МОДЕЛИ ==========
class PendingUser(BaseModel):
    first_name: str
    last_name: str

class CreateChat(BaseModel):
    name: str
    user_ids: List[int]

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

    async def send_personal(self, message: dict, user_id: int):
        if user_id in self.active:
            await self.active[user_id].send_json(message)

    async def broadcast_to_chat(self, message: dict, chat_id: str, sender_id: int = None):
        conn = sqlite3.connect("messenger.db")
        c = conn.cursor()
        c.execute("SELECT user_id FROM chat_members WHERE chat_id = ?", (chat_id,))
        members = [r[0] for r in c.fetchall()]
        conn.close()
        
        for user_id in members:
            if user_id != sender_id and user_id in self.active:
                await self.active[user_id].send_json(message)

manager = ConnectionManager()

# ========== API РОУТЫ ==========

@app.post("/api/apply")
async def apply(user: PendingUser):
    conn = sqlite3.connect("messenger.db")
    c = conn.cursor()
    c.execute("INSERT INTO pending_users (first_name, last_name, created_at) VALUES (?, ?, ?)",
              (user.first_name, user.last_name, datetime.datetime.now().isoformat()))
    conn.commit()
    user_id = c.lastrowid
    conn.close()
    
    # Уведомление в Telegram
    await notify_admin_new_request(user_id, user.first_name, user.last_name)
    
    return {"status": "pending", "user_id": user_id}

@app.get("/api/check_status/{user_id}")
def check_status(user_id: int):
    conn = sqlite3.connect("messenger.db")
    c = conn.cursor()
    
    c.execute("SELECT status FROM pending_users WHERE id = ?", (user_id,))
    row = c.fetchone()
    if row:
        if row[0] == "approved":
            c.execute("SELECT id, first_name, last_name FROM users WHERE id = ?", (user_id,))
            user = c.fetchone()
            conn.close()
            return {"status": "approved", "user": {"id": user[0], "first_name": user[1], "last_name": user[2]}}
        conn.close()
        return {"status": row[0]}
    
    c.execute("SELECT id, first_name, last_name FROM users WHERE id = ?", (user_id,))
    user = c.fetchone()
    conn.close()
    if user:
        return {"status": "approved", "user": {"id": user[0], "first_name": user[1], "last_name": user[2]}}
    
    return {"status": "unknown"}

@app.get("/api/pending")
def get_pending():
    conn = sqlite3.connect("messenger.db")
    c = conn.cursor()
    c.execute("SELECT id, first_name, last_name, created_at FROM pending_users WHERE status = 'pending'")
    pending = [{"id": r[0], "first_name": r[1], "last_name": r[2], "created_at": r[3]} for r in c.fetchall()]
    conn.close()
    return {"pending": pending}

@app.post("/api/approve/{user_id}")
def approve_user(user_id: int):
    conn = sqlite3.connect("messenger.db")
    c = conn.cursor()
    
    c.execute("SELECT * FROM pending_users WHERE id = ? AND status = 'pending'", (user_id,))
    user = c.fetchone()
    if not user:
        conn.close()
        raise HTTPException(status_code=404, detail="Заявка не найдена")
    
    c.execute("UPDATE pending_users SET status = 'approved' WHERE id = ?", (user_id,))
    c.execute("INSERT INTO users (id, first_name, last_name) VALUES (?, ?, ?)",
              (user[0], user[1], user[2]))
    
    # Создаём общий чат с админом
    chat_id = str(uuid.uuid4())[:8]
    c.execute("INSERT INTO chats (id, name, is_group, created_by) VALUES (?, ?, 0, ?)",
              (chat_id, f"{user[1]} {user[2]}", 1))
    c.execute("INSERT INTO chat_members (chat_id, user_id) VALUES (?, ?)", (chat_id, 1))
    c.execute("INSERT INTO chat_members (chat_id, user_id) VALUES (?, ?)", (chat_id, user_id))
    
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.post("/api/reject/{user_id}")
def reject_user(user_id: int):
    conn = sqlite3.connect("messenger.db")
    c = conn.cursor()
    c.execute("UPDATE pending_users SET status = 'rejected' WHERE id = ? AND status = 'pending'", (user_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.get("/api/users")
def get_users():
    conn = sqlite3.connect("messenger.db")
    c = conn.cursor()
    c.execute("SELECT id, first_name, last_name, avatar_url FROM users")
    users = [{"id": r[0], "first_name": r[1], "last_name": r[2], "avatar_url": r[3]} for r in c.fetchall()]
    conn.close()
    return {"users": users}

@app.post("/api/create_chat")
def create_chat(data: CreateChat):
    chat_id = str(uuid.uuid4())[:8]
    conn = sqlite3.connect("messenger.db")
    c = conn.cursor()
    is_group = len(data.user_ids) > 2
    c.execute("INSERT INTO chats (id, name, is_group, created_by) VALUES (?, ?, ?, ?)",
              (chat_id, data.name, 1 if is_group else 0, data.user_ids[0]))
    for user_id in data.user_ids:
        c.execute("INSERT OR IGNORE INTO chat_members (chat_id, user_id) VALUES (?, ?)",
                  (chat_id, user_id))
    conn.commit()
    conn.close()
    return {"chat_id": chat_id, "name": data.name}

@app.get("/api/my_chats/{user_id}")
def my_chats(user_id: int):
    conn = sqlite3.connect("messenger.db")
    c = conn.cursor()
    c.execute("""SELECT c.id, c.name, c.is_group FROM chats c 
                 JOIN chat_members cm ON c.id = cm.chat_id 
                 WHERE cm.user_id = ?""", (user_id,))
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
    
    file_type = "image" if ext.lower() in ["jpg", "jpeg", "png", "gif", "webp"] else \
                "video" if ext.lower() in ["mp4", "mov", "webm"] else "file"
    return {"url": f"/uploads/{filename}", "type": file_type}

@app.get("/api/messages/{chat_id}")
def get_messages(chat_id: str, limit: int = 50):
    conn = sqlite3.connect("messenger.db")
    c = conn.cursor()
    c.execute("""SELECT sender_id, sender_name, text, file_url, file_type, timestamp 
                 FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?""",
              (chat_id, limit))
    messages = [{"sender_id": r[0], "sender_name": r[1], "text": r[2], 
                 "file_url": r[3], "file_type": r[4], "timestamp": r[5]} for r in c.fetchall()]
    conn.close()
    return {"messages": list(reversed(messages))}

# Webhook для Telegram
@app.post("/api/telegram-webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    
    if "callback_query" in data:
        callback = data["callback_query"]
        data_callback = callback["data"]
        chat_id = callback["message"]["chat"]["id"]
        message_id = callback["message"]["message_id"]
        
        if data_callback.startswith("approve_"):
            user_id = int(data_callback.split("_")[1])
            approve_user(user_id)
            
            # Обновляем сообщение в Telegram
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
            await httpx.AsyncClient().post(url, json={
                "chat_id": chat_id,
                "message_id": message_id,
                "text": f"✅ Заявка #{user_id} одобрена!"
            })
            
        elif data_callback.startswith("reject_"):
            user_id = int(data_callback.split("_")[1])
            reject_user(user_id)
            
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
            await httpx.AsyncClient().post(url, json={
                "chat_id": chat_id,
                "message_id": message_id,
                "text": f"❌ Заявка #{user_id} отклонена"
            })
    
    return {"status": "ok"}

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
                    "text": data.get("text", ""),
                    "file_url": data.get("file_url"),
                    "file_type": data.get("file_type"),
                    "timestamp": datetime.datetime.now().isoformat()
                }
                
                conn = sqlite3.connect("messenger.db")
                c = conn.cursor()
                c.execute("""INSERT INTO messages (chat_id, sender_id, sender_name, text, file_url, file_type, timestamp) 
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                         (msg["chat_id"], user_id, msg["sender_name"], msg["text"], 
                          msg["file_url"], msg["file_type"], msg["timestamp"]))
                conn.commit()
                conn.close()
                
                await manager.broadcast_to_chat(msg, data["chat_id"], user_id)
                
    except WebSocketDisconnect:
        manager.disconnect(user_id)

# Раздача статики
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")
app.mount("/static", StaticFiles(directory="static", html=True), name="static")

@app.get("/")
def root():
    return FileResponse("static/index.html")

# ========== УСТАНОВКА WEBHOOK TELEGRAM ==========
@app.on_event("startup")
async def set_webhook():
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "https://test-c1u3.onrender.com")
    webhook_url = f"{render_url}/api/telegram-webhook"
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={"url": webhook_url})
    print(f"Webhook set to {webhook_url}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)