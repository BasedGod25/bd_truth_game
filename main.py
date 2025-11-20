import os
import asyncio
import socketio
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
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

# --- СОСТОЯНИЕ ИГРЫ (В памяти) ---
game_state = {
    "players": {},  # {user_id: {"name": str, "score": 0}}
    "current_author_id": None, # Кто сейчас загадал факты
    "correct_option": 1,       # Какой факт верный (1, 2 или 3)
    "votes": {},    # {user_id: option_number}
    "status": "lobby" # lobby, voting, result
}

# --- ЛОГИКА БОТА (Aiogram) ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user = message.from_user
    # Регистрируем игрока
    if user.id not in game_state["players"]:
        game_state["players"][user.id] = {"name": user.first_name, "score": 0}
        # Обновляем экран лобби
        await sio.emit('player_joined', {"name": user.first_name, "count": len(game_state["players"])})
    
    await message.answer(f"Привет, {user.first_name}! Ты в игре. Смотри на большой экран.")

# Команда для АДМИНА/ВЕДУЩЕГО: Начать раунд голосования
# Пример: /round 1 (где 1 - это номер правильного факта)
@dp.message(Command("round"))
async def cmd_round(message: types.Message):
    try:
        correct_opt = int(message.text.split()[1])
    except:
        correct_opt = 1 # По дефолту первый
    
    game_state["correct_option"] = correct_opt
    game_state["votes"] = {} # Сброс голосов
    game_state["status"] = "voting"
    
    # Клавиатура для голосования
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Факт №1", callback_data="vote_1")],
        [InlineKeyboardButton(text="Факт №2", callback_data="vote_2")],
        [InlineKeyboardButton(text="Факт №3", callback_data="vote_3")]
    ])
    
    # Рассылаем всем игрокам (в реальном проекте лучше отправлять только активным)
    for user_id in game_state["players"]:
        try:
            await bot.send_message(user_id, "Какой факт - ПРАВДА? Голосуй!", reply_markup=kb)
        except:
            pass # Если юзер заблокировал бота

    await sio.emit('state_update', {"status": "voting"})
    await message.answer("Раунд начался! Кнопки отправлены.")

@dp.callback_query(F.data.startswith("vote_"))
async def handle_vote(callback: types.CallbackQuery):
    if game_state["status"] != "voting":
        await callback.answer("Голосование закрыто!", show_alert=True)
        return

    choice = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    game_state["votes"][user_id] = choice
    
    # Отправляем на экран инфо, что проголосовал еще один человек (без спойлера кто)
    vote_counts = {1: 0, 2: 0, 3: 0}
    for v in game_state["votes"].values():
        vote_counts[v] += 1
        
    await sio.emit('vote_update', vote_counts)
    await callback.answer("Голос принят!")
    await callback.message.edit_text(f"Ты выбрал Факт №{choice}. Ждем остальных...")

# Команда для завершения раунда и подсчета
@dp.message(Command("reveal"))
async def cmd_reveal(message: types.Message):
    correct = game_state["correct_option"]
    author_points = 0
    
    # Подсчет очков
    for uid, choice in game_state["votes"].items():
        if choice == correct:
            # Угадал - получает балл
            game_state["players"][uid]["score"] += 1
        else:
            # Не угадал - балл уходит автору (условно, пока без конкретного автора)
            author_points += 1
            
    # Обновляем лидерборд
    leaderboard = sorted(
        [{"name": p["name"], "score": p["score"]} for p in game_state["players"].values()],
        key=lambda x: x["score"], reverse=True
    )
    
    await sio.emit('round_result', {
        "correct": correct,
        "leaderboard": leaderboard
    })
    await message.answer(f"Раунд завершен. Правильный ответ: {correct}")


# --- ВЕБ-СЕРВЕР (FastAPI) ---

app.mount("/socket.io", socket_app)

@app.get("/")
async def index():
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

# --- ЗАПУСК ---
# Запускаем и веб-сервер, и поллинг бота
@app.on_event("startup")
async def on_startup():
    asyncio.create_task(dp.start_polling(bot))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
