@echo off
chcp 65001 >nul
title Ruslan Helper Bot

REM Переходим в папку, где лежит этот батник
cd /d "%~dp0"

REM Проверяем Python
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python не найден. Установи Python 3.11+ с python.org
    pause
    exit /b 1
)

REM Создаём venv при первом запуске
if not exist ".venv\Scripts\python.exe" (
    echo [setup] Создаю виртуальное окружение...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Не удалось создать venv
        pause
        exit /b 1
    )
    echo [setup] Ставлю зависимости (это займёт минуту)...
    .venv\Scripts\python -m pip install --upgrade pip
    .venv\Scripts\pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Не удалось поставить зависимости
        pause
        exit /b 1
    )
)

REM Проверка .env
if not exist ".env" (
    echo [!!] Файл .env не найден. Скопируй .env.example в .env и заполни TELEGRAM_BOT_TOKEN.
    pause
    exit /b 1
)

REM Старт бота
echo [run] Запускаю бота...
.venv\Scripts\python -u bot.py
echo.
echo [stop] Бот остановлен.
pause
