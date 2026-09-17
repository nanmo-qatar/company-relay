"""
Company announcement relay -- instant Telegram-to-Telegram translation.
Powered by Sarvam AI (purpose-built for Indian languages).
------------------------------------------------------------------------
The boss posts in English in one "source" channel. This webhook fires
the moment that happens, translates the message into Hindi, Urdu,
Nepali, and Bengali, and posts each version to its own dedicated
channel -- instantly, no polling.

Uses Sarvam's sarvam-translate:v1 model, which covers all 22 scheduled
Indian languages (mayura:v1 does NOT support Nepali or Urdu).

Required environment variables:
  TELEGRAM_BOT_TOKEN     - token for THIS dedicated bot (from @BotFather)
  SOURCE_CHANNEL_ID      - the boss's channel ("@name" or numeric -100... ID)
  SARVAM_API_KEY         - your Sarvam API subscription key
  TELEGRAM_CHAT_ID_HI    - Hindi channel
  TELEGRAM_CHAT_ID_UR    - Urdu channel
  TELEGRAM_CHAT_ID_NE    - Nepali channel
  TELEGRAM_CHAT_ID_BN    - Bengali channel
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

SARVAM_URL = "https://api.sarvam.ai/translate"
SARVAM_MODEL = "sarvam-translate:v1"  # covers all 22 languages incl. Nepali + Urdu
SOURCE_LANG = "en-IN"
MAX_CHARS = 1900  # Sarvam caps at 2000 per request; leave a little headroom

# Sarvam's documented language codes -- no guessing needed.
TARGET_LANGS = {
    "hi": "hi-IN",  # Hindi
    "ur": "ur-IN",  # Urdu
    "ne": "ne-IN",  # Nepali
    "bn": "bn-IN",  # Bengali
}


def target_channels() -> dict:
    return {
        "hi": os.environ["TELEGRAM_CHAT_ID_HI"],
        "ur": os.environ["TELEGRAM_CHAT_ID_UR"],
        "ne": os.environ["TELEGRAM_CHAT_ID_NE"],
        "bn": os.environ["TELEGRAM_CHAT_ID_BN"],
    }


def _chunk(text: str, size: int = MAX_CHARS) -> list[str]:
    """Split long text on paragraph boundaries where possible, so each
    piece fits under Sarvam's per-request character limit."""
    if len(text) <= size:
        return [text]
    chunks, current = [], ""
    for para in text.split("\n"):
        if len(current) + len(para) + 1 <= size:
            current = f"{current}\n{para}" if current else para
        else:
            if current:
                chunks.append(current)
            while len(para) > size:  # a single giant paragraph -- hard split
                chunks.append(para[:size])
                para = para[size:]
            current = para
    if current:
        chunks.append(current)
    return chunks


def translate(text: str, lang_key: str) -> str:
    """Translate English text into the given language via Sarvam."""
    target = TARGET_LANGS[lang_key]
    pieces = []
    for chunk in _chunk(text):
        resp = requests.post(
            SARVAM_URL,
            headers={
                "api-subscription-key": os.environ["SARVAM_API_KEY"],
                "Content-Type": "application/json",
            },
            json={
                "input": chunk,
                "source_language_code": SOURCE_LANG,
                "target_language_code": target,
                "model": SARVAM_MODEL,
                "mode": "formal",
            },
            timeout=30,
        )
        resp.raise_for_status()
        pieces.append(resp.json()["translated_text"])
    return "\n".join(pieces)


def tg_call(method: str, payload: dict) -> dict:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=15
    )
    resp.raise_for_status()
    return resp.json()


def extract_media(post: dict) -> list[dict]:
    """Telegram file_ids work across chats for the same bot -- no need
    to fetch a public URL, just reuse the file_id directly."""
    media = []
    if "photo" in post:
        largest = post["photo"][-1]  # last entry = highest resolution
        media.append({"type": "photo", "file_id": largest["file_id"]})
    if "video" in post:
        media.append({"type": "video", "file_id": post["video"]["file_id"]})
    return media


def send_to_channel(chat_id: str, text: str, media: list[dict]) -> None:
    if media and len(text) <= 1024:
        first, rest = media[0], media[1:]
        method = "sendVideo" if first["type"] == "video" else "sendPhoto"
        field = "video" if first["type"] == "video" else "photo"
        try:
            tg_call(method, {"chat_id": chat_id, field: first["file_id"], "caption": text})
        except Exception as e:
            print(f"Primary media send failed, falling back to text: {e}", file=sys.stderr)
            tg_call("sendMessage", {"chat_id": chat_id, "text": text[:4096]})
        for item in rest:
            m = "sendVideo" if item["type"] == "video" else "sendPhoto"
            f = "video" if item["type"] == "video" else "photo"
            try:
                tg_call(m, {"chat_id": chat_id, f: item["file_id"]})
            except Exception as e:
                print(f"Extra media failed, skipping: {e}", file=sys.stderr)
    else:
        tg_call("sendMessage", {"chat_id": chat_id, "text": text[:4096]})
        for item in media:
            m = "sendVideo" if item["type"] == "video" else "sendPhoto"
            f = "video" if item["type"] == "video" else "photo"
            try:
                tg_call(m, {"chat_id": chat_id, f: item["file_id"]})
            except Exception as e:
                print(f"Extra media failed, skipping: {e}", file=sys.stderr)


def process_update(update: dict) -> None:
    post = update.get("channel_post")
    if not post:
        return  # not a channel post (e.g. a private DM to the bot) -- ignore

    posted_in = str(post["chat"]["id"])
    source = os.environ["SOURCE_CHANNEL_ID"].lstrip("@")
    if posted_in != source and posted_in != f"@{source}":
        return  # a post from some other channel this bot admins -- ignore

    text = post.get("text") or post.get("caption") or ""
    if not text.strip():
        return  # nothing to translate (e.g. a sticker with no caption)

    media = extract_media(post)

    for lang_key, chat_id in target_channels().items():
        try:
            translated = translate(text, lang_key)
        except Exception as e:
            print(f"Sarvam translation to {lang_key} failed: {e}", file=sys.stderr)
            translated = text  # fall back to the original rather than dropping it
        try:
            send_to_channel(chat_id, translated, media)
        except Exception as e:
            print(f"Send to {lang_key} channel failed: {e}", file=sys.stderr)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        body = self.rfile.read(length)
        try:
            process_update(json.loads(body))
        except Exception as e:
            print(f"webhook error: {e}", file=sys.stderr)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Company relay webhook is running.")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"Company relay listening on port {port}")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
