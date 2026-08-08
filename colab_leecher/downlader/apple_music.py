# copyright 2024 © Xron Trix | https://github.com/Xrontrix10


"""
Apple Music downloader integration for Colab Leecher.

Adds a /amusic command that:
  1. Downloads a predefined Apple Music playlist (AM_PLAYLIST_URL, set in the
     Colab notebook) in ALL available audio formats:
       - ALAC            (default run)
       - Dolby Atmos/EC3 (--atmos run)
       - AAC-LC 256      (--aac --aac-type aac-lc)
- AAC 128         (--aac --aac-type aac-128)
        - HE-AAC 64       (--aac --aac-type he-aac-64)
     Music videos (musicVideo items) are downloaded too, in a second phase
     (mv-max: 2160, mv-audio-type: atmos) into the MV save folder.
  2. Names every file with the SAME convention across all formats:
      {SongNumber}.{SongName}.{FORMAT}.{VARIANT}.m4a
     e.g. 01.KangalIrandal.ALAC.Lossless.m4a, 01.KangalIrandal.AAC.256Kbps.m4a,
     so each song downstream yields one file per format (5 files/song).
  3. Saves everything under /music (AM_MUSIC_PATH).
  4. Uploads every downloaded track to Telegram (via the standard Leech
     pipeline).
  5. Mirrors each format's download log to S3 (music-logs/) for tracking.

Requirements on the Colab VM (set up automatically):
  - wrapper (https://github.com/WorldObservationLog/wrapper) decryption
    server, downloaded prebuilt from S3 and started on ports 10020/20020/30020
  - am-downloader (https://github.com/zhaarey/apple-music-downloader)
    prebuilt binary, downloaded from S3
  - MP4Box (gpac) for ALAC packaging

The prebuilt toolchain is fetched from:
  https://s3.ap-northeast-1.wasabisys.com/musicapple/am-tools/{wrapper-release.tar.gz,am-downloader}
"""


import os
import re
import json
import shutil
import logging
import subprocess
from time import sleep
from asyncio import sleep as asleep
from os import makedirs, path as ospath

from colab_leecher.utility.variables import Paths, Messages, MSG, BOT
from colab_leecher.utility.helper import keyboard, sysINFO
from colab_leecher import S3_BUCKET_NAME


AM_MUSIC_PATH = "/music"  # new path where all Apple Music formats land
# /amusic local saves everything here instead (Colab disk, no Telegram upload).
AM_LOCAL_MUSIC_PATH = "/content"
AM_TOOLS_PATH = "/content/am-tools"

AM_TOOL_WRAPPER_URL = (
    "https://s3.ap-northeast-1.wasabisys.com/musicapple/am-tools/wrapper-release.tar.gz"
)
AM_TOOL_DOWNLOADER_URL = (
    "https://s3.ap-northeast-1.wasabisys.com/musicapple/am-tools/am-downloader"
)
# Tools are stored in the S3 bucket configured in credentials.json under
# the "am-tools" prefix. Public URLs above are only a fallback.
AM_TOOLS_S3_PREFIX = "am-tools"

# Each pass = (name, extra args, song-file-format). Playlist URL is appended
# at run time.
#
# Naming convention (flow.txt): every file is named
#     {SongNumber}.{SongName}.{FORMAT}.{VARIANT}.m4a
# so all five formats follow the SAME naming scheme and each song yields 5
# files, e.g.:
#     01.KangalIrandal.ALAC.Lossless.m4a
#     01.KangalIrandal.ATMOS.Dolby.m4a
#     01.KangalIrandal.AAC.256Kbps.m4a
#     01.KangalIrandal.AAC.128Kbps.m4a
#     01.KangalIrandal.AAC.64Kbps.m4a
# The downloader appends ".m4a" itself, so the format string has no extension.
AM_FORMATS = [
    ("ALAC", [], "{SongNumer}.{SongName}.ALAC.Lossless"),
    ("ATMOS", ["--atmos"], "{SongNumer}.{SongName}.ATMOS.Dolby"),
    (
        "AAC-LC-256",
        ["--aac", "--aac-type", "aac-lc"],
        "{SongNumer}.{SongName}.AAC.256Kbps",
    ),
    (
        "AAC-128",
        ["--aac", "--aac-type", "aac-128"],
        "{SongNumer}.{SongName}.AAC.128Kbps",
    ),
    (
        "HE-AAC-64",
        ["--aac", "--aac-type", "he-aac-64"],
        "{SongNumer}.{SongName}.AAC.64Kbps",
    ),
]

# Music-video pass: am-downloader handles /music-video/ URLs directly and
# saves them under the configured MV folder (AM-DL-MV downloads). The
# highest quality is picked via the config: mv-max 2160 + mv-audio-type
# atmos (main.go extractMvAudio picks Atmos > AC3 > AAC 256 > ...).
AM_MV_FORMAT = "MV"

AM_LOG_DIR = ospath.join(AM_MUSIC_PATH, "am-logs")
# Every format log is also mirrored to S3 under this key prefix
# (music-logs/<format>-batchNN.log). It doubles as the resume tracker:
# if a batch has ALL of its format logs in S3, that batch was finished in
# a previous run and is skipped on the next one.
S3_LOG_PREFIX = "music-logs"
AM_CONFIG_PATH = ospath.join(AM_MUSIC_PATH, "config.yaml")
AM_WRAPPER_CMD = "./wrapper -H 0.0.0.0 -B rootfs/data/data/com.apple.android.music/files"
AM_WRAPPER_ENV = {"AM_BIND_PROC": "1", "AM_NO_PIDNS": "1"}

# Songlist mode (/amusic songs): an arbitrary song list file, e.g. generated
# from /root/links.txt, is downloaded in ALL formats. Resume state for it is
# ONE single file (appended line per track×format) mirrored to S3, NOT the
# per-batch batchNN logs of playlist mode. Two marker kinds: "UPLOADING <fmt>
# <adamID>" (write-ahead, before the upload) and "DONE <fmt> <adamID>" (after
# the upload landed), so a crash mid-upload is verified against the chat
# history on the next run.
AM_SONGLIST_PATH = "/content/songlist.txt"
AM_SONGLIST_DEDUPE_KEY = f"{S3_LOG_PREFIX}/songlist-dedupe.log"
AM_SONGLIST_CHUNK = 25  # songs per am-downloader subprocess (bounds runtime)


def is_am_playlist(url: str) -> bool:
    """True for https://music.apple.com/<cc>/playlist/<name>/<id> links."""
    return "music.apple.com" in url and "/playlist/" in url


def is_am_artist(url: str) -> bool:
    """True for https://music.apple.com/<cc>/artist/<name>/<id>[/...] links."""
    return "music.apple.com" in url and "/artist/" in url


def set_am_music_path(path: str):
    """Point the AM working directory at ``path`` and derive the log/config
    sub-paths from it.

    The default is /music (AM_MUSIC_PATH). ``/amusic local`` switches it to
    /content (AM_LOCAL_MUSIC_PATH) so everything is saved to the Colab disk
    instead of the tmpfs /music mount and nothing is uploaded to Telegram.
    """
    global AM_MUSIC_PATH, AM_LOG_DIR, AM_CONFIG_PATH
    AM_MUSIC_PATH = path
    AM_LOG_DIR = ospath.join(path, "am-logs")
    AM_CONFIG_PATH = ospath.join(path, "config.yaml")


def _am_tools_ready() -> bool:
    return ospath.exists(ospath.join(AM_TOOLS_PATH, "am-downloader")) and ospath.exists(
        ospath.join(AM_TOOLS_PATH, "wrapper-release", "wrapper")
    )


def _parse_playlist_page(playlist_url: str):
    """Fetch the public playlist page and return (songs, music_videos) URLs.

    Parses serialized-server-data — no auth needed. Songs come back as
    ``/song/{adamID}`` URLs, music videos as ``/music-video/{adamID}`` URLs
    (am-downloader accepts both forms directly).
    """
    import urllib.request

    req = urllib.request.Request(
        playlist_url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
    )
    html = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "ignore")
    m = re.search(r'serialized-server-data">(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError("Could not find playlist data on page")
    data = json.loads(m.group(1))
    try:
        sections = data["data"][0]["data"]["sections"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("Unexpected playlist page structure")
    songs = []
    mvs = []
    for sec in sections:
        if "track" not in str(sec.get("id", "")):
            continue
        for item in sec.get("items", []):
            cd = item.get("contentDescriptor") or {}
            ident = (cd.get("identifiers") or {}).get("storeAdamID")
            if not ident:
                continue
            if cd.get("kind") == "song":
                songs.append(f"https://music.apple.com/in/song/{ident}")
            elif cd.get("kind") == "musicVideo":
                mvs.append(f"https://music.apple.com/in/music-video/{ident}")
    return songs, mvs


def fetch_playlist_songs(playlist_url: str, limit: int = 0) -> list:
    """Return per-song download URLs for an Apple Music playlist.

    Parses the public playlist page (serialized-server-data) — no auth needed.
    am-downloader accepts these /song/ URLs directly. Music videos in the
    same playlist are skipped here (they get their own pass).
    """
    songs, _ = _parse_playlist_page(playlist_url)
    if not songs:
        raise RuntimeError("No songs found in playlist page")
    logging.info("AM playlist has %d songs", len(songs))
    return songs[:limit] if limit else songs


def fetch_playlist_mvs(playlist_url: str, limit: int = 0) -> list:
    """Return per-music-video download URLs for an Apple Music playlist.

    Same page parsing as :func:`fetch_playlist_songs`, but keeps only the
    ``musicVideo`` items. am-downloader's ``/music-video/{id}`` handler needs
    the media-user-token and the mp4decrypt binary.
    """
    _, mvs = _parse_playlist_page(playlist_url)
    logging.info("AM playlist has %d music videos", len(mvs))
    return mvs[:limit] if limit else mvs


def _am_amp_token() -> str:
    """Fetch a Bearer JWT for the amp-api endpoints.

    am-downloader scrapes this from the Apple Music web bundle at startup
    (ampapi.GetToken). Some IPs/locales get an EMPTY JWT from that scrape and
    Apple silently 401s every amp-api/webPlayback call afterwards. When
    AM_AUTH_TOKEN is pinned in credentials.json we prefer it (it is a real,
    long-lived token), falling back to the same scrape the binary does.
    """
    from colab_leecher import AM_AUTH_TOKEN

    if AM_AUTH_TOKEN:
        return AM_AUTH_TOKEN

    import urllib.request

    hdr = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    html = urllib.request.urlopen(
        urllib.request.Request("https://music.apple.com", headers=hdr), timeout=60
    ).read().decode("utf-8", "ignore")
    m = re.search(r"/assets/index~[^/]+\.js", html)
    if not m:
        raise RuntimeError("Could not locate the Apple Music web bundle (index~*.js)")
    body = urllib.request.urlopen(
        urllib.request.Request("https://music.apple.com" + m.group(0), headers=hdr),
        timeout=60,
    ).read().decode("utf-8", "ignore")
    tok = re.search(r"eyJ[A-Za-z0-9-_=]+\.[A-Za-z0-9-_=]+\.[A-Za-z0-9-_=]+", body)
    if not tok:
        raise RuntimeError("Could not extract a JWT from the Apple Music web bundle")
    return tok.group(0)


def _am_amp_get(url: str, token: str) -> dict:
    """GET an amp-api URL with the Bearer JWT. Raises on non-200."""
    import urllib.request

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Origin": "https://music.apple.com",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        if resp.status != 200:
            raise RuntimeError(f"amp-api returned HTTP {resp.status}")
        return json.loads(resp.read().decode("utf-8", "ignore"))


def _am_api_paginate(base_url: str, token: str, field: str) -> list:
    """Yield all ``data`` entries from a paginated amp-api JSON payload."""
    out = []
    offset = 0
    while True:
        sep = "&" if "?" in base_url else "?"
        data = _am_amp_get(f"{base_url}{sep}limit=100&offset={offset}&l=en-GB", token)
        items = data.get(field, [])
        if not isinstance(items, list) or not items:
            break
        out.extend(items)
        if not data.get("next"):
            break
        offset += len(items)
    return out


def fetch_artist_albums(artist_url: str, limit: int = 0) -> list:
    """Return album URLs for an artist, oldest release first.

    Paginates amp-api ``/catalog/<cc>/artists/<id>/albums`` (the same
    relationship am-downloader's checkArtist uses, ``limit=100`` pages) and
    sorts by release date ascending so the classic albums come first — the
    same order am-downloader presents them in its artist picker.
    """
    m = re.search(r"music\.apple\.com/(\w{2})/artist/.+?/(\d+)(?:$|[/?#])", artist_url)
    if not m:
        raise RuntimeError(f"Not a valid Apple Music artist URL: {artist_url}")
    cc, artist_id = m.group(1), m.group(2)
    token = _am_amp_token()
    base = f"https://amp-api.music.apple.com/v1/catalog/{cc}/artists/{artist_id}/albums"
    items = _am_api_paginate(base, token, "data")
    rows = []
    for it in items:
        a = it.get("attributes") or {}
        rows.append((a.get("releaseDate") or "", a.get("url") or "", a.get("name") or ""))
    rows.sort(key=lambda r: r[0])  # oldest first
    urls = [r[1] for r in rows if r[1]]
    if limit:
        urls = urls[:limit]
    logging.info("AM artist has %d albums (showing %d)", len(rows), len(urls))
    return urls


def fetch_artist_mvs(artist_url: str, limit: int = 0) -> list:
    """Return music-video URLs for an artist (amp-api ``music-videos``).

    Apple does not expose an album→music-video mapping in this API, so the
    MV pass for an artist is done artist-wide: every music video credited to
    the artist gets downloaded at max quality.
    """
    m = re.search(r"music\.apple\.com/(\w{2})/artist/.+?/(\d+)(?:$|[/?#])", artist_url)
    if not m:
        raise RuntimeError(f"Not a valid Apple Music artist URL: {artist_url}")
    cc, artist_id = m.group(1), m.group(2)
    token = _am_amp_token()
    base = f"https://amp-api.music.apple.com/v1/catalog/{cc}/artists/{artist_id}/music-videos"
    items = _am_api_paginate(base, token, "data")
    urls = []
    for it in items:
        a = it.get("attributes") or {}
        if a.get("url"):
            urls.append(a["url"])
    if limit:
        urls = urls[:limit]
    logging.info("AM artist has %d music videos (taking %d)", len(urls), len(items))
    return urls


async def _am_update_status(head: str):
    Messages.status_head = head
    try:
        text = Messages.task_msg + head + sysINFO()
        if getattr(_am_update_status, "last_text", None) == text:
            return
        MSG.status_msg = await MSG.status_msg.edit_text(
            text=text,
            reply_markup=keyboard(),
        )
        _am_update_status.last_text = text
    except Exception as e:
        logging.error(f"AM status update failed: {e}")


def _download_file(url: str, dest: str):
    """Download a URL, retrying up to 3 times."""
    for attempt in range(3):
        try:
            subprocess.run(
                ["aria2c", "-x", "8", "-s", "8", "-d", ospath.dirname(dest), "-o", ospath.basename(dest), url],
                check=True,
                capture_output=True,
                timeout=900,
            )
            if ospath.exists(dest):
                return
        except Exception as e:
            logging.error(f"Download attempt {attempt + 1} failed for {url}: {e}")
            sleep(5)
    raise RuntimeError(f"Failed to download {url}")


def _download_tool(client, key: str, dest: str, fallback_url: str = ""):
    """Download a tool from S3 (credentials.json bucket) or public URL fallback.

    Raises a clear, actionable error when the object is missing from the
    bucket so a wiped ``am-tools/`` prefix isn't reported as a cryptic 404.
    """
    if client is not None:
        try:
            client.download_file(S3_BUCKET_NAME, key, dest)
            if ospath.exists(dest):
                return
        except Exception as e:
            logging.error(f"S3 tool download failed for {key}: {e}")
    if fallback_url:
        try:
            _download_file(fallback_url, dest)
            return
        except Exception:
            logging.error(f"Fallback download failed for {fallback_url}")
    raise RuntimeError(
        f"am-tools missing from s3://{S3_BUCKET_NAME}/ — object `{key}` was not "
        "found. /amusic needs `am-tools/am-downloader`, "
        "`am-tools/wrapper-release.tar.gz` and `am-tools/mp4decrypt` in the "
        "bucket (see docs/APPLE_MUSIC.md, 'Toolchain' section). Re-upload them "
        "or restore the prefix, then re-run /amusic."
    )


def _s3_client_or_none():
    from colab_leecher import (
        S3_ACCESS_KEY,
        S3_SECRET_KEY,
        S3_ENDPOINT_URL,
        S3_REGION,
    )
    from colab_leecher.uploader.s3 import ensure_s3_client

    try:
        if all([S3_ACCESS_KEY, S3_SECRET_KEY, S3_BUCKET_NAME]):
            return ensure_s3_client()
    except Exception as e:
        logging.error(f"S3 client init failed: {e}")
    return None


async def ensure_am_tools():
    """Fetch the wrapper + am-downloader toolchain from S3 if missing."""
    makedirs(AM_TOOLS_PATH, exist_ok=True)
    if _am_tools_ready():
        await _ensure_am_mp4decrypt()
        return

    await _am_update_status("<b>🎵 APPLE MUSIC » </b>\n⏳ __Fetching am-downloader toolchain from S3...__")

    client = _s3_client_or_none()

    dl = ospath.join(AM_TOOLS_PATH, "am-downloader")
    if not ospath.exists(dl):
        _download_tool(client, f"{AM_TOOLS_S3_PREFIX}/am-downloader", dl)
        os.chmod(dl, 0o755)

    wr_dir = ospath.join(AM_TOOLS_PATH, "wrapper-release")
    if not ospath.exists(ospath.join(wr_dir, "wrapper")):
        tarball = ospath.join(AM_TOOLS_PATH, "wrapper-release.tar.gz")
        if not ospath.exists(tarball):
            _download_tool(client, f"{AM_TOOLS_S3_PREFIX}/wrapper-release.tar.gz", tarball)
        subprocess.run(
            ["tar", "xzf", tarball, "-C", AM_TOOLS_PATH], check=True, timeout=600
        )
        os.chmod(ospath.join(wr_dir, "wrapper"), 0o755)
        main_bin = ospath.join(wr_dir, "rootfs", "system", "bin", "main")
        if ospath.exists(main_bin):
            os.chmod(main_bin, 0o755)

    await _ensure_am_mp4decrypt(client)
    logging.info("AM tools ready.")


async def _ensure_am_mp4decrypt(client=None):
    """Make sure the ``mp4decrypt`` (Bento4) binary is on PATH.

    Required by am-downloader for the music-video pass. Where it's already
    installed (e.g. apt bento4) nothing happens; otherwise it is pulled
    from S3 (am-tools/mp4decrypt).
    """
    import shutil

    if shutil.which("mp4decrypt"):
        return
    dest = "/usr/local/bin/mp4decrypt"
    if ospath.exists(dest):
        return
    if client is None:
        client = _s3_client_or_none()
    if client is None:
        logging.warning("AM mp4decrypt unavailable and no S3 client — MV tracks will be skipped.")
        return
    try:
        client.download_file(S3_BUCKET_NAME, f"{AM_TOOLS_S3_PREFIX}/mp4decrypt", dest)
        os.chmod(dest, 0o755)
        logging.info("AM mp4decrypt installed at %s", dest)
    except Exception as e:
        logging.error(f"AM mp4decrypt download failed ({e}) — MV tracks will be skipped.")

def _am_wrapper_running() -> bool:
    try:
        import socket

        with socket.create_connection(("127.0.0.1", 10020), timeout=2):
            return True
    except OSError:
        return False


def start_am_wrapper():
    """Start the decryption wrapper in the background (ports 10020/20020/30020).

    The Android build sometimes fails its first devToken fetch (transient);
    retry a few times before giving up.
    """
    if _am_wrapper_running():
        logging.info("AM wrapper already running.")
        return
    wr_dir = ospath.join(AM_TOOLS_PATH, "wrapper-release")
    last_log = ""
    for attempt in range(4):
        log_path = ospath.join(AM_TOOLS_PATH, f"wrapper.log")
        logf = open(log_path, "w")
        subprocess.Popen(
            AM_WRAPPER_CMD,
            cwd=wr_dir,
            shell=True,
            stdout=logf,
            stderr=logf,
            start_new_session=True,
            env={**os.environ, **AM_WRAPPER_ENV},
        )
        for _ in range(30):
            sleep(2)
            if _am_wrapper_running():
                logging.info("AM wrapper started OK (attempt %s).", attempt + 1)
                return
        # not up: kill any leftover child and retry
        subprocess.run(["pkill", "-f", "system/bin/main"], check=False)
        subprocess.run(["pkill", "-f", "wrapper -H"], check=False)
        sleep(3)
        try:
            with open(log_path) as f:
                last_log = f.read()[-800:]
        except OSError:
            last_log = ""
        logging.warning("AM wrapper attempt %s failed to open port.", attempt + 1)
    raise RuntimeError(
        "AM wrapper failed to start after 4 attempts. "
        f"Last wrapper.log:\n{last_log}"
    )


def _write_am_config():
    """Write am-downloader config.yaml inside /music.

    Folders are relative so every format lands under /music when the binary
    runs with cwd=/music. The media user token comes from credentials.json
    (AM_MEDIA_TOKEN, set in the Colab notebook). AM_AUTH_TOKEN, when set, is
    pinned as authorization-token so am-downloader never relies on its own
    GetToken() scrape (which can return an empty JWT from some IPs/locales,
    silently breaking every webPlayback request as "media-user-token may wrong
    or expired").
    """
    from colab_leecher import AM_MEDIA_TOKEN, AM_AUTH_TOKEN

    cfg = f"""media-user-token: "{AM_MEDIA_TOKEN}"
authorization-token: "{AM_AUTH_TOKEN}"
language: ""
lrc-type: "lyrics"
lrc-format: "lrc"
embed-lrc: true
save-lrc-file: false
save-artist-cover: false
save-animated-artwork: false
emby-animated-artwork: false
embed-cover: true
cover-size: 5000x5000
cover-format: jpg
tag-sort-order: true
tag-itunes-id: true
alac-save-folder: AM-DL downloads
atmos-save-folder: AM-DL-Atmos downloads
aac-save-folder: AM-DL-AAC downloads
mv-save-folder: AM-DL-MV downloads
max-memory-limit: 256
decrypt-m3u8-port: "127.0.0.1:10020"
get-m3u8-port: "127.0.0.1:20020"
get-m3u8-from-device: true
# Keep exit-on-error TRUE: am-downloader otherwise drops into an interactive
# "press Enter to retry" loop on the FIRST erroring track (e.g. an
# explicit-content-blocked MV), and with no TTY that loops forever. With
# exit-on-error the batch still processes ALL tracks once (good ones get
# downloaded) and then exits, which _run_am_pass treats as a pass.
exit-on-error: true
get-m3u8-mode: all
aac-type: aac-lc
alac-max: 192000
atmos-max: 2768
limit-max: 200
album-folder-format: "{{AlbumName}}"
playlist-folder-format: "{{PlaylistName}}"
song-file-format: "{{SongNumer}}. {{SongName}}"
artist-folder-format: "{{UrlArtistName}}"
explicit-choice: "[E]"
clean-choice: "[C]"
apple-master-choice: "[M]"
use-songinfo-for-playlist: false
dl-albumcover-for-playlist: false
mv-audio-type: atmos
mv-max: 2160
storefront: "in"
alac-fix: false
convert-after-download: false
convert-format: "flac"
convert-keep-original: false
convert-skip-if-source-matches: true
ffmpeg-path: "ffmpeg"
convert-extra-args: ""
convert-with-metadata: true
convert-warn-lossy-to-lossless: true
convert-skip-lossy-to-lossless: true
convert-check-bad-alac: false
convert-delete-bad-alac: false
proxy: ""
"""
    makedirs(AM_MUSIC_PATH, exist_ok=True)
    with open(AM_CONFIG_PATH, "w") as f:
        f.write(cfg)
    logging.info("AM config written to %s", AM_CONFIG_PATH)


def _run_am_pass(name: str, extra_args: list, urls: list, suffix: str = "", file_format: str = "", append: bool = False) -> str:
    """Run one am-downloader pass over a list of song URLs.

    Returns path to its log file. With ``append=True`` the pass APPENDS to the
    same log file instead of truncating it (songlist mode keeps one log per
    format across all its chunks).
    """
    makedirs(AM_LOG_DIR, exist_ok=True)
    log_name = f"{name.lower()}{suffix}.log"
    log_path = ospath.join(AM_LOG_DIR, log_name)
    cmd = [ospath.join(AM_TOOLS_PATH, "am-downloader"), *extra_args]
    if file_format:
        cmd += ["--song-file-format", file_format]
    cmd += urls
    with open(log_path, "a" if append else "w") as logf:
        proc = subprocess.run(
            cmd,
            cwd=AM_MUSIC_PATH,
            stdout=logf,
            stderr=subprocess.STDOUT,
            timeout=3 * 60 * 60,
        )
    ok = proc.returncode == 0
    logging.info("AM pass %s finished rc=%s log=%s", name, proc.returncode, log_path)
    # Non-zero return codes still leave partially downloaded tracks behind;
    # treat as pass and let the log tell the story (Unavailable tracks are
    # normal for formats Apple doesn't offer).
    _ = ok
    return log_path


def _am_files_snapshot() -> set:
    """Absolute paths of every file currently under /music.

    Pass logs live under AM_LOG_DIR (inside AM_MUSIC_PATH) — they are internal
    state, NOT media, and must never be picked up by the upload diff.
    """
    out = set()
    for root, _dirs, files in os.walk(AM_MUSIC_PATH):
        for f in files:
            p = ospath.join(root, f)
            if p.startswith(AM_LOG_DIR):
                continue
            out.add(p)
    return out


def _am_log_key(name: str, suffix: str = "") -> str:
    """S3 object key for one format's download log.

    Mirrors the on-disk name (name.lower() + optional -batchNN suffix) so
    the resume scan can tie an S3 object back to a concrete batch/format.
    """
    return f"{S3_LOG_PREFIX}/{name.lower()}{suffix}.log"


def _am_mv_log_key(batch_no: int, artist: bool = False) -> str:
    """S3 object key for a music-video batch log.

    Playlist MVs use ``music-logs/mv-batchNN.log`` and artist-wide MVs use
    ``music-logs/artist-mv-batchNN.log`` — two distinct keyspaces so resuming
    one source never reads the other's MV logs.
    """
    prefix = "artist-mv" if artist else "mv"
    return f"{S3_LOG_PREFIX}/{prefix}-batch{batch_no:02d}.log"


def _am_album_log_key(album_id: str, name: str, suffix: str = "") -> str:
    """S3 object key for one album: ``music-logs/album-<id>/<format>[<suffix>].log``."""
    return f"{S3_LOG_PREFIX}/album-{album_id}/{name.lower()}{suffix}.log"


def _am_scan_s3(prefix: str) -> list:
    """List S3 object keys under ``prefix`` (best effort, [] on failure)."""
    from colab_leecher.uploader.s3 import ensure_s3_client

    keys = []
    try:
        client = ensure_s3_client()
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=prefix):
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
    except Exception as e:
        logging.error(f"AM resume: S3 scan failed for {prefix!r}: {e}")
    return keys


def am_completed_batches() -> set:
    """Batch numbers (int) already finished in a PREVIOUS run in S3.

    A batch counts as completed when every one of the AM_FORMATS has its
    mirror of ``music-logs/<format>-batchNN.log`` in the bucket:

        AM pass ALAC finished     -> music-logs/alac-batch01.log
        AM pass ATMOS finished    -> music-logs/atmos-batch01.log
        ...

    These objects are written AFTER a format pass finishes inside
    ``Do_AM_Music``, so their presence means the download itself was
    completed and the log made its way to S3. Re-running /amusic then
    skips those batches instead of restarting from scratch.
    """
    required = {name.lower() for name, _, _ in AM_FORMATS}
    fmt_alt = "|".join(sorted(required))
    batch_to_formats = {}
    for key in _am_scan_s3(f"{S3_LOG_PREFIX}/"):
        # Strictly playlist song keys only: music-logs/<format>[-batchNN].log
        # where <format> is one of the AM_FORMATS. This deliberately excludes
        # playlist MV keys (music-logs/mv-batchNN.log), artist-MV keys
        # (music-logs/artist-mv-batchNN.log) and album-scoped keys
        # (music-logs/album-<id>/...) so resuming a playlist never reads logs
        # that belong to the artist mode (and vice versa).
        m = re.match(
            rf"^{re.escape(S3_LOG_PREFIX)}/({fmt_alt})(?:-batch(\d+))?\.log$",
            key,
        )
        if not m:
            continue
        fmt = m.group(1)
        batch = int(m.group(2)) if m.group(2) else 0
        batch_to_formats.setdefault(batch, set()).add(fmt)

    return {batch for batch, fmts in batch_to_formats.items() if required.issubset(fmts)}


def am_completed_mv_batches() -> set:
    """Batch numbers (int) of the MV pass already finished in a PREVIOUS run.

    The MV pass is a single run per batch (unlike the 5 audio formats), so a
    batch counts as completed when its own log ``music-logs/mv-batchNN.log``
    exists in S3.
    """
    done = set()
    for key in _am_scan_s3(f"{S3_LOG_PREFIX}/"):
        m = re.match(rf"^{re.escape(S3_LOG_PREFIX)}/mv-batch(\d+)\.log$", key)
        if m:
            done.add(int(m.group(1)))
    return done


def am_completed_albums() -> set:
    """Album IDs (str) already fully downloaded in a PREVIOUS run.

    An album counts as completed when EVERY AM_FORMATS log exists under
    ``music-logs/album-<id>/`` (e.g. alac.log, atmos.log, aac-lc-256.log,
    aac-128.log, he-aac-64.log) — same convention as the playlist batches.
    """
    required = {name.lower() for name, _, _ in AM_FORMATS}
    done = set()
    # Group keys by album id.
    per_album = {}
    for key in _am_scan_s3(f"{S3_LOG_PREFIX}/album-"):
        m = re.match(
            rf"^{re.escape(S3_LOG_PREFIX)}/album-([^/]+)/([^/]+?)\.log$", key
        )
        if m:
            per_album.setdefault(m.group(1), set()).add(m.group(2))
    return {
        album_id
        for album_id, fmts in per_album.items()
        if required.issubset(fmts)
    }


def am_completed_artist_mv_batches() -> set:
    """Batch numbers (int) of the artist-wide MV pass already finished.

    Mirrors ``music-logs/artist-mv-batchNN.log`` — present means that batch
    of artist music videos was downloaded in a previous run.
    """
    done = set()
    for key in _am_scan_s3(f"{S3_LOG_PREFIX}/"):
        m = re.match(rf"^{re.escape(S3_LOG_PREFIX)}/artist-mv-batch(\d+)\.log$", key)
        if m:
            done.add(int(m.group(1)))
    return done


# --- per-song dedupe -------------------------------------------------------
#
# Beyond the batch/album-level logs above, every individual track gets its own
# S3 marker the moment a pass finishes it. On a resume, a pass only re-runs the
# tracks that are still missing THAT format, so a crash mid-batch never forces
# the whole batch to be re-downloaded (or re-uploaded to Telegram).
#
# Keyspaces (all isolated from the batch/album logs above):
#   playlist songs : music-logs/song-<format>-<adamID>.log
#   album songs    : music-logs/album-<id>/song-<format>-<adamID>.log
#   playlist MVs   : music-logs/mv-song-<adamID>.log
#   artist MVs     : music-logs/artist-mv-song-<adamID>.log


def _am_url_adam_id(url: str) -> str:
    """Extract the store adamID from a /song/, /album/ or /music-video/ URL."""
    m = re.search(r"/(?:song|album|music-video|playlist)/(?:[^/]+/)?(\d+)", url)
    return m.group(1) if m else ""


def _am_parse_pass_log_done(log_path: str, urls: list, mode: str = "queue", offset: int = 0) -> set:
    """Read an am-downloader log and return the set of adamIDs that COMPLETED.

    am-downloader splits its output into per-track segments:
      - ``mode="queue"``: ``Queue N of M:`` — one segment per URL passed
        (playlist songs + MVs, and album partial passes given song URLs)
      - ``mode="track"``: ``Track N of M:`` — one segment per album track,
        used when an album URL is expanded internally (the ``Queue 1 of 1:
        Album`` header line is NOT a song segment)
    A segment counts as done when it contains ``Decrypted`` (media written) or
    ``no codec found`` (Apple simply doesn't offer that format for the track,
    e.g. a legacy song in the Atmos pass — mark it done so it is never retried).
    Anything else (``Failed to dl``, auth errors, network errors) is left for a
    later run. ``urls`` must be the exact list am-downloader iterated for this
    pass — its segment index is the mapping key. ``offset`` lets songlist mode
    parse only the tail of an APPENDED multi-chunk log.
    """
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(offset)
            text = f.read()
    except OSError:
        return set()
    if mode == "track":
        delim = r"^\s*Track \d+ of \d+:"
    else:
        delim = r"^\s*Queue \d+ of \d+:"
    marks = [m.start() for m in re.finditer(delim, text, re.M)]
    done = set()
    for i, start in enumerate(marks):
        end = marks[i + 1] if i + 1 < len(marks) else len(text)
        seg = text[start:end]
        if "Decrypted" in seg or "no codec found" in seg:
            if i < len(urls):
                done.add(_am_url_adam_id(urls[i]))
    if len(marks) != len(urls):
        logging.warning(
            "AM per-song parse mismatch: log %s has %d segments for %d urls (mode=%s)",
            ospath.basename(log_path), len(marks), len(urls), mode,
        )
    return done


def _am_done_formats_for_urls(urls: list, album_id: str = "") -> dict:
    """Return {url: set(fmt_names already marked done in S3)} for the given URLs."""
    base = f"{S3_LOG_PREFIX}/album-{album_id}/song-" if album_id else f"{S3_LOG_PREFIX}/song-"
    keys = set(_am_scan_s3(base))
    pat = re.compile(
        rf"^{re.escape(base)}(?P<fmt>[a-z0-9-]+)-(?P<adam>\d+)\.log$"
    )
    done_by_adam = {}
    for k in keys:
        m = pat.match(k)
        if m:
            done_by_adam.setdefault(m.group("adam"), set()).add(m.group("fmt"))
    out = {}
    for u in urls:
        adam = _am_url_adam_id(u)
        if not adam:
            out.setdefault(u, set())
            continue
        out[u] = done_by_adam.get(adam, set())
    return out


def _am_mv_done_for_urls(urls: list, artist: bool = False) -> set:
    """Set of adamIDs whose MV is already marked done in S3."""
    prefix = "artist-mv" if artist else "mv"
    keys = set(_am_scan_s3(f"{S3_LOG_PREFIX}/{prefix}-song-"))
    pat = re.compile(rf"^{re.escape(S3_LOG_PREFIX)}/{prefix}-song-(\d+)\.log$")
    done = set()
    for k in keys:
        m = pat.match(k)
        if m:
            done.add(m.group(1))
    return done


async def am_download(urls: list, batch_no: int = 0, batch_total: int = 1):
    """Download ONE batch of songs in ALL formats into /music.

    For every format, only the songs that are still missing that format's
    per-song marker are passed to am-downloader — already-done tracks (from a
    previous, possibly interrupted run) are skipped so nothing is re-downloaded
    and re-uploaded. Returns ``(format_logs, markers)``, where markers is a
    list of ``(fmt, adamID, "")`` collected from each pass but NOT yet written
    to S3 — task_manager calls :func:`am_write_song_markers` with them AFTER
    the files are uploaded (download → upload → log append).
    """
    await _am_update_status(
        "<b>🎵 APPLE MUSIC » </b>\n⏳ __Preparing tools...__"
    )
    await ensure_am_tools()
    start_am_wrapper()
    _write_am_config()

    os.chdir(AM_MUSIC_PATH)

    suffix = "" if batch_total <= 1 else f"-batch{batch_no:02d}"
    done_map = _am_done_formats_for_urls(urls)
    results = []
    markers = []
    total = len(AM_FORMATS)
    for i, (name, extra_args, file_format) in enumerate(AM_FORMATS, start=1):
        fmt = name.lower()
        missing = [u for u in urls if fmt not in done_map.get(u, set())]
        if not missing:
            logging.info("AM skip %s pass for batch %d — all songs already done", name, batch_no)
            continue
        if batch_total > 1:
            head = f"<b>🎵 APPLE MUSIC » {name}</b>\n⏳ __Batch {batch_no}/{batch_total} — {len(missing)} missing songs in {name} ({i}/{total})...__"
        else:
            head = (
                f"<b>🎵 APPLE MUSIC » {name}</b>\n"
                f"⏳ __Downloading {len(missing)} songs in {name} format ({i}/{total})...__"
            )
        await _am_update_status(head)
        log_path = _run_am_pass(name, extra_args, missing, suffix, file_format)
        results.append((name, log_path))
        for adam in sorted(_am_parse_pass_log_done(log_path, missing)):
            if adam:
                markers.append((fmt, adam, ""))
        await asleep(2)

    return results, markers


def am_write_song_markers(markers):
    """Write collected per-song markers ``(fmt, adamID, album_id)`` to S3.

    task_manager calls this ONLY after the corresponding files were uploaded
    to Telegram (download → upload → log append), so a crash can never leave
    songs marked-done-but-never-uploaded.
    """
    if not S3_BUCKET_NAME or not markers:
        return
    try:
        from colab_leecher.uploader.s3 import ensure_s3_client
        from colab_leecher import S3_BUCKET_NAME as _bucket

        client = ensure_s3_client()
        n = 0
        for fmt, adam, album_id in markers:
            if not adam:
                continue
            if album_id:
                key = f"{S3_LOG_PREFIX}/album-{album_id}/song-{fmt}-{adam}.log"
            else:
                key = f"{S3_LOG_PREFIX}/song-{fmt}-{adam}.log"
            client.put_object(Bucket=_bucket, Key=key, Body=b"")
            n += 1
        if n:
            logging.info("AM wrote %d per-song markers to S3 (post-upload)", n)
    except Exception as e:
        logging.error(f"AM per-song marker write failed: {e}")


def am_write_mv_markers(adam_ids, artist: bool = False):
    """Write collected per-MV markers to S3 — called only after upload."""
    if not S3_BUCKET_NAME or not adam_ids:
        return
    try:
        from colab_leecher.uploader.s3 import ensure_s3_client
        from colab_leecher import S3_BUCKET_NAME as _bucket

        prefix = "artist-mv" if artist else "mv"
        client = ensure_s3_client()
        for adam in sorted(adam_ids):
            if adam:
                client.put_object(
                    Bucket=_bucket, Key=f"{S3_LOG_PREFIX}/{prefix}-song-{adam}.log", Body=b""
                )
        logging.info("AM wrote %d per-MV markers to S3 (post-upload)", len(adam_ids))
    except Exception as e:
        logging.error(f"AM per-MV marker write failed: {e}")


async def am_download_album(album_url: str, album_id: str, album_no: int = 1, album_total: int = 1):
    """Download ONE album in ALL formats into /music.

    am-downloader expands the album URL into its tracks itself, so a single
    pass covers the whole album. Only the album's songs still missing each
    format are downloaded (via per-song markers) so a crash mid-album only
    re-downloads the unfinished tracks. Returns ``(format_logs, markers)`` —
    markers are written to S3 by task_manager only AFTER upload
    (download → upload → log append).
    """
    await _am_update_status(
        "<b>🎵 APPLE MUSIC » </b>\n⏳ __Preparing tools...__"
    )
    await ensure_am_tools()
    start_am_wrapper()
    _write_am_config()

    os.chdir(AM_MUSIC_PATH)

    # Resolve the album's track URLs (amp-api keeps authoritative order).
    track_urls = fetch_album_tracks(album_url)
    if not track_urls:
        # Fall back to passing the album URL itself (am-downloader expands it).
        track_urls = [album_url]

    done_map = _am_done_formats_for_urls(track_urls, album_id=album_id)
    results = []
    markers = []
    total = len(AM_FORMATS)
    for i, (name, extra_args, file_format) in enumerate(AM_FORMATS, start=1):
        fmt = name.lower()
        missing = [u for u in track_urls if fmt not in done_map.get(u, set())]
        if not missing:
            logging.info("AM skip %s pass for album %s — all tracks already done", name, album_id)
            continue
        head = (
            f"<b>🎵 APPLE MUSIC » {name}</b>\n"
            f"⏳ __Album {album_no}/{album_total} ({album_id}) — {name} format "
            f"({i}/{total}), {len(missing)} missing tracks...__"
        )
        await _am_update_status(head)
        # Pass the album URL once when every track is still missing (preserves
        # am-downloader's artist/album folder layout); otherwise pass the
        # individual missing track URLs.
        if len(missing) == len(track_urls):
            pass_urls = [album_url]
        else:
            pass_urls = missing
        log_path = _run_am_pass(name, extra_args, pass_urls, "", file_format)
        results.append((name, log_path))
        # Collect markers using the url list + segment mode that match the log:
        # a full pass expands the album URL into Track N of M segments aligned
        # with track_urls; a partial pass logs Queue N of M aligned with
        # pass_urls (== the missing track URLs). They are written to S3 only
        # AFTER the files are uploaded (am_write_song_markers in task_manager).
        if len(pass_urls) == 1:
            mirror_urls, mode = track_urls, "track"
        else:
            mirror_urls, mode = pass_urls, "queue"
        for adam in sorted(_am_parse_pass_log_done(log_path, mirror_urls, mode=mode)):
            if adam:
                markers.append((fmt, adam, album_id))
        await asleep(2)

    return results, markers


def fetch_album_tracks(album_url: str) -> list:
    """Return per-track song URLs of an album, in track order.

    Uses amp-api ``/catalog/<cc>/albums/<id>/tracks`` (authoritative order,
    disc/track-number sorted); falls back to the public album page parser.
    """
    m = re.search(r"music\.apple\.com/(\w{2})/album/(?:[^/]+/)?(\d+)", album_url)
    if not m:
        return []
    cc, album_id = m.group(1), m.group(2)
    try:
        token = _am_amp_token()
        base = f"https://amp-api.music.apple.com/v1/catalog/{cc}/albums/{album_id}/tracks"
        items = _am_api_paginate(base, token, "data")
        # sort by (disc, track number) to mirror am-downloader's expansion order
        def track_key(it):
            a = it.get("attributes") or {}
            return (int(a.get("discNumber") or 1), int(a.get("trackNumber") or 0))
        items.sort(key=track_key)
        urls = []
        for it in items:
            a = it.get("attributes") or {}
            if a.get("url"):
                urls.append(a["url"])
        if urls:
            return urls
    except Exception as e:
        logging.warning(f"AM album tracks via amp-api failed ({e}) — using page parser")
    try:
        songs, _ = _parse_playlist_page(album_url)
        return songs
    except Exception as e:
        logging.warning(f"AM album tracks page parse failed ({e})")
        return []


# --- songlist mode (/amusic songs) ------------------------------------------
#
# A songlist file (e.g. /content/songlist.txt, generated from /root/links.txt)
# is a freeform list of songs, optionally grouped under album headers. This
# mode downloads the WHOLE list in every format, chunked into small
# am-downloader subprocess runs, and tracks progress in ONE single log file
# (music-logs/songlist-dedupe.log) — every completed track×format is appended
# as a line so a crash/resume never re-downloads done songs.


def fetch_songlist(path: str = AM_SONGLIST_PATH):
    """Parse a songlist file into ordered, globally-deduplicated song URLs.

    Format (as produced by the links.txt extractor):

        # comment line (ignored)
        Album Name (Year):          <- header line (ends in ':', display only)
          https://music.apple.com/in/song/<adamID>
          <adamID>                  <- bare adamID is also accepted

    Returns ``(song_urls, groups)`` where groups is ``[(header, count), ...]``
    for status display. Duplicate songs (same track on multiple albums) are
    kept only once, in first-seen order.
    """
    urls = []
    groups = []  # [(header, count)]
    current = None
    seen = set()
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.endswith(":") and "/" not in line:
                current = [line[:-1].strip(), 0]
                groups.append(current)
                continue
            if line.isdigit():
                line = f"https://music.apple.com/in/song/{line}"
            if "music.apple.com" not in line:
                logging.warning(f"AM songlist: skipping unrecognized line: {line!r}")
                continue
            adam = _am_url_adam_id(line)
            if not adam or adam in seen:
                continue
            seen.add(adam)
            urls.append(line)
            if current is not None:
                current[1] += 1
    logging.info(
        "AM songlist: %d unique songs in %d album group(s) from %s",
        len(urls), len(groups), path,
    )
    return urls, groups


def _am_songlist_dedupe_path() -> str:
    return ospath.join(AM_LOG_DIR, "songlist-dedupe.log")


def _am_songlist_dedupe_load():
    """Load the songlist resume state — ``(done, uploading)`` sets of
    ``(format, adamID)`` pairs.

    ``done`` = DONE lines (downloaded, uploaded, confirmed). ``uploading`` =
    write-ahead UPLOADING lines WITHOUT a DONE: the upload started in a
    previous run but was never confirmed — the next run re-verifies them
    against the chat history before re-uploading (see
    ``_am_filter_already_uploaded``).

    Sources the S3-mirrored log (remote state survives a Colab restart) and
    merges it with any local copy, then rewrites the local file deduplicated,
    so future appends stay minimal. A pair with both lines is DONE.
    """
    makedirs(AM_LOG_DIR, exist_ok=True)
    merged = ""
    if S3_BUCKET_NAME:
        try:
            from colab_leecher.uploader.s3 import ensure_s3_client

            body = (
                ensure_s3_client()
                .get_object(Bucket=S3_BUCKET_NAME, Key=AM_SONGLIST_DEDUPE_KEY)["Body"]
                .read()
            )
            merged += body.decode("utf-8", "ignore")
        except Exception as e:
            logging.info(f"AM songlist dedupe: no previous S3 log ({e})")
    local_path = _am_songlist_dedupe_path()
    if ospath.exists(local_path):
        with open(local_path, "r", encoding="utf-8", errors="ignore") as f:
            merged += "\n" + f.read()

    done, uploading = set(), set()
    with open(local_path, "w") as f:
        for line in merged.splitlines():
            m = re.match(r"^(DONE|UPLOADING) ([a-z0-9-]+) (\d+)$", line.strip())
            if not m:
                continue
            pair = (m.group(2), m.group(3))
            if m.group(1) == "DONE":
                done.add(pair)
            else:
                uploading.add(pair)
        for fmt, adam in sorted(done):
            f.write(f"DONE {fmt} {adam}\n")
        for fmt, adam in sorted(uploading - done):
            f.write(f"UPLOADING {fmt} {adam}\n")
    unconfirmed = uploading - done
    logging.info(
        "AM songlist dedupe: %d completions, %d unconfirmed uploads loaded",
        len(done),
        len(unconfirmed),
    )
    return done, unconfirmed


def _am_songlist_dedupe_write(kind: str, pairs: list):
    """Append ``<kind> <fmt> <adamID>`` lines to the dedupe log and mirror the
    whole file back to S3."""
    if not pairs:
        return
    makedirs(AM_LOG_DIR, exist_ok=True)
    local_path = _am_songlist_dedupe_path()
    with open(local_path, "a") as f:
        for fmt, adam in pairs:
            f.write(f"{kind} {fmt} {adam}\n")
    if S3_BUCKET_NAME:
        try:
            from colab_leecher.uploader.s3 import ensure_s3_client
            from colab_leecher import S3_BUCKET_NAME as _bucket

            with open(local_path, "rb") as f:
                ensure_s3_client().put_object(
                    Bucket=_bucket, Key=AM_SONGLIST_DEDUPE_KEY, Body=f.read()
                )
        except Exception as e:
            logging.error(f"AM songlist dedupe mirror failed: {e}")


def _am_songlist_dedupe_append(done_pairs):
    """Append DONE <fmt> <adamID> lines — a track is marked only AFTER its
    files reached Telegram — and mirror the whole log back to S3."""
    _am_songlist_dedupe_write("DONE", sorted(done_pairs))


def _am_songlist_dedupe_mark_uploading(pairs):
    """Write-ahead marker: append ``UPLOADING <fmt> <adamID>`` lines BEFORE the
    chunk's Telegram upload, and mirror to S3. If the runtime crashes mid-
    upload, the next run sees these as unconfirmed and verifies them against
    the chat history instead of blindly re-uploading."""
    _am_songlist_dedupe_write("UPLOADING", sorted(pairs))


def _am_history_confirmed_files(files: list, history_pairs: set) -> set:
    """Of local ``files``, the subset already present in a Telegram history
    snapshot of ``(file_name, file_size)`` pairs.

    Pure name+size match: am-downloader output is deterministic per song and
    format, so an identical file name AND byte size in the destination chat
    means the previous upload landed there.
    """
    confirmed = set()
    for f in files:
        try:
            size = ospath.getsize(f)
        except OSError:
            continue
        if (ospath.basename(f), size) in history_pairs:
            confirmed.add(f)
    return confirmed


async def _am_filter_already_uploaded(new_files: list) -> list:
    """Drop files that Telegram's own history proves were already uploaded.

    Scans the task chat (where AM files are sent, replying to the command
    message) for media with the same file name + byte size. This resolves
    UPLOADING markers left by a crash mid-upload: a file is re-uploaded ONLY
    when it is genuinely missing from the chat. Any scan failure degrades to
    "upload everything" (the pre-tracker behaviour).
    """
    if not new_files:
        return []
    try:
        from colab_leecher import colab_bot
        from colab_leecher.utility.variables import MSG

        chat_id = MSG.sent_msg.chat.id
    except Exception as e:
        logging.warning(f"AM history check skipped (no chat context): {e}")
        return new_files
    try:
        history = [
            m
            async for m in colab_bot.get_chat_history(
                chat_id, limit=max(len(new_files) * 4, 100)
            )
            if m.media and getattr(m, m.media.value, None)
        ]
    except Exception as e:
        logging.warning(
            f"AM history check failed — uploading all {len(new_files)} files: {e}"
        )
        return new_files
    seen = set()
    for m in history:
        media = getattr(m, m.media.value)
        name = getattr(media, "file_name", None)
        size = getattr(media, "file_size", None)
        if name and size:
            seen.add((name, size))
    confirmed = _am_history_confirmed_files(new_files, seen)
    if confirmed:
        logging.info(
            "AM history check: %d file(s) already on Telegram — skipping re-upload",
            len(confirmed),
        )
    return [f for f in new_files if f not in confirmed]


def _am_songlist_log_key(fmt: str) -> str:
    """S3 key of the appended am-downloader log for one songlist format pass."""
    return f"{S3_LOG_PREFIX}/songlist/{fmt}.log"


async def am_download_songlist(song_urls: list, on_new_files=None):
    """Download an arbitrary song list in ALL formats, chunk by chunk.

    Resume state lives in a SINGLE appended dedupe log mirroring
    ``music-logs/songlist-dedupe.log`` in S3: every completed track×format is
    a ``DONE <format> <adamID>`` line — but only AFTER its files were uploaded
    (download → upload → log append). Before each chunk's upload the write-
    ahead ``UPLOADING <format> <adamID>`` markers are mirrored too, so a crash
    mid-upload is verified against the chat history on the next boot instead
    of silently duplicating files. The am-downloader output of every
    chunk of the same format is appended into ONE format log
    (`` songlist/<fmt>-songlist.log ``). ``on_new_files(fmt, new_files)`` is
    awaited after every chunk (task manager uses it to upload/report) and must
    raise on upload failure — the chunk then stays unmarked and is retried.
    """
    await _am_update_status(
        "<b>🎵 APPLE MUSIC » </b>\n⏳ __Preparing tools...__"
    )
    await ensure_am_tools()
    start_am_wrapper()
    _write_am_config()

    os.chdir(AM_MUSIC_PATH)

    done, _unconfirmed = _am_songlist_dedupe_load()
    results = []  # [(format_name, appended_log_path)] — one entry per format
    total_fmt = len(AM_FORMATS)
    for i, (name, extra_args, file_format) in enumerate(AM_FORMATS, start=1):
        fmt = name.lower()
        missing = [u for u in song_urls if (fmt, _am_url_adam_id(u)) not in done]
        if not missing:
            logging.info("AM songlist skip %s — all songs already done", name)
            continue
        chunks = [missing[k : k + AM_SONGLIST_CHUNK] for k in range(0, len(missing), AM_SONGLIST_CHUNK)]
        log_path = ospath.join(AM_LOG_DIR, f"{fmt}-songlist.log")
        for ci, chunk in enumerate(chunks, start=1):
            head = (
                f"<b>🎵 APPLE MUSIC » SONGLIST {name}</b>\n"
                f"⏳ __Chunk {ci}/{len(chunks)} — {len(chunk)} of {len(missing)} "
                f"missing songs in {name} (format {i}/{total_fmt})...__"
            )
            await _am_update_status(head)
            # Capture the appended-file offset BEFORE this chunk runs, so the
            # log parser only inspects THIS chunk's segments. The per-format
            # log is ALWAYS appended (also across Colab restarts) so it stays
            # one single cumulative file, per the songlist design.
            offset = os.path.getsize(log_path) if ospath.exists(log_path) else 0
            before = _am_files_snapshot()
            _run_am_pass(
                name, extra_args, chunk, file_format=file_format,
                suffix="-songlist", append=True,
            )
            if ci == 1:
                results.append((name, log_path))
            chunk_done = _am_parse_pass_log_done(log_path, chunk, mode="queue", offset=offset)
            # am-downloader SKIPS tracks whose file is already on disk with
            # "Track already exists locally." — within the SAME Colab runtime
            # those files survived, but since the chunk was never uploaded
            # they must be uploaded now and only then marked done.
            extra_files = set()
            for adam, base in _am_parse_pass_log_skip_exists(log_path, chunk, offset=offset):
                found = _am_find_skip_files([base])
                if found:
                    chunk_done.add(adam)
                    extra_files |= found
            newly = {(fmt, a) for a in chunk_done if (fmt, a) not in done}
            # Mirror the appended per-format log too (best effort), so even a
            # crash leaves the full traceback of what was attempted.
            _am_songlist_mirror_log(fmt, log_path)
            # ORDER IS CRITICAL: 1) download (above) -> 2) write-ahead
            # UPLOADING markers -> 3) upload -> 4) dedupe DONE append. The
            # UPLOADING lines are mirrored to S3 BEFORE the upload starts, so
            # a crash mid-upload leaves an unconfirmed pair that the next run
            # verifies against the chat history instead of re-uploading blind.
            # A track is marked DONE only AFTER its files reached Telegram.
            if on_new_files is not None:
                new_files = sorted((_am_files_snapshot() - before) | extra_files)
                if new_files:
                    _am_songlist_dedupe_mark_uploading(newly)
                    await on_new_files(fmt, new_files)
            done |= newly
            _am_songlist_dedupe_append(newly)
            await asleep(1)
    return results


def _am_parse_pass_log_skip_exists(log_path: str, urls: list, offset: int = 0):
    """(adam_id, base_name) pairs of tracks am-downloader reported as already
    on disk ("Track already exists locally.") in the tail region of the log.

    Used by songlist mode: same-runtime restarts skip the download step, but
    those files were never uploaded, so they must be uploaded now and only
    then marked done.
    """
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(offset)
            text = f.read()
    except OSError:
        return []
    delim = r"^\s*Queue \d+ of \d+:"
    marks = [m.start() for m in re.finditer(delim, text, re.M)]
    pairs = []
    for i, start in enumerate(marks):
        end = marks[i + 1] if i + 1 < len(marks) else len(text)
        seg = text[start:end]
        if "Track already exists locally." in seg:
            lines = [l.strip() for l in seg.splitlines() if l.strip()]
            base = ""
            for j, l in enumerate(lines):
                if l == "Track already exists locally." and j > 0:
                    base = lines[j - 1]
            adam = _am_url_adam_id(urls[i]) if i < len(urls) else ""
            if adam and base:
                pairs.append((adam, base))
    return pairs


def _am_find_skip_files(skip_bases: list) -> set:
    """Glob for existing files matching am-downloader's skip-segment names."""
    import glob as _glob

    found = set()
    for base in skip_bases:
        base = base.strip()
        if not base:
            continue
        pat = ospath.join(AM_MUSIC_PATH, "**", base + ".m4a")
        found.update(_glob.glob(pat, recursive=True))
    return found


def _am_songlist_mirror_log(fmt: str, log_path: str):
    """Best-effort upload of the appended songlist-<fmt>.log to S3."""
    if not S3_BUCKET_NAME:
        return
    try:
        from colab_leecher.uploader.s3 import ensure_s3_client
        from colab_leecher import S3_BUCKET_NAME as _bucket

        ensure_s3_client().upload_file(log_path, _bucket, _am_songlist_log_key(fmt))
    except Exception as e:
        logging.error(f"AM songlist log mirror failed: {e}")


async def am_download_mvs(
    urls: list, batch_no: int = 0, batch_total: int = 1, artist: bool = False
):
    """Download ONE batch of music videos into /music.

    am-downloader picks the highest quality automatically from the config
    (mv-max: 2160, mv-audio-type: atmos). Only MVs still missing their
    per-song marker are passed to am-downloader, so an interrupted MV batch
    resumes where it stopped. Returns ``(log_path, done_adam_ids)`` — the
    caller mirrors the log and writes the markers (am_write_mv_markers) only
    AFTER the files reached Telegram (download → upload → log append).
    """
    await _am_update_status(
        "<b>🎵 APPLE MUSIC » </b>\n⏳ __Preparing tools...__"
    )
    await ensure_am_tools()
    start_am_wrapper()
    _write_am_config()

    os.chdir(AM_MUSIC_PATH)

    done = _am_mv_done_for_urls(urls, artist=artist)
    missing = [u for u in urls if _am_url_adam_id(u) not in done]
    if not missing:
        logging.info("AM skip MV batch %d — all videos already done", batch_no)
        return "", set()

    suffix = "" if batch_total <= 1 else f"-batch{batch_no:02d}"
    if batch_total > 1:
        head = (
            f"<b>🎵 APPLE MUSIC » {AM_MV_FORMAT}</b>\n"
            f"⏳ __Batch {batch_no}/{batch_total} — {len(missing)} missing music "
            f"videos at max quality...__"
        )
    else:
        head = (
            f"<b>🎵 APPLE MUSIC » {AM_MV_FORMAT}</b>\n"
            f"⏳ __Downloading {len(missing)} missing music videos at max quality...__"
        )
    await _am_update_status(head)
    log_path = _run_am_pass(AM_MV_FORMAT, [], missing, suffix)
    mv_done = _am_parse_pass_log_done(log_path, missing, mode="queue")
    await asleep(2)
    return log_path, mv_done
