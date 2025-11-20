import os
import asyncio
import socketio
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

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
    author_id: int
    author_name: str
    fact1: str
    fact2: str
    fact3: str
    correct: int

# --- FSM (СОСТОЯНИЯ ДИАЛОГА) ---
class Registration(StatesGroup):
    waiting_for_nickname = State()

# --- СОСТОЯНИЕ ИГРЫ ---
game_state = {
    "players": {},  # {user_id: {"name": str, "score": 0}}
    "round_data": { 
        "author_id": None,
        "author_name": "",
        "facts": {1: "", 2: "", 3: ""},
        "correct": 1
    },
    "votes": {},
    "status": "lobby" 
}

# --- SOCKET.IO (ЭКРАН) ---
@sio.event
async def connect(sid, environ):
    # При подключении экрана отправляем актуальные данные
    await sio.emit('sync_state', {
        "status": game_state["status"],
        "playerCount": len(game_state["players"]),
        "round_data": {
            "author": game_state["round_data"]["author_name"],
            "facts": game_state["round_data"]["facts"]
        },
        "votes": get_vote_counts()
    }, to=sid)

def get_vote_counts():
    counts = {1: 0, 2: 0, 3: 0}
    for v in game_state["votes"].values():
        counts[v] += 1
    return counts

# --- ЛОГИКА БОТА ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    # Запускаем сценарий регистрации
    await message.answer("Привет! Добро пожаловать в 'Игру в Правду'.\n\nКак тебя называть? Введи свой никнейм:")
    await state.set_state(Registration.waiting_for_nickname)

@dp.message(Registration.waiting_for_nickname)
async def process_nickname(message: types.Message, state: FSMContext):
    nickname = message.text.strip()
    
    # Простая валидация
    if len(nickname) > 20:
        await message.answer("Слишком длинное имя! Попробуй короче.")
        return
    
    user_id = message.from_user.id
    
    # Если игрока не было - создаем, если был - обновляем имя, но сохраняем очки
    current_score = 0
    if user_id in game_state["players"]:
        current_score = game_state["players"][user_id]["score"]

    game_state["players"][user_id] = {"name": nickname, "score": current_score}
    
    await message.answer(f"Отлично, {nickname}! Ты в игре. Смотри на экран.")
    await state.clear() # Выход из режима ожидания имени
    
    # ВАЖНО: Обновляем счетчик на экране мгновенно
    await sio.emit('player_update', {"count": len(game_state["players"])})


@dp.callback_query(F.data.startswith("vote_"))
async def handle_vote(callback: types.CallbackQuery):
    if game_state["status"] != "voting":
        await callback.answer("Голосование закрыто!", show_alert=True)
        return

    choice = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    
    # Проверка, зарегистрирован ли игрок (на всякий случай)
    if user_id not in game_state["players"]:
        await callback.answer("Сначала нажми /start и введи имя!", show_alert=True)
        return

    if user_id == game_state["round_data"]["author_id"]:
        await callback.answer("Ты автор, тебе нельзя голосовать!", show_alert=True)
        return

    game_state["votes"][user_id] = choice
    await sio.emit('vote_update', get_vote_counts())
    await callback.answer(f"Принято: Факт {choice}")

# --- API ---

@app.get("/api/players")
async def get_players():
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
    print("--- [DEBUG] Попытка запуска голосования ---")
    game_state["status"] = "voting"
    
    # 1. Проверяем, есть ли вообще игроки
    players_count = len(game_state["players"])
    print(f"--- [DEBUG] Игроков в базе памяти: {players_count}")
    
    if players_count == 0:
        print("--- [ERROR] Список игроков пуст! Никто не зарегистрировался после перезагрузки.")
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Факт 1", callback_data="vote_1")],
        [InlineKeyboardButton(text="Факт 2", callback_data="vote_2")],
        [InlineKeyboardButton(text="Факт 3", callback_data="vote_3")]
    ])
    
    sent_count = 0
    author_id = game_state["round_data"]["author_id"]

    for user_id in game_state["players"]:
        # Приводим к int на всякий случай
        try:
            uid = int(user_id)
        except:
            continue

        # Пропускаем автора
        if uid == author_id:
            print(f"--- [DEBUG] Пропуск автора (ID: {uid})")
            continue
            
        try:
            await bot.send_message(uid, "Голосование открыто! Какой факт - правда?", reply_markup=kb)
            sent_count += 1
            print(f"--- [SUCCESS] Отправлено юзеру {uid}")
        except Exception as e:
            # ТЕПЕРЬ МЫ УВИДИМ ОШИБКУ В КОНСОЛИ
            print(f"--- [ERROR] Не удалось отправить юзеру {uid}: {e}")
            
    await sio.emit('state_update', {"status": "voting"})
    print(f"--- [RESULT] Итог: отправлено {sent_count} сообщений")
    return {"ok": True, "sent_to": sent_count, "total_players": players_count}
    
    game_state["status"] = "voting"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Факт 1", callback_data="vote_1")],
        [InlineKeyboardButton(text="Факт 2", callback_data="vote_2")],
        [InlineKeyboardButton(text="Факт 3", callback_data="vote_3")]
    ])
    
    count = 0
    for user_id in game_state["players"]:
        if user_id == game_state["round_data"]["author_id"]:
            continue
        try:
            await bot.send_message(user_id, "Голосуй!", reply_markup=kb)
            count += 1
        except:
            pass
            
    await sio.emit('state_update', {"status": "voting"})
    return {"sent_to": count}

@app.post("/api/reveal")
async def api_reveal():
    correct = game_state["round_data"]["correct"]
    author_id = game_state["round_data"]["author_id"]
    
    for voter_id, choice in game_state["votes"].items():
        if choice == correct:
            game_state["players"][voter_id]["score"] += 1
        else:
            if author_id in game_state["players"]:
                game_state["players"][author_id]["score"] += 1

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

# --- RUN ---
app.mount("/socket.io", socket_app)

@app.get("/")
async def index():
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

@app.get("/admin")
async def admin():
    with open("admin.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(dp.start_polling(bot))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)