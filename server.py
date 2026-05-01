from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
import sqlite3
import json
import datetime
import asyncio
import os
import uuid
from typing import Dict, List, Optional

app = FastAPI()

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ========== БАЗА ДАННЫХ ==========
def init_db():
    conn = sqlite3.connect("messenger.db")
    c = conn.cursor()
    
    c.execute("""CREATE TABLE IF NOT EXISTS pending_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        first_name TEXT,
        last_name TEXT,
        status TEXT DEFAULT 'pending'
    )""")
    
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        first_name TEXT,
        last_name TEXT,
        avatar_url TEXT,
        theme TEXT DEFAULT 'dark',
        wallpaper_url TEXT
    )""")
    
    c.execute("""CREATE TABLE IF NOT EXISTS chats (
        id TEXT PRIMARY KEY,
        name TEXT,
        is_group INTEGER DEFAULT 0
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
        url TEXT
    )""")
    
    # Админ по умолчанию
    c.execute("INSERT OR IGNORE INTO users (id, first_name, last_name) VALUES (1, 'Админ', 'Главный')")
    
    conn.commit()
    conn.close()

init_db()

# ========== МОДЕЛИ ==========
class PendingUser(BaseModel):
    first_name: str
    last_name: str

class ApproveUser(BaseModel):
    user_id: int
    approved: bool

class CreateChat(BaseModel):
    name: str
    user_ids: List[int]

class ThemeUpdate(BaseModel):
    theme: str
    wallpaper_url: Optional[str] = None

# ========== WebSocket МЕНЕДЖЕР ==========
class ConnectionManager:
    def __init__(self):
        self.active: Dict[int, WebSocket] = {}  # user_id -> websocket

    async def connect(self, user_id: int, websocket: WebSocket):
        await websocket.accept()
        self.active[user_id] = websocket
        await self.broadcast_status()

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

    async def broadcast_status(self):
        online = list(self.active.keys())
        for ws in self.active.values():
            await ws.send_json({"type": "online_users", "users": online})

manager = ConnectionManager()

# ========== API РОУТЫ ==========

# Подача заявки
@app.post("/api/apply")
def apply(user: PendingUser):
    conn = sqlite3.connect("messenger.db")
    c = conn.cursor()
    c.execute("INSERT INTO pending_users (first_name, last_name) VALUES (?, ?)",
              (user.first_name, user.last_name))
    conn.commit()
    user_id = c.lastrowid
    conn.close()
    
    # Уведомляем админа (user_id=1)
    asyncio.create_task(manager.send_personal({
        "type": "new_application",
        "user_id": user_id,
        "first_name": user.first_name,
        "last_name": user.last_name
    }, 1))
    
    return {"status": "pending", "user_id": user_id}

# Проверить статус заявки
@app.get("/api/check_status/{user_id}")
def check_status(user_id: int):
    conn = sqlite3.connect("messenger.db")
    c = conn.cursor()
    
    # Проверяем в pending
    c.execute("SELECT status FROM pending_users WHERE id = ?", (user_id,))
    row = c.fetchone()
    if row:
        conn.close()
        return {"status": row[0]}
    
    # Проверяем в users (одобрен)
    c.execute("SELECT id FROM users WHERE id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"status": "approved"}
    
    return {"status": "unknown"}

# Получить список заявок (для админа)
@app.get("/api/pending")
def get_pending():
    conn = sqlite3.connect("messenger.db")
    c = conn.cursor()
    c.execute("SELECT id, first_name, last_name, status FROM pending_users WHERE status = 'pending'")
    pending = [{"id": r[0], "first_name": r[1], "last_name": r[2]} for r in c.fetchall()]
    conn.close()
    return {"pending": pending}

# Одобрить или отклонить
@app.post("/api/approve")
def approve(data: ApproveUser):
    conn = sqlite3.connect("messenger.db")
    c = conn.cursor()
    
    c.execute("SELECT * FROM pending_users WHERE id = ? AND status = 'pending'", (data.user_id,))
    user = c.fetchone()
    if not user:
        conn.close()
        raise HTTPException(status_code=404, detail="Заявка не найдена")
    
    if data.approved:
        c.execute("UPDATE pending_users SET status = 'approved' WHERE id = ?", (data.user_id,))
        c.execute("INSERT INTO users (id, first_name, last_name) VALUES (?, ?, ?)",
                  (user[0], user[1], user[2]))
    else:
        c.execute("UPDATE pending_users SET status = 'rejected' WHERE id = ?", (data.user_id,))
    
    conn.commit()
    conn.close()
    return {"status": "ok"}

# Получить список пользователей
@app.get("/api/users")
def get_users():
    conn = sqlite3.connect("messenger.db")
    c = conn.cursor()
    c.execute("SELECT id, first_name, last_name, avatar_url FROM users")
    users = [{"id": r[0], "first_name": r[1], "last_name": r[2], "avatar_url": r[3]} for r in c.fetchall()]
    conn.close()
    return {"users": users}

# Создать чат
@app.post("/api/create_chat")
def create_chat(data: CreateChat):
    chat_id = str(uuid.uuid4())[:8]
    conn = sqlite3.connect("messenger.db")
    c = conn.cursor()
    c.execute("INSERT INTO chats (id, name, is_group) VALUES (?, ?, ?)",
              (chat_id, data.name, 1 if len(data.user_ids) > 2 else 0))
    for user_id in data.user_ids:
        c.execute("INSERT OR IGNORE INTO chat_members (chat_id, user_id) VALUES (?, ?)",
                  (chat_id, user_id))
    conn.commit()
    conn.close()
    return {"chat_id": chat_id}

# Получить чаты пользователя
@app.get("/api/my_chats/{user_id}")
def my_chats(user_id: int):
    conn = sqlite3.connect("messenger.db")
    c = conn.cursor()
    c.execute("""SELECT c.id, c.name FROM chats c 
                 JOIN chat_members cm ON c.id = cm.chat_id 
                 WHERE cm.user_id = ?""", (user_id,))
    chats = [{"id": r[0], "name": r[1]} for r in c.fetchall()]
    conn.close()
    return {"chats": chats}

# Загрузить файл
@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    ext = file.filename.split(".")[-1]
    filename = f"{uuid.uuid4()}.{ext}"
    filepath = os.path.join(UPLOAD_DIR, filename)
    
    with open(filepath, "wb") as f:
        content = await file.read()
        f.write(content)
    
    file_type = "image" if ext in ["jpg", "jpeg", "png", "gif"] else "video" if ext in ["mp4"] else "file"
    return {"url": f"/uploads/{filename}", "type": file_type}

# Получить сообщения
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
        await manager.broadcast_status()

# Раздача статики
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")
app.mount("/static", StaticFiles(directory="static", html=True), name="static")

@app.get("/")
def root():
    return FileResponse("static/index.html")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)