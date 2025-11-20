import os
import asyncio
import socketio
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
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

# --- MODELS (Данные от админки) ---
class RoundData(BaseModel):
    author: str
    fact1: str
    fact2: str
    fact3: str
    correct: int

# --- СОСТОЯНИЕ ИГРЫ ---
game_state = {
    "players": {},  # {user_id: {"name": str, "score": 0}}
    "round_data": { # Текущие факты
        "author": "",
        "facts": {1: "", 2: "", 3: ""},
        "correct": 1
    },
    "votes": {},    # {user_id: option_number}
    "status": "lobby" # lobby, presentation, voting, result
}

# --- ЛОГИКА БОТА ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user = message.from_user
    if user.id not in game_state["players"]:
        game_state["players"][user.id] = {"name": user.first_name, "score": 0}
        await sio.emit('player_joined', {"name": user.first_name, "count": len(game_state["players"])})
    await message.answer(f"Привет, {user.first_name}! Жди начала раунда.")

@dp.callback_query(F.data.startswith("vote_"))
async def handle_vote(callback: types.CallbackQuery):
    if game_state["status"] != "voting":
        await callback.answer("Голосование закрыто!", show_alert=True)
        return

    choice = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    
    # Защита от переголосования (опционально, сейчас разрешим менять голос)
    game_state["votes"][user_id] = choice
    
    # Считаем статистику для экрана
    vote_counts = {1: 0, 2: 0, 3: 0}
    for v in game_state["votes"].values():
        vote_counts[v] += 1
        
    await sio.emit('vote_update', vote_counts)
    await callback.answer(f"Принято: Факт {choice}")

# --- API ДЛЯ АДМИНКИ ---

@app.post("/api/prepare")
async def api_prepare(data: RoundData):
    # Сохраняем данные раунда
    game_state["round_data"]["author"] = data.author
    game_state["round_data"]["facts"] = {1: data.fact1, 2: data.fact2, 3: data.fact3}
    game_state["round_data"]["correct"] = data.correct
    game_state["status"] = "presentation"
    game_state["votes"] = {} # Сброс голосов прошлого раунда

    # Обновляем экран: показываем тексты
    await sio.emit('state_update', {
        "status": "presentation",
        "author": data.author,
        "facts": game_state["round_data"]["facts"]
    })
    return {"ok": True}

@app.post("/api/start_voting")
async def api_start_voting():
    game_state["status"] = "voting"
    
    # Клавиатура
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Факт 1", callback_data="vote_1")],
        [InlineKeyboardButton(text="Факт 2", callback_data="vote_2")],
        [InlineKeyboardButton(text="Факт 3", callback_data="vote_3")]
    ])
    
    # Рассылка всем игрокам
    count = 0
    for user_id in game_state["players"]:
        try:
            await bot.send_message(user_id, "Голосование открыто! Какой факт - правда?", reply_markup=kb)
            count += 1
        except:
            pass
            
    await sio.emit('state_update', {"status": "voting"}) # Экран меняется на графики
    return {"sent_to": count}

@app.post("/api/reveal")
async def api_reveal():
    correct = game_state["round_data"]["correct"]
    # Подсчет очков
    for uid, choice in game_state["votes"].items():
        if choice == correct:
            game_state["players"][uid]["score"] += 1
    
    # Лидерборд
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

# --- ВЕБ РОУТЫ ---
app.mount("/socket.io", socket_app)

@app.get("/")
async def index():
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

@app.get("/admin")
async def admin():
    with open("admin.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

# --- ЗАПУСК ---
@app.on_event("startup")
async def on_startup():
    asyncio.create_task(dp.start_polling(bot))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)