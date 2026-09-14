"""
Company announcement relay -- instant Telegram-to-Telegram translation.
------------------------------------------------------------------------
The boss posts in English in one "source" channel. This webhook fires
the moment that happens, translates the message into Hindi, Urdu,
Nepali, and Bengali, and posts each version to its own dedicated
channel -- instantly, no polling.

Since everything happens inside Telegram, media (photos/videos) don't
need external URLs -- Telegram lets a bot resend a file it has already
seen using its file_id, so images are relayed directly.

Required environment variables:
  TELEGRAM_BOT_TOKEN    - token for THIS dedicated bot (from @BotFather)
  SOURCE_CHANNEL_ID      - the boss's channel ("@name" or numeric -100... ID)
  LANGBLY_URL            - your langbly translate endpoint (full URL)
  LANGBLY_API_KEY        - langbly auth key
  TELEGRAM_CHAT_ID_HI    - Hindi channel
  TELEGRAM_CHAT_ID_UR    - Urdu channel
  TELEGRAM_CHAT_ID_NE    - Nepali channel
  TELEGRAM_CHAT_ID_BN    - Bengali channel

Known limitation: if the boss posts multiple photos at once (an
"album"), Telegram delivers each photo as a separate update sharing a
media_group_id. This script relays each photo individually rather than
regrouping them -- fine for single-image posts (the common case), but
a multi-photo album will arrive as several separate messages in the
target channels instead of one grouped album.
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

LANGUAGE_CODE_CANDIDATES = {
    "hi": ["hi", "hi-IN", "hin", "Hindi"],
    "ur": ["ur", "ur-PK", "urd", "Urdu"],
    "ne": ["ne", "ne-NP", "nep", "Nepali"],
    "bn": ["bn", "bn-BD", "ben", "Bengali"],
}


def target_channels() -> dict:
    return {
        "hi": os.environ["TELEGRAM_CHAT_ID_HI"],
        "ur": os.environ["TELEGRAM_CHAT_ID_UR"],
        "ne": os.environ["TELEGRAM_CHAT_ID_NE"],
        "bn": os.environ["TELEGRAM_CHAT_ID_BN"],
    }


def translate(text: str, target: str) -> str:
    resp = requests.post(
        os.environ["LANGBLY_URL"],
        headers={
            "Authorization": f"Bearer {os.environ['LANGBLY_API_KEY']}",
            "Content-Type": "application/json",
        },
        json={"q": text, "source": "en", "target": target},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["data"]["translations"][0]["translatedText"]


def translate_with_fallback(text: str, lang: str) -> str:
    for code in LANGUAGE_CODE_CANDIDATES.get(lang, [lang]):
        try:
            result = translate(text, code)
        except Exception as e:
            print(f"langbly code {code!r} for {lang!r} failed: {e}", file=sys.stderr)
            continue
        if result.strip() != text.strip():
            return result
    print(f"WARNING: no working langbly code for {lang!r}, using original", file=sys.stderr)
    return text


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

    chat_id = str(post["chat"]["id"])
    source = os.environ["SOURCE_CHANNEL_ID"].lstrip("@")
    if chat_id != source and chat_id != f"@{source}":
        return  # a post from some other channel this bot happens to admin -- ignore

    text = post.get("text") or post.get("caption") or ""
    if not text.strip():
        return  # nothing to translate (e.g. a sticker with no caption)

    media = extract_media(post)

    for lang, chat_id in target_channels().items():
        translated = translate_with_fallback(text, lang)
        try:
            send_to_channel(chat_id, translated, media)
        except Exception as e:
            print(f"Send to {lang} channel failed: {e}", file=sys.stderr)


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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"Company relay listening on port {port}")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
