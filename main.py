import os
import json
import asyncio
import socketio
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Нет токена! Укажи переменную окружения BOT_TOKEN")

DATA_FILE = "data.json"

app = FastAPI()
sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')
socket_app = socketio.ASGIApp(sio, app)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- МЕНЕДЖЕР ДАННЫХ (JSON) ---
class DataManager:
    def __init__(self):
        self.data = {
            "guests": {},   # {guest_id: {name, facts, correct, tg_id, score}} - ЗАГОТОВКИ
            "viewers": {},  # {tg_id: {name, score}} - ПРОСТЫЕ ЗРИТЕЛИ
            "current_round": None
        }
        self.load()

    def load(self):
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                try:
                    loaded = json.load(f)
                    # Мержим, чтобы не потерять структуру если файл старый
                    self.data.update(loaded) 
                except: pass
    
    def save(self):
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def get_all_participants(self):
        # Объединяем гостей (сценарий) и зрителей в один список для лидерборда
        participants = []
        
        # Добавляем Гостей
        for g_id, g in self.data["guests"].items():
            # Показываем гостя в топе, только если он привязан к ТГ или у него есть очки
            participants.append({"id": g_id, "name": g["name"], "score": g.get("score", 0)})
            
        # Добавляем Зрителей
        for v_id, v in self.data["viewers"].items():
            participants.append({"id": v_id, "name": v["name"], "score": v.get("score", 0)})
            
        return sorted(participants, key=lambda x: x["score"], reverse=True)

db = DataManager()
# Состояние игры (временное, для текущего раунда)
round_state = {"votes": {}, "status": "lobby"} 

# --- SOCKET IO ---
@sio.event
async def connect(sid, environ):
    current_rd = db.data.get("current_round")
    await sio.emit('sync_state', {
        "status": round_state["status"],
        "playerCount": len(db.get_all_participants()),
        "round_data": current_rd if round_state["status"] != "lobby" else None,
        "votes": get_vote_counts(),
        "breakdown": get_breakdown() if round_state["status"] == "result" else None
    }, to=sid)

def get_vote_counts():
    counts = {1: 0, 2: 0, 3: 0}
    for v in round_state["votes"].values():
        counts[v] += 1
    return counts

def get_breakdown():
    bd = {1: [], 2: [], 3: []}
    # Собираем имена
    # Нужно искать имя и среди guests, и среди viewers
    for uid, choice in round_state["votes"].items():
        name = "Unknown"
        uid_str = str(uid)
        
        # Ищем в зрителях
        if uid_str in db.data["viewers"]:
            name = db.data["viewers"][uid_str]["name"]
        # Ищем в гостях (по tg_id)
        else:
            for g in db.data["guests"].values():
                if str(g.get("tg_id")) == uid_str:
                    name = g["name"]
                    break
        
        if choice in bd:
            bd[choice].append(name)
    return bd

# --- БОТ: АВТОРИЗАЦИЯ И СВЯЗЫВАНИЕ ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    tg_id = message.from_user.id
    tg_id_str = str(tg_id)
    
    # 1. Проверяем, не привязан ли уже
    if tg_id_str in db.data["viewers"]:
        await message.answer(f"Привет, {db.data['viewers'][tg_id_str]['name']}! Ты зарегистрирован как зритель.")
        return

    for g in db.data["guests"].values():
        if g.get("tg_id") == tg_id:
            await message.answer(f"Привет, {g['name']}! Твой профиль спикера готов.")
            return

    # 2. Если не привязан - предлагаем выбор
    # Ищем свободных гостей (у которых нет tg_id)
    available_guests = []
    for g_id, g in db.data["guests"].items():
        if not g.get("tg_id"):
            available_guests.append(InlineKeyboardButton(text=f"Я — {g['name']}", callback_data=f"link_{g_id}"))
    
    kb_rows = []
    # Разбиваем по 2 кнопки в ряд
    for i in range(0, len(available_guests), 2):
        kb_rows.append(available_guests[i:i+2])
    
    # Кнопка обычного зрителя
    kb_rows.append([InlineKeyboardButton(text="👁 Я просто зритель", callback_data="link_viewer")])
    
    await message.answer(
        f"Привет, {message.from_user.first_name}!\nДавай настроим профиль. Ты есть в этом списке?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)
    )

@dp.callback_query(F.data.startswith("link_"))
async def handle_link(callback: types.CallbackQuery):
    action = callback.data.split("_")[1]
    tg_id = callback.from_user.id
    
    if action == "viewer":
        # Регистрируем как зрителя
        # Используем имя из телеграма или берем ник
        name = callback.from_user.first_name
        db.data["viewers"][str(tg_id)] = {"name": name, "score": 0}
        role = "Зритель"
    else:
        # Привязываем к гостю
        guest_id = action
        if guest_id in db.data["guests"]:
            if db.data["guests"][guest_id].get("tg_id"):
                await callback.answer("Этот профиль уже занят!", show_alert=True)
                return
            db.data["guests"][guest_id]["tg_id"] = tg_id
            role = db.data["guests"][guest_id]["name"]
        else:
            await callback.answer("Ошибка профиля", show_alert=True)
            return
            
    db.save()
    await sio.emit('player_update', {"count": len(db.get_all_participants())})
    
    await callback.message.edit_text(f"Успешно! Ты в игре как: **{role}**")

# --- БОТ: ГОЛОСОВАНИЕ ---
@dp.callback_query(F.data.startswith("vote_"))
async def handle_vote(callback: types.CallbackQuery):
    if round_state["status"] != "voting":
        await callback.answer("Голосование закрыто!", show_alert=True)
        return

    choice = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    
    # Проверка: Автор не голосует за себя
    current_author_tg = None
    cur_round = db.data.get("current_round")
    if cur_round:
        # Ищем tg_id автора
        for g in db.data["guests"].values():
            if g["name"] == cur_round["author"]:
                current_author_tg = g.get("tg_id")
                break
    
    if current_author_tg == user_id:
         await callback.answer("Нельзя голосовать в своём раунде!", show_alert=True)
         return

    round_state["votes"][user_id] = choice
    await sio.emit('vote_update', get_vote_counts())
    await callback.answer(f"Принято: Факт {choice}")

# --- API ДЛЯ АДМИНКИ ---

class GuestModel(BaseModel):
    id: str = None # Если создание нового
    name: str
    fact1: str
    fact2: str
    fact3: str
    correct: int

@app.get("/api/guests")
async def get_guests():
    return db.data["guests"]

@app.post("/api/guests/save")
async def save_guest(guest: GuestModel):
    import uuid
    g_id = guest.id or str(uuid.uuid4())
    
    # Сохраняем, не затирая tg_id и score, если они уже были
    existing = db.data["guests"].get(g_id, {})
    
    db.data["guests"][g_id] = {
        "name": guest.name,
        "facts": {1: guest.fact1, 2: guest.fact2, 3: guest.fact3},
        "correct": guest.correct,
        "tg_id": existing.get("tg_id"), 
        "score": existing.get("score", 0)
    }
    db.save()
    return {"ok": True, "id": g_id}

@app.post("/api/guests/delete/{g_id}")
async def delete_guest(g_id: str):
    if g_id in db.data["guests"]:
        del db.data["guests"][g_id]
        db.save()
    return {"ok": True}

@app.post("/api/prepare_round/{g_id}")
async def prepare_round(g_id: str):
    guest = db.data["guests"].get(g_id)
    if not guest: return {"error": "Not found"}
    
    round_data = {
        "author_id": g_id, # ID из базы гостей
        "author": guest["name"],
        "facts": guest["facts"],
        "correct": guest["correct"]
    }
    
    db.data["current_round"] = round_data
    db.save()
    
    round_state["status"] = "presentation"
    round_state["votes"] = {}
    
    await sio.emit('state_update', {
        "status": "presentation",
        "author": round_data["author"],
        "facts": round_data["facts"]
    })
    return {"ok": True}

@app.post("/api/start_voting")
async def api_start_voting():
    round_state["status"] = "voting"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Факт 1", callback_data="vote_1")],
        [InlineKeyboardButton(text="Факт 2", callback_data="vote_2")],
        [InlineKeyboardButton(text="Факт 3", callback_data="vote_3")]
    ])
    
    # Определяем кого исключить (автора)
    current_author_tg = None
    cur_round = db.data.get("current_round")
    if cur_round:
         guest = db.data["guests"].get(cur_round["author_id"])
         if guest: current_author_tg = guest.get("tg_id")

    # Рассылка
    count = 0
    targets = list(db.data["viewers"].keys()) + [g["tg_id"] for g in db.data["guests"].values() if g.get("tg_id")]
    
    for tg_id in targets:
        if not tg_id: continue
        if tg_id == current_author_tg: continue
        
        try:
            await bot.send_message(tg_id, "Голосование открыто!", reply_markup=kb)
            count += 1
        except: pass
            
    await sio.emit('state_update', {"status": "voting"})
    return {"sent_to": count}

@app.post("/api/reveal")
async def api_reveal():
    if not db.data.get("current_round"): return
    
    correct = db.data["current_round"]["correct"]
    author_g_id = db.data["current_round"]["author_id"]
    
    # Начисление очков
    for uid, choice in round_state["votes"].items():
        uid_str = str(uid)
        is_correct = (choice == correct)
        
        # Если это зритель
        if uid_str in db.data["viewers"]:
            if is_correct: db.data["viewers"][uid_str]["score"] += 1
        # Если это гость
        else:
            for g in db.data["guests"].values():
                if str(g.get("tg_id")) == uid_str:
                    if is_correct: g["score"] = g.get("score", 0) + 1
                    break
    
    # Очки автору за неугадавших (опционально)
    total_votes = len(round_state["votes"])
    correct_votes = list(round_state["votes"].values()).count(correct)
    wrong_votes = total_votes - correct_votes
    
    if author_g_id in db.data["guests"]:
         db.data["guests"][author_g_id]["score"] = db.data["guests"][author_g_id].get("score", 0) + wrong_votes

    db.save() # Сохраняем новые очки

    await sio.emit('round_result', {
        "correct": correct,
        "leaderboard": db.get_all_participants(),
        "breakdown": get_breakdown()
    })
    return {"ok": True}

@app.post("/api/reset")
async def api_reset():
    round_state["status"] = "lobby"
    await sio.emit('state_update', {"status": "lobby"})
    return {"ok": True}

app.mount("/socket.io", socket_app)

@app.get("/")
async def index():
    with open("index.html", "r", encoding="utf-8") as f: return HTMLResponse(f.read())

@app.get("/admin")
async def admin():
    with open("admin.html", "r", encoding="utf-8") as f: return HTMLResponse(f.read())

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(dp.start_polling(bot))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)