import os
import requests

XAI_API_KEY = os.environ.get("XAI_API_KEY", "")
XAI_API_URL = "https://api.x.ai/v1/chat/completions"

SYSTEM_PROMPT = """Ты личный помощник Руслана — умный, краткий, по делу.
Отвечай на русском языке. Будь полезным, конкретным и дружелюбным.
Не лей воду — только суть. Если вопрос деловой — отвечай профессионально."""


def ask_grok(user_message: str, history: list = None) -> str:
    """
    Отправляет сообщение в Grok и возвращает ответ.
    history — список предыдущих сообщений [{"role": "user"/"assistant", "content": "..."}]
    """
    if not XAI_API_KEY:
        return "❌ Grok не настроен — нет API ключа."

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history:
        messages.extend(history[-10:])  # берём последние 10 сообщений контекста
    messages.append({"role": "user", "content": user_message})

    try:
        resp = requests.post(
            XAI_API_URL,
            headers={
                "Authorization": f"Bearer {XAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "grok-3",
                "messages": messages,
                "max_tokens": 1024,
                "temperature": 0.7,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except requests.exceptions.Timeout:
        return "⏳ Grok не ответил вовремя. Попробуй ещё раз."
    except requests.exceptions.HTTPError as e:
        if resp.status_code == 401:
            return "❌ Неверный API ключ Grok."
        if resp.status_code == 429:
            return "⏳ Лимит запросов к Grok. Попробуй через минуту."
        return f"❌ Ошибка Grok ({resp.status_code}): {resp.text[:200]}"
    except Exception as e:
        return f"❌ Ошибка: {str(e)[:200]}"
