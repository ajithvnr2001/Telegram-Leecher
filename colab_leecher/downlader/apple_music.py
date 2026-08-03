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


def is_am_playlist(url: str) -> bool:
    """True for https://music.apple.com/<cc>/playlist/<name>/<id> links."""
    return "music.apple.com" in url and "/playlist/" in url


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


def _am_log_key(name: str, suffix: str = "") -> str:
    """S3 object key for one format's download log.

    Mirrors the on-disk name (name.lower() + optional -batchNN suffix) so
    the resume scan can tie an S3 object back to a concrete batch/format.
    """
    return f"{S3_LOG_PREFIX}/{name.lower()}{suffix}.log"


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
    from colab_leecher.uploader.s3 import ensure_s3_client

    batch_to_formats = {}
    try:
        client = ensure_s3_client()
    except Exception as e:
        logging.error(f"AM resume: S3 client init failed ({e}) — will start from batch 1")
        return set()

    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=S3_BUCKET_NAME, Prefix=f"{S3_LOG_PREFIX}/"
        ):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                m = re.match(
                    rf"^{re.escape(S3_LOG_PREFIX)}/(.+?)(?:-batch(\d+))?\.log$",
                    key,
                )
                if not m:
                    continue
                fmt = m.group(1)
                batch = int(m.group(2)) if m.group(2) else 0
                batch_to_formats.setdefault(batch, set()).add(fmt)
    except Exception as e:
        logging.error(f"AM resume: S3 log scan failed ({e})")
        return set()

    required = {name.lower() for name, _, _ in AM_FORMATS}
    return {batch for batch, fmts in batch_to_formats.items() if required.issubset(fmts)}


def am_completed_mv_batches() -> set:
    """Batch numbers (int) of the MV pass already finished in a PREVIOUS run.

    The MV pass is a single run per batch (unlike the 5 audio formats), so a
    batch counts as completed when its own log ``music-logs/mv-batchNN.log``
    exists in S3.
    """
    from colab_leecher.uploader.s3 import ensure_s3_client

    done = set()
    try:
        client = ensure_s3_client()
    except Exception as e:
        logging.error(f"AM resume: S3 client init failed ({e}) — will start MV from batch 1")
        return done

    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=S3_BUCKET_NAME, Prefix=f"{S3_LOG_PREFIX}/"
        ):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                m = re.match(
                    rf"^{re.escape(S3_LOG_PREFIX)}/mv-batch(\d+)\.log$",
                    key,
                )
                if m:
                    done.add(int(m.group(1)))
    except Exception as e:
        logging.error(f"AM resume: S3 MV log scan failed ({e})")
        return set()
    return done


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


async def am_download_mvs(urls: list, batch_no: int = 0, batch_total: int = 1) -> str:
    """Download ONE batch of music videos into /music.

    am-downloader picks the highest quality automatically from the config
    (mv-max: 2160, mv-audio-type: atmos). Returns the log path for the S3
    mirror step. The media-user-token and mp4decrypt are both required —
    skip tracks the tool reports as unavailable, keep the ones it lands.
    """
    await _am_update_status(
        "<b>🎵 APPLE MUSIC » </b>\n⏳ __Preparing tools...__"
    )
    await ensure_am_tools()
    start_am_wrapper()
    _write_am_config()

    os.chdir(AM_MUSIC_PATH)

    suffix = "" if batch_total <= 1 else f"-batch{batch_no:02d}"
    if batch_total > 1:
        head = (
            f"<b>🎵 APPLE MUSIC » {AM_MV_FORMAT}</b>\n"
            f"⏳ __Batch {batch_no}/{batch_total} — {len(urls)} music videos at "
            f"max quality...__"
        )
    else:
        head = (
            f"<b>🎵 APPLE MUSIC » {AM_MV_FORMAT}</b>\n"
            f"⏳ __Downloading {len(urls)} music videos at max quality...__"
        )
    await _am_update_status(head)
    log_path = _run_am_pass(AM_MV_FORMAT, [], urls, suffix)
    await asleep(2)
    return log_path
