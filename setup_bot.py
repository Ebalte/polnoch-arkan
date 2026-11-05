"""Однократная настройка профиля бота: описание, короткое описание, команды.
Запуск: python setup_bot.py  (токен берётся из .env или переменной BOT_TOKEN)"""
import json
import os
import urllib.request
from pathlib import Path

env = Path(__file__).with_name(".env")
if env.exists():
    for line in env.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
TOKEN = os.environ["BOT_TOKEN"]


def call(method: str, **params) -> None:
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/{method}",
                                 data=json.dumps(params).encode(), headers={"Content-Type": "application/json"})
    res = json.load(urllib.request.urlopen(req))
    print(method, "ok" if res.get("ok") else res)


call("setMyDescription", description=(
    "🌙 Полночный Аркан — Таро прямо в Telegram.\n\n"
    "🔮 Карта дня, «Да или Нет», расклады на любовь, работу и судьбу, Кельтский крест.\n"
    "🃏 Настоящая колода Райдера–Уэйта, 78 карт с толкованиями.\n"
    "🎁 3 расклада каждый день — бесплатно.\n\n"
    "Нажми «Старт» и задай свой вопрос картам ✨"))
call("setMyShortDescription", short_description="🌙 Таро в Telegram: карта дня и расклады на любовь, работу и судьбу. 3 расклада в день бесплатно.")
call("setMyCommands", commands=[
    {"command": "start", "description": "Сделать расклад"},
    {"command": "daily", "description": "Карта дня: on / off"},
    {"command": "paysupport", "description": "Помощь с оплатой"},
    {"command": "terms", "description": "Условия"},
])
