import os
import asyncio
import socketio
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Нет токена! Укажи переменную окружения BOT_TOKEN")

app = FastAPI()
sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')
socket_app = socketio.ASGIApp(sio, app)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- MODELS ---
class RoundData(BaseModel):
    author_id: int # ID автора (чтобы начислять очки)
    author_name: str # Имя для отображения
    fact1: str
    fact2: str
    fact3: str
    correct: int

# --- СОСТОЯНИЕ ИГРЫ ---
game_state = {
    "players": {},  # {user_id: {"name": str, "score": 0}}
    "round_data": { 
        "author_id": None,
        "author_name": "",
        "facts": {1: "", 2: "", 3: ""},
        "correct": 1
    },
    "votes": {},    # {user_id: option_number}
    "status": "lobby" 
}

# --- ЛОГИКА SOCKET.IO (Синхронизация) ---
@sio.event
async def connect(sid, environ):
    # При обновлении страницы отправляем актуальные данные
    await sio.emit('sync_state', {
        "status": game_state["status"],
        "playerCount": len(game_state["players"]),
        "round_data": {
            "author": game_state["round_data"]["author_name"],
            "facts": game_state["round_data"]["facts"]
        },
        "votes": get_vote_counts() # Чтобы графики не падали при рефреше
    }, to=sid)

def get_vote_counts():
    counts = {1: 0, 2: 0, 3: 0}
    for v in game_state["votes"].values():
        counts[v] += 1
    return counts

# --- ЛОГИКА БОТА ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user = message.from_user
    # Если игрока нет или он сменил имя - обновляем
    if user.id not in game_state["players"]:
        game_state["players"][user.id] = {"name": user.first_name, "score": 0}
        await sio.emit('player_update', {"count": len(game_state["players"])})
    
    await message.answer(f"Привет, {user.first_name}! Ты в игре. Твой текущий счет: {game_state['players'][user.id]['score']}")

@dp.callback_query(F.data.startswith("vote_"))
async def handle_vote(callback: types.CallbackQuery):
    if game_state["status"] != "voting":
        await callback.answer("Голосование закрыто!", show_alert=True)
        return

    choice = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    
    # Автор не может голосовать в своем раунде (опционально)
    if user_id == game_state["round_data"]["author_id"]:
        await callback.answer("Ты автор, тебе нельзя голосовать!", show_alert=True)
        return

    game_state["votes"][user_id] = choice
    await sio.emit('vote_update', get_vote_counts())
    await callback.answer(f"Принято: Факт {choice}")

# --- API ---

@app.get("/api/players")
async def get_players():
    # Возвращаем список игроков для админки
    return [{"id": k, "name": v["name"]} for k, v in game_state["players"].items()]

@app.post("/api/prepare")
async def api_prepare(data: RoundData):
    game_state["round_data"] = {
        "author_id": data.author_id,
        "author_name": data.author_name,
        "facts": {1: data.fact1, 2: data.fact2, 3: data.fact3},
        "correct": data.correct
    }
    game_state["status"] = "presentation"
    game_state["votes"] = {} 

    await sio.emit('state_update', {
        "status": "presentation",
        "author": data.author_name,
        "facts": game_state["round_data"]["facts"]
    })
    return {"ok": True}

@app.post("/api/start_voting")
async def api_start_voting():
    game_state["status"] = "voting"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Факт 1", callback_data="vote_1")],
        [InlineKeyboardButton(text="Факт 2", callback_data="vote_2")],
        [InlineKeyboardButton(text="Факт 3", callback_data="vote_3")]
    ])
    
    for user_id in game_state["players"]:
        # Не отправляем кнопки автору
        if user_id == game_state["round_data"]["author_id"]:
            continue
        try:
            await bot.send_message(user_id, "Голосуй!", reply_markup=kb)
        except:
            pass
            
    await sio.emit('state_update', {"status": "voting"})
    return {"ok": True}

@app.post("/api/reveal")
async def api_reveal():
    correct = game_state["round_data"]["correct"]
    author_id = game_state["round_data"]["author_id"]
    
    # Логика начисления очков
    for voter_id, choice in game_state["votes"].items():
        if choice == correct:
            # Угадал -> получает очко
            game_state["players"][voter_id]["score"] += 1
        else:
            # Не угадал -> очко уходит автору
            if author_id in game_state["players"]:
                game_state["players"][author_id]["score"] += 1

    # Сортировка лидерборда
    leaderboard = sorted(
        [{"name": p["name"], "score": p["score"]} for p in game_state["players"].values()],
        key=lambda x: x["score"], reverse=True
    )
    
    game_state["status"] = "result"
    await sio.emit('round_result', {
        "correct": correct,
        "leaderboard": leaderboard
    })
    return {"ok": True}

@app.post("/api/reset")
async def api_reset():
    game_state["status"] = "lobby"
    await sio.emit('state_update', {"status": "lobby"})
    return {"ok": True}

# --- FRONTEND ROUTING ---
app.mount("/socket.io", socket_app)

@app.get("/")
async def index():
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

@app.get("/admin")
async def admin():
    with open("admin.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

# --- RUN ---
@app.on_event("startup")
async def on_startup():
    asyncio.create_task(dp.start_polling(bot))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)