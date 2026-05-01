from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import sqlite3
import json
import hashlib
import datetime
import asyncio
from typing import Dict, List

app = FastAPI()

# ========== БАЗА ДАННЫХ ==========
def init_db():
    conn = sqlite3.connect("messenger.db")
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE,
        avatar_color TEXT,
        invite_code TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender TEXT,
        chat_id TEXT,
        text TEXT,
        timestamp TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS invites (
        code TEXT PRIMARY KEY,
        created_by TEXT,
        used INTEGER DEFAULT 0
    )""")
    # Создаём инвайт-код, если его нет
    c.execute("INSERT OR IGNORE INTO invites (code, created_by) VALUES (?, ?)", ("CLUB2024", "admin"))
    conn.commit()
    conn.close()

init_db()

# ========== МОДЕЛИ ==========
class UserCreate(BaseModel):
    name: str
    invite_code: str

class MessageRequest(BaseModel):
    sender: str
    chat_id: str
    text: str

# ========== WebSocket МЕНЕДЖЕР ==========
class ConnectionManager:
    def __init__(self):
        self.active: Dict[str, WebSocket] = {}
        self.typing_status: Dict[str, Dict[str, bool]] = {}  # chat_id -> {username: typing}

    async def connect(self, username: str, websocket: WebSocket):
        await websocket.accept()
        self.active[username] = websocket
        await self.broadcast_status()

    def disconnect(self, username: str):
        if username in self.active:
            del self.active[username]
        # Убираем статус печати
        for chat_id in self.typing_status:
            if username in self.typing_status[chat_id]:
                del self.typing_status[chat_id][username]

    async def send_personal(self, message: dict, username: str):
        if username in self.active:
            await self.active[username].send_json(message)

    async def broadcast_to_chat(self, message: dict, chat_id: str, exclude: str = None):
        # В нашей простой модели chat_id = "general" для общего чата
        # или "user1_user2" для личного (алфавитный порядок)
        for username, ws in self.active.items():
            if username != exclude:
                await ws.send_json(message)

    async def broadcast_status(self):
        online = list(self.active.keys())
        for ws in self.active.values():
            await ws.send_json({"type": "online_users", "users": online})

manager = ConnectionManager()

# ========== API РОУТЫ ==========
@app.post("/api/register")
def register(user: UserCreate):
    conn = sqlite3.connect("messenger.db")
    c = conn.cursor()
    
    # Проверяем инвайт-код
    c.execute("SELECT * FROM invites WHERE code = ? AND used = 0", (user.invite_code,))
    invite = c.fetchone()
    if not invite:
        raise HTTPException(status_code=403, detail="Неверный код приглашения")
    
    # Проверяем уникальность имени
    c.execute("SELECT id FROM users WHERE name = ?", (user.name,))
    if c.fetchone():
        raise HTTPException(status_code=400, detail="Пользователь с таким именем уже существует")
    
    # Создаём пользователя
    colors = ["#F44336", "#E91E63", "#9C27B0", "#673AB7", "#3F51B5", 
              "#2196F3", "#009688", "#4CAF50", "#FF9800", "#795548"]
    color = colors[len(user.name) % len(colors)]
    
    c.execute("INSERT INTO users (name, avatar_color, invite_code) VALUES (?, ?, ?)",
              (user.name, color, user.invite_code))
    c.execute("UPDATE invites SET used = 1 WHERE code = ?", (user.invite_code,))
    
    conn.commit()
    conn.close()
    return {"status": "ok", "avatar_color": color}

@app.get("/api/users")
def get_users():
    conn = sqlite3.connect("messenger.db")
    c = conn.cursor()
    c.execute("SELECT name, avatar_color FROM users")
    users = [{"name": r[0], "avatar_color": r[1]} for r in c.fetchall()]
    conn.close()
    return {"users": users}

@app.get("/api/messages/{chat_id}")
def get_messages(chat_id: str, limit: int = 50):
    conn = sqlite3.connect("messenger.db")
    c = conn.cursor()
    c.execute("SELECT sender, text, timestamp FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
              (chat_id, limit))
    messages = [{"sender": r[0], "text": r[1], "timestamp": r[2]} for r in c.fetchall()]
    conn.close()
    return {"messages": list(reversed(messages))}

@app.post("/api/invite")
def create_invite(name: str):
    conn = sqlite3.connect("messenger.db")
    c = conn.cursor()
    code = hashlib.md5(str(datetime.datetime.now()).encode()).hexdigest()[:8].upper()
    c.execute("INSERT INTO invites (code, created_by) VALUES (?, ?)", (code, name))
    conn.commit()
    conn.close()
    return {"invite_code": code}

# ========== WebSocket ==========
@app.websocket("/ws/{username}")
async def websocket_endpoint(websocket: WebSocket, username: str):
    await manager.connect(username, websocket)
    try:
        while True:
            data = await websocket.receive_json()
            
            if data["type"] == "message":
                msg = {
                    "sender": username,
                    "chat_id": data["chat_id"],
                    "text": data["text"],
                    "timestamp": datetime.datetime.now().isoformat()
                }
                # Сохраняем в БД
                conn = sqlite3.connect("messenger.db")
                c = conn.cursor()
                c.execute("INSERT INTO messages (sender, chat_id, text, timestamp) VALUES (?, ?, ?, ?)",
                         (msg["sender"], msg["chat_id"], msg["text"], msg["timestamp"]))
                conn.commit()
                conn.close()
                
                # Рассылаем
                msg["type"] = "new_message"
                await manager.broadcast_to_chat(msg, data["chat_id"])
                
            elif data["type"] == "typing":
                await manager.broadcast_to_chat({
                    "type": "typing",
                    "user": username,
                    "chat_id": data["chat_id"],
                    "is_typing": data["is_typing"]
                }, data["chat_id"], exclude=username)
                
    except WebSocketDisconnect:
        manager.disconnect(username)
        await manager.broadcast_status()
    except Exception as e:
        print(f"Error: {e}")
        manager.disconnect(username)
        await manager.broadcast_status()

# ========== СТАТИКА ==========
app.mount("/static", StaticFiles(directory="static", html=True), name="static")

@app.get("/")
def root():
    return FileResponse("static/index.html")

# ========== ЗАПУСК ==========
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)