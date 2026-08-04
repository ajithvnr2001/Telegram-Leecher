# copyright 2023 © Xron Trix | https://github.com/Xrontrix10

import logging, json, asyncio
from uvloop import install
from pyrogram.client import Client

# Read the dictionary from the txt file
with open("/content/Telegram-Leecher/credentials.json", "r") as file:
    credentials = json.loads(file.read())

API_ID = credentials["API_ID"]
API_HASH = credentials["API_HASH"]
BOT_TOKEN = credentials["BOT_TOKEN"]
OWNER = credentials["USER_ID"]
DUMP_ID = credentials["DUMP_ID"]

# S3 / Wasabi config (optional — used by /s3upload and /s3leech)
S3_ACCESS_KEY = credentials.get("S3_ACCESS_KEY", "") or ""
S3_SECRET_KEY = credentials.get("S3_SECRET_KEY", "") or ""
S3_BUCKET_NAME = credentials.get("S3_BUCKET_NAME", "") or ""
S3_ENDPOINT_URL = credentials.get("S3_ENDPOINT_URL", "") or ""
S3_REGION = credentials.get("S3_REGION", "") or "us-east-1"

# Apple Music config (optional — used by /amusic)
# AM_PLAYLIST_URL — playlist link → batch download (5-song batches, MVs after).
# AM_ARTIST_URL — artist link → all-album download (one album per batch, MVs after).
# Both may be set at the same time; each source is processed independently and
# keeps its OWN S3 log keyspace, so resuming one never reads the other's logs.
AM_PLAYLIST_URL = credentials.get("AM_PLAYLIST_URL", "") or ""
AM_ARTIST_URL = credentials.get("AM_ARTIST_URL", "") or ""
AM_MEDIA_TOKEN = credentials.get("AM_MEDIA_TOKEN", "") or ""
# Optional pinned Authorization JWT for am-downloader. When set, am-downloader
# prefers it over its own GetToken() scrape (which can return an empty JWT from
# some IPs/locales — Apple then silently rejects every webPlayback request,
# surfacing as "media-user-token may wrong or expired"). Leave empty to use the
# built-in scrape.
AM_AUTH_TOKEN = credentials.get("AM_AUTH_TOKEN", "") or ""
# Artist mode caps: 0 = everything. AM_ALBUM_LIMIT limits how many of the
# artist's albums are downloaded (oldest first), AM_MV_LIMIT caps the artist
# music-video pass.
AM_ALBUM_LIMIT = int(credentials.get("AM_ALBUM_LIMIT", 0) or 0)
AM_MV_LIMIT = int(credentials.get("AM_MV_LIMIT", 0) or 0)
# Pacing between successive Telegram uploads (seconds). Telegram rate-limits
# upload bursts account-wide ([420 FLOOD_WAIT_X]); 3s/file keeps long AM runs
# inside limits. 0 = no pacing. Pass "AM_UPLOAD_GAP" in credentials.json.
AM_UPLOAD_GAP = int(credentials.get("AM_UPLOAD_GAP", 3) or 0)
# Songlist mode: when AM_SONGLIST_AUTO is truthy the bot starts downloading
# /content/songlist.txt by itself right after startup — no /amusic "START"
# press needed ("ticked" in the Colab cell = True).
AM_SONGLIST_AUTO = str(credentials.get("AM_SONGLIST_AUTO", "") or "").lower() in (
    "1", "true", "yes", "on",
) or credentials.get("AM_SONGLIST_AUTO") is True


logging.basicConfig(level=logging.INFO)

install()

# Fix for Python 3.12+ — uvloop.install() replaces the event loop policy but does NOT
# create an event loop. In Python 3.12+ asyncio.get_event_loop() raises RuntimeError if
# there is no running loop in the current thread. We must explicitly create one BEFORE
# instantiating Pyrogram's Client (whose Dispatcher.__init__ calls get_event_loop()).
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

colab_bot = Client("my_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
