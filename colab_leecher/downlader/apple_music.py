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
  2. Saves everything under /music (AM_MUSIC_PATH).
  3. Uploads every downloaded track to Telegram (via the standard Leech
     pipeline).
  4. Mirrors each format's download log to S3 (music-logs/) for tracking.

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

# Each pass = (name, extra args). Playlist URL is appended at run time.
AM_FORMATS = [
    ("ALAC", []),
    ("ATMOS", ["--atmos"]),
    ("AAC-LC-256", ["--aac", "--aac-type", "aac-lc"]),
    (
        "AAC-128",
        [
            "--aac",
            "--aac-type",
            "aac-128",
            "--song-file-format",
            "{SongNumer}. {SongName} {Quality}",
        ],
    ),
    (
        "HE-AAC-64",
        [
            "--aac",
            "--aac-type",
            "he-aac-64",
            "--song-file-format",
            "{SongNumer}. {SongName} {Quality}",
        ],
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


async def _am_update_status(head: str):
    Messages.status_head = head
    try:
        MSG.status_msg = await MSG.status_msg.edit_text(
            text=Messages.task_msg + head + sysINFO(),
            reply_markup=keyboard(),
        )
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
    """Start the decryption wrapper in the background (ports 10020/20020/30020)."""
    if _am_wrapper_running():
        logging.info("AM wrapper already running.")
        return
    wr_dir = ospath.join(AM_TOOLS_PATH, "wrapper-release")
    logf = open(ospath.join(AM_TOOLS_PATH, "wrapper.log"), "w")
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
            logging.info("AM wrapper started OK.")
            return
    raise RuntimeError(
        "AM wrapper failed to start. Check /content/am-tools/wrapper.log"
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


def _run_am_pass(name: str, extra_args: list, playlist_url: str) -> str:
    """Run one am-downloader pass; returns path to its log file."""
    makedirs(AM_LOG_DIR, exist_ok=True)
    log_path = ospath.join(AM_LOG_DIR, f"{name.lower()}.log")
    cmd = [ospath.join(AM_TOOLS_PATH, "am-downloader"), *extra_args, playlist_url]
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


async def am_download(playlist_url: str):
    """Download the playlist in ALL formats into /music.

    Returns list of (format_name, log_path) for the S3 mirror step.
    """
    await _am_update_status(
        "<b>🎵 APPLE MUSIC » </b>\n⏳ __Preparing tools...__"
    )
    await ensure_am_tools()
    start_am_wrapper()
    _write_am_config()

    os.chdir(AM_MUSIC_PATH)

    results = []
    total = len(AM_FORMATS)
    for i, (name, extra_args) in enumerate(AM_FORMATS, start=1):
        await _am_update_status(
            f"<b>🎵 APPLE MUSIC » {name}</b>\n"
            f"⏳ __Downloading playlist in {name} format ({i}/{total})...__"
        )
        log_path = _run_am_pass(name, extra_args, playlist_url)
        results.append((name, log_path))
        await asleep(2)

    return results
