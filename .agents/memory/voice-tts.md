---
name: Voice STT/TTS setup
description: Correct OpenAI model names and flow for speech-to-text and text-to-speech in the Telegram bot
---

# Voice STT/TTS in Ruslan Helper bot

## Rule
- STT (transcription): `model="whisper-1"`, `response_format="text"`, `language="ru"`. Returns a plain string — check `isinstance(transcript, str)` before calling `.text`.
- TTS (speech synthesis): `openai_client.audio.speech.create(model="tts-1", voice="onyx")`. Stream to BytesIO then send as voice.
- The model name "gpt-4o-mini-transcribe" caused 404s; "gpt-audio-mini" does not exist.

**Why:** These were the actual errors in production; wrong model names caused silent failures on every voice message.

**How to apply:** Any time voice features are touched, verify model names against this file before deploying.

## Key check before using voice
```python
openai_ok = bool(
    os.environ.get("OPENAI_API_KEY") or
    os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY")
)
```
Fail fast with a clear user message if key is missing — do not attempt API calls.
