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

TELEGRAM_BOT_TOKEN = "8650988930:AAHk5gbchTiE5_zhgR0l5mE6yEGA5CxVUGI"
ADMIN_TELEGRAM_ID = 1637635130

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs("avatars", exist_ok=True)
os.makedirs("voice", exist_ok=True)

def init_db():
    conn = sqlite3.connect("messenger.db", timeout=10)
    c = conn.cursor()
    
    c.execute("CREATE TABLE IF NOT EXISTS pending_users (id INTEGER PRIMARY KEY AUTOINCREMENT,first_name TEXT,last_name TEXT,password TEXT,phone TEXT,status TEXT DEFAULT 'pending',created_at TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT,first_name TEXT,last_name TEXT,password TEXT,phone TEXT,avatar_url TEXT,bio TEXT,theme TEXT DEFAULT 'dark',wallpaper_url TEXT,custom_theme TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS chats (id TEXT PRIMARY KEY,name TEXT,is_group INTEGER DEFAULT 0,created_by INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS chat_members (chat_id TEXT,user_id INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id TEXT,sender_id INTEGER,sender_name TEXT,text TEXT,file_url TEXT,file_type TEXT,timestamp TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS posts (id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,text TEXT,file_url TEXT,file_type TEXT,timestamp TEXT)")
    
    conn.commit()
    conn.close()

init_db()

async def send_telegram_message(text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": ADMIN_TELEGRAM_ID, "text": text, "parse_mode": "HTML"}
    if reply_markup: payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient() as client: await client.post(url, json=payload)

async def notify_admin_new_request(user_id, first_name, last_name, phone):
    text = f"🆕 <b>Новая заявка!</b>\n\n👤 {first_name} {last_name}\n📱 {phone}\n🆔 {user_id}"
    keyboard = {"inline_keyboard": [[{"text": "✅ Одобрить", "callback_data": f"approve_{user_id}"},{"text": "❌ Отклонить", "callback_data": f"reject_{user_id}"}]]}
    await send_telegram_message(text, json.dumps(keyboard))

class PendingUser(BaseModel):
    first_name: str; last_name: str; password: str; phone: str

class LoginRequest(BaseModel):
    user_id: int; password: str

class CreateChat(BaseModel):
    name: str; user_ids: List[int]

class ThemeUpdate(BaseModel):
    theme: Optional[str] = None; wallpaper_url: Optional[str] = None; custom_theme: Optional[str] = None; bio: Optional[str] = None

class PostCreate(BaseModel):
    text: Optional[str] = None; file_url: Optional[str] = None; file_type: Optional[str] = None

class ConnectionManager:
    def __init__(self): self.active: Dict[int, WebSocket] = {}
    async def connect(self, uid, ws): await ws.accept(); self.active[uid] = ws
    def disconnect(self, uid):
        if uid in self.active: del self.active[uid]
    async def broadcast_to_chat(self, msg, chat_id, sender_id=None):
        conn = sqlite3.connect("messenger.db", timeout=10)
        c = conn.cursor(); c.execute("SELECT user_id FROM chat_members WHERE chat_id=?", (chat_id,))
        members = [r[0] for r in c.fetchall()]; conn.close()
        for uid in members:
            if uid != sender_id and uid in self.active: await self.active[uid].send_json(msg)

manager = ConnectionManager()

@app.post("/api/apply")
async def apply(user: PendingUser):
    conn = sqlite3.connect("messenger.db", timeout=10)
    c = conn.cursor()
    c.execute("INSERT INTO pending_users (first_name,last_name,password,phone,created_at) VALUES (?,?,?,?,?)",(user.first_name,user.last_name,user.password,user.phone,datetime.datetime.now().isoformat()))
    conn.commit(); uid = c.lastrowid; conn.close()
    await notify_admin_new_request(uid, user.first_name, user.last_name, user.phone)
    return {"status":"pending","user_id":uid}

@app.post("/api/login")
def login(data: LoginRequest):
    conn = sqlite3.connect("messenger.db", timeout=10)
    c = conn.cursor()
    c.execute("SELECT id,first_name,last_name,avatar_url,theme,wallpaper_url,custom_theme,phone,bio FROM users WHERE id=? AND password=?",(data.user_id,data.password))
    user = c.fetchone(); conn.close()
    if user: return {"status":"ok","user":{"id":user[0],"first_name":user[1],"last_name":user[2],"avatar_url":user[3],"theme":user[4],"wallpaper_url":user[5],"custom_theme":user[6],"phone":user[7],"bio":user[8]}}
    raise HTTPException(status_code=403, detail="Неверный ID или пароль")

@app.get("/api/check_status/{user_id}")
def check_status(user_id: int):
    conn = sqlite3.connect("messenger.db", timeout=10); c = conn.cursor()
    c.execute("SELECT status FROM pending_users WHERE id=?",(user_id,)); row = c.fetchone()
    if row:
        if row[0]=="approved":
            c.execute("SELECT id,first_name,last_name,avatar_url,theme,wallpaper_url,custom_theme,phone,bio FROM users WHERE id=?",(user_id,)); u=c.fetchone(); conn.close()
            if u: return {"status":"approved","user":{"id":u[0],"first_name":u[1],"last_name":u[2],"avatar_url":u[3],"theme":u[4],"wallpaper_url":u[5],"custom_theme":u[6],"phone":u[7],"bio":u[8]}}
        conn.close(); return {"status":row[0]}
    c.execute("SELECT id,first_name,last_name,avatar_url,theme,wallpaper_url,custom_theme,phone,bio FROM users WHERE id=?",(user_id,)); u=c.fetchone(); conn.close()
    if u: return {"status":"approved","user":{"id":u[0],"first_name":u[1],"last_name":u[2],"avatar_url":u[3],"theme":u[4],"wallpaper_url":u[5],"custom_theme":u[6],"phone":u[7],"bio":u[8]}}
    return {"status":"unknown"}

@app.post("/api/approve/{user_id}")
def approve_user(user_id: int):
    conn = sqlite3.connect("messenger.db", timeout=10); c = conn.cursor()
    c.execute("SELECT * FROM pending_users WHERE id=? AND status='pending'",(user_id,)); user = c.fetchone()
    if not user: conn.close(); raise HTTPException(status_code=404)
    c.execute("UPDATE pending_users SET status='approved' WHERE id=?",(user_id,))
    c.execute("INSERT INTO users (id,first_name,last_name,password,phone) VALUES (?,?,?,?,?)",(user[0],user[1],user[2],user[3],user[4]))
    chat_id = str(uuid.uuid4())[:8]
    c.execute("INSERT INTO chats (id,name,is_group,created_by) VALUES (?,?,0,?)",(chat_id,f"{user[1]} {user[2]}",ADMIN_TELEGRAM_ID))
    c.execute("INSERT OR IGNORE INTO chat_members (chat_id,user_id) VALUES (?,?)",(chat_id,ADMIN_TELEGRAM_ID))
    c.execute("INSERT OR IGNORE INTO chat_members (chat_id,user_id) VALUES (?,?)",(chat_id,user_id))
    conn.commit(); conn.close()
    return {"status":"ok"}

@app.post("/api/reject/{user_id}")
def reject_user(user_id: int):
    conn = sqlite3.connect("messenger.db", timeout=10); c = conn.cursor()
    c.execute("UPDATE pending_users SET status='rejected' WHERE id=? AND status='pending'",(user_id,)); conn.commit(); conn.close()
    return {"status":"ok"}

@app.get("/api/users")
def get_users():
    conn = sqlite3.connect("messenger.db", timeout=10); c = conn.cursor()
    c.execute("SELECT id,first_name,last_name,avatar_url,phone,bio FROM users")
    users = [{"id":r[0],"first_name":r[1],"last_name":r[2],"avatar_url":r[3],"phone":r[4],"bio":r[5]} for r in c.fetchall()]; conn.close()
    return {"users":users}

@app.get("/api/user/{user_id}")
def get_user(user_id: int):
    conn = sqlite3.connect("messenger.db", timeout=10); c = conn.cursor()
    c.execute("SELECT id,first_name,last_name,avatar_url,phone,bio FROM users WHERE id=?",(user_id,)); u=c.fetchone(); conn.close()
    if u: return {"id":u[0],"first_name":u[1],"last_name":u[2],"avatar_url":u[3],"phone":u[4],"bio":u[5]}
    raise HTTPException(status_code=404)

@app.post("/api/update_profile/{user_id}")
def update_profile(user_id: int, data: ThemeUpdate):
    conn = sqlite3.connect("messenger.db", timeout=10); c = conn.cursor()
    if data.theme: c.execute("UPDATE users SET theme=? WHERE id=?",(data.theme,user_id))
    if data.wallpaper_url is not None: c.execute("UPDATE users SET wallpaper_url=? WHERE id=?",(data.wallpaper_url,user_id))
    if data.custom_theme: c.execute("UPDATE users SET custom_theme=? WHERE id=?",(data.custom_theme,user_id))
    if data.bio is not None: c.execute("UPDATE users SET bio=? WHERE id=?",(data.bio,user_id))
    conn.commit(); conn.close()
    return {"status":"ok"}

@app.post("/api/upload_avatar/{user_id}")
async def upload_avatar(user_id: int, file: UploadFile = File(...)):
    ext = file.filename.split(".")[-1] if "." in file.filename else "jpg"
    filename = f"avatar_{user_id}.{ext}"; filepath = os.path.join("avatars", filename)
    with open(filepath, "wb") as f: f.write(await file.read())
    url = f"/avatars/{filename}"
    conn = sqlite3.connect("messenger.db", timeout=10); c = conn.cursor()
    c.execute("UPDATE users SET avatar_url=? WHERE id=?",(url,user_id)); conn.commit(); conn.close()
    return {"url":url}

@app.post("/api/create_chat")
def create_chat(data: CreateChat):
    chat_id = str(uuid.uuid4())[:8]
    conn = sqlite3.connect("messenger.db", timeout=10); c = conn.cursor()
    c.execute("INSERT INTO chats (id,name,is_group,created_by) VALUES (?,?,?,?)",(chat_id,data.name,1 if len(data.user_ids)>2 else 0,data.user_ids[0]))
    for uid in data.user_ids: c.execute("INSERT OR IGNORE INTO chat_members (chat_id,user_id) VALUES (?,?)",(chat_id,uid))
    conn.commit(); conn.close()
    return {"chat_id":chat_id,"name":data.name}

@app.get("/api/my_chats/{user_id}")
def my_chats(user_id: int):
    conn = sqlite3.connect("messenger.db", timeout=10); c = conn.cursor()
    c.execute("SELECT c.id,c.name,c.is_group FROM chats c JOIN chat_members cm ON c.id=cm.chat_id WHERE cm.user_id=?",(user_id,)); chats=[{"id":r[0],"name":r[1],"is_group":r[2]} for r in c.fetchall()]; conn.close()
    return {"chats":chats}

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    ext = file.filename.split(".")[-1] if "." in file.filename else "file"; filename = f"{uuid.uuid4()}.{ext}"; filepath = os.path.join(UPLOAD_DIR, filename)
    with open(filepath, "wb") as f: f.write(await file.read())
    file_type = "image" if ext.lower() in ["jpg","jpeg","png","gif","webp"] else "video" if ext.lower() in ["mp4","mov","webm"] else "voice" if ext.lower() in ["mp3","wav","ogg","webm","m4a"] else "file"
    return {"url":f"/uploads/{filename}","type":file_type}

@app.post("/api/upload_voice")
async def upload_voice(file: UploadFile = File(...)):
    filename = f"{uuid.uuid4()}.webm"; filepath = os.path.join("voice", filename)
    with open(filepath, "wb") as f: f.write(await file.read())
    return {"url":f"/voice/{filename}","type":"voice"}

@app.get("/api/messages/{chat_id}")
def get_messages(chat_id: str, limit: int = 50):
    conn = sqlite3.connect("messenger.db", timeout=10); c = conn.cursor()
    c.execute("SELECT sender_id,sender_name,text,file_url,file_type,timestamp FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?",(chat_id,limit)); msgs=[{"sender_id":r[0],"sender_name":r[1],"text":r[2],"file_url":r[3],"file_type":r[4],"timestamp":r[5]} for r in c.fetchall()]; conn.close()
    return {"messages":list(reversed(msgs))}

@app.get("/api/pending")
def get_pending():
    conn = sqlite3.connect("messenger.db", timeout=10); c = conn.cursor()
    c.execute("SELECT id,first_name,last_name,phone,created_at FROM pending_users WHERE status='pending'"); pending=[{"id":r[0],"first_name":r[1],"last_name":r[2],"phone":r[3],"created_at":r[4]} for r in c.fetchall()]; conn.close()
    return {"pending":pending}

# Посты (статусы)
@app.post("/api/create_post/{user_id}")
def create_post(user_id: int, data: PostCreate):
    conn = sqlite3.connect("messenger.db", timeout=10); c = conn.cursor()
    c.execute("INSERT INTO posts (user_id,text,file_url,file_type,timestamp) VALUES (?,?,?,?,?)",(user_id,data.text,data.file_url,data.file_type,datetime.datetime.now().isoformat())); conn.commit(); conn.close()
    return {"status":"ok"}

@app.get("/api/posts/{user_id}")
def get_posts(user_id: int):
    conn = sqlite3.connect("messenger.db", timeout=10); c = conn.cursor()
    c.execute("SELECT text,file_url,file_type,timestamp FROM posts WHERE user_id=? ORDER BY id DESC LIMIT 20",(user_id,)); posts=[{"text":r[0],"file_url":r[1],"file_type":r[2],"timestamp":r[3]} for r in c.fetchall()]; conn.close()
    return {"posts":posts}

# Telegram Webhook
@app.post("/api/telegram-webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    if "callback_query" in data:
        cb = data["callback_query"]; d = cb["data"]; cid = cb["message"]["chat"]["id"]; mid = cb["message"]["message_id"]
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
        if d.startswith("approve_"):
            uid = int(d.split("_")[1])
            try: approve_user(uid); await httpx.AsyncClient().post(url, json={"chat_id":cid,"message_id":mid,"text":f"✅ Заявка #{uid} одобрена!"})
            except: await httpx.AsyncClient().post(url, json={"chat_id":cid,"message_id":mid,"text":"❌ Ошибка"})
        elif d.startswith("reject_"):
            uid = int(d.split("_")[1]); reject_user(uid)
            await httpx.AsyncClient().post(url, json={"chat_id":cid,"message_id":mid,"text":f"❌ Заявка #{uid} отклонена"})
    return {"status":"ok"}

@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: int):
    await manager.connect(user_id, websocket)
    try:
        while True:
            data = await websocket.receive_json()
            if data["type"] == "message":
                msg = {"type":"new_message","chat_id":data["chat_id"],"sender_id":user_id,"sender_name":data["sender_name"],"text":data.get("text",""),"file_url":data.get("file_url"),"file_type":data.get("file_type"),"timestamp":datetime.datetime.now().isoformat()}
                conn = sqlite3.connect("messenger.db", timeout=10); c = conn.cursor()
                c.execute("INSERT INTO messages (chat_id,sender_id,sender_name,text,file_url,file_type,timestamp) VALUES (?,?,?,?,?,?,?)",(msg["chat_id"],user_id,msg["sender_name"],msg["text"],msg["file_url"],msg["file_type"],msg["timestamp"])); conn.commit(); conn.close()
                await manager.broadcast_to_chat(msg, data["chat_id"], user_id)
    except WebSocketDisconnect: manager.disconnect(user_id)

app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")
app.mount("/avatars", StaticFiles(directory="avatars"), name="avatars")
app.mount("/voice", StaticFiles(directory="voice"), name="voice")
app.mount("/static", StaticFiles(directory="static", html=True), name="static")

@app.get("/")
def root(): return FileResponse("static/index.html")

@app.on_event("startup")
async def set_webhook():
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "https://nora-uugb.onrender.com")
    async with httpx.AsyncClient() as client: await client.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook", json={"url":f"{render_url}/api/telegram-webhook"})

if __name__ == "__main__":
    import uvicorn; uvicorn.run(app, host="0.0.0.0", port=8000)