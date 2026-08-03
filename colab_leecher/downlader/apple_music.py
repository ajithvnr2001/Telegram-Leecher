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
     Music Videos are skipped entirely (matching the process rules).
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

AM_LOG_DIR = ospath.join(AM_MUSIC_PATH, "am-logs")
AM_CONFIG_PATH = ospath.join(AM_MUSIC_PATH, "config.yaml")
AM_WRAPPER_CMD = "./wrapper -H 0.0.0.0 -B rootfs/data/data/com.apple.android.music/files"
AM_WRAPPER_ENV = {"AM_BIND_PROC": "1", "AM_NO_PIDNS": "1"}


def is_am_playlist(url: str) -> bool:
    """True for https://music.apple.com/<cc>/playlist/<name>/<id> links."""
    return "music.apple.com" in url and "/playlist/" in url


def _am_tools_ready() -> bool:
    return ospath.exists(ospath.join(AM_TOOLS_PATH, "am-downloader")) and ospath.exists(
        ospath.join(AM_TOOLS_PATH, "wrapper-release", "wrapper")
    )


def fetch_playlist_songs(playlist_url: str, limit: int = 0) -> list:
    """Return per-song download URLs for an Apple Music playlist.

    Parses the public playlist page (serialized-server-data) — no auth needed.
    am-downloader accepts these /song/ URLs directly.
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
    songs = []
    try:
        sections = data["data"][0]["data"]["sections"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("Unexpected playlist page structure")
    for sec in sections:
        if "track" not in str(sec.get("id", "")):
            continue
        for item in sec.get("items", []):
            cd = item.get("contentDescriptor") or {}
            if cd.get("kind") == "song" and (cd.get("identifiers") or {}).get("storeAdamID"):
                songs.append(
                    f"https://music.apple.com/in/song/{(cd['identifiers'])['storeAdamID']}"
                )
            elif cd.get("url") and "song" in cd.get("url", ""):
                songs.append(cd["url"])
    if not songs:
        raise RuntimeError("No songs found in playlist page")
    logging.info("AM playlist has %d songs", len(songs))
    return songs[:limit] if limit else songs


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


def _download_tool(client, key: str, dest: str):
    """Download a tool from S3 (credentials.json bucket) or public URL fallback."""
    if client is not None:
        try:
            client.download_file(S3_BUCKET_NAME, key, dest)
            if ospath.exists(dest):
                return
        except Exception as e:
            logging.error(f"S3 tool download failed for {key}: {e}")
    url = (
        AM_TOOL_WRAPPER_URL if key.endswith("wrapper-release.tar.gz") else AM_TOOL_DOWNLOADER_URL
    )
    _download_file(url, dest)


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

    logging.info("AM tools ready.")


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
    (AM_MEDIA_TOKEN, set in the Colab notebook).
    """
    from colab_leecher import AM_MEDIA_TOKEN

    cfg = f"""media-user-token: "{AM_MEDIA_TOKEN}"
authorization-token: ""
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
exit-on-error: false
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


def _run_am_pass(name: str, extra_args: list, urls: list, suffix: str = "", file_format: str = "") -> str:
    """Run one am-downloader pass over a list of song URLs.

    Returns path to its log file.
    """
    makedirs(AM_LOG_DIR, exist_ok=True)
    log_name = f"{name.lower()}{suffix}.log"
    log_path = ospath.join(AM_LOG_DIR, log_name)
    cmd = [ospath.join(AM_TOOLS_PATH, "am-downloader"), *extra_args]
    if file_format:
        cmd += ["--song-file-format", file_format]
    cmd += urls
    with open(log_path, "w") as logf:
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
    """Absolute paths of every file currently under /music."""
    out = set()
    for root, _dirs, files in os.walk(AM_MUSIC_PATH):
        for f in files:
            out.add(ospath.join(root, f))
    return out


async def am_download(urls: list, batch_no: int = 0, batch_total: int = 1):
    """Download ONE batch of songs in ALL formats into /music.

    Returns list of (format_name, log_path) for the S3 mirror step.
    """
    await _am_update_status(
        "<b>🎵 APPLE MUSIC » </b>\n⏳ __Preparing tools...__"
    )
    await ensure_am_tools()
    start_am_wrapper()
    _write_am_config()

    os.chdir(AM_MUSIC_PATH)

    suffix = "" if batch_total <= 1 else f"-batch{batch_no:02d}"
    results = []
    total = len(AM_FORMATS)
    for i, (name, extra_args, file_format) in enumerate(AM_FORMATS, start=1):
        if batch_total > 1:
            head = f"<b>🎵 APPLE MUSIC » {name}</b>\n⏳ __Batch {batch_no}/{batch_total} — {len(urls)} songs in {name} format ({i}/{total})...__"
        else:
            head = (
                f"<b>🎵 APPLE MUSIC » {name}</b>\n"
                f"⏳ __Downloading in {name} format ({i}/{total})...__"
            )
        await _am_update_status(head)
        log_path = _run_am_pass(name, extra_args, urls, suffix, file_format)
        results.append((name, log_path))
        await asleep(2)

    return results
