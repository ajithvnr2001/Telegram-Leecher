# copyright 2024 © Xron Trix | https://github.com/Xrontrix10


import pytz
import shutil
import logging
from time import time
from datetime import datetime
from asyncio import sleep
from os import makedirs, path as ospath, system
from colab_leecher import OWNER, colab_bot, DUMP_ID
from colab_leecher.downlader.manager import calDownSize, get_d_name, downloadManager
from colab_leecher.utility.helper import (
    getSize,
    applyCustomName,
    keyboard,
    sysINFO,
    is_google_drive,
    is_s3,
    is_telegram,
    is_ytdl_link,
    is_mega,
    is_terabox,
    is_torrent,
)
from colab_leecher.utility.handler import (
    Leech,
    Unzip_Handler,
    Zip_Handler,
    SendLogs,
    cancelTask,
    S3_Mirror_Handler,
)
from colab_leecher.utility.s3_iter import (
    is_multi_object_s3,
    iterate_s3_to_s3,
    iterate_s3_to_telegram,
)
from colab_leecher.utility.variables import (
    BOT,
    MSG,
    BotTimes,
    Messages,
    Paths,
    Aria2c,
    Transfer,
    TaskError,
)


def _should_iterate_s3(is_dir: bool) -> bool:
    """Return True iff the current task is a single multi-object S3 URI.

    Triggers the iterative whole-bucket pipeline (download → process →
    upload → cleanup → next) instead of the existing bulk pipeline.
    Only single-source tasks qualify; mixed sources keep the bulk flow.
    """
    if is_dir:
        return False
    if BOT.Mode.mode not in ("leech", "s3-mirror"):
        return False
    if len(BOT.SOURCE) != 1:
        return False
    src = BOT.SOURCE[0]
    if not is_s3(src):
        return False
    try:
        return is_multi_object_s3(src)
    except Exception as e:
        logging.warning(f"Falling back to bulk S3 flow ({e})")
        return False


async def task_starter(message, text):
    global BOT
    await message.delete()
    BOT.State.started = True
    if BOT.State.task_going == False:
        src_request_msg = await message.reply_text(text)
        return src_request_msg
    else:
        msg = await message.reply_text(
            "I am already working ! Please wait until I finish !!"
        )
        await sleep(15)
        await msg.delete()
        return None


async def taskScheduler():
    global BOT, MSG, BotTimes, Messages, Paths, Transfer, TaskError
    src_text = []
    is_dualzip, is_unzip, is_zip, is_dir = (
        BOT.Mode.type == "undzip",
        BOT.Mode.type == "unzip",
        BOT.Mode.type == "zip",
        BOT.Mode.mode == "dir-leech",
    )
    # Reset Texts
    Messages.download_name = ""
    Messages.task_msg = f"<b>🦞 TASK MODE » </b>"
    Messages.dump_task = (
        Messages.task_msg
        + f"<i>{BOT.Mode.type.capitalize()} {BOT.Mode.mode.capitalize()} as {BOT.Setting.stream_upload}</i>\n\n<b>🖇️ SOURCES » </b>"
    )
    Transfer.sent_file = []
    Transfer.sent_file_names = []
    Transfer.failed_files = []
    Transfer.down_bytes = [0, 0]
    Transfer.up_bytes = [0, 0]
    Messages.download_name = ""
    Messages.task_msg = ""
    Messages.status_head = f"<b>📥 DOWNLOADING » </b>\n"

    if is_dir:
        if not ospath.exists(BOT.SOURCE[0]):
            TaskError.state = True
            TaskError.text = "Task Failed. Because: Provided Directory Path Not Exists"
            logging.error(TaskError.text)
            return
        if not ospath.exists(Paths.temp_dirleech_path):
            makedirs(Paths.temp_dirleech_path)
        Messages.dump_task += f"\n\n📂 <code>{BOT.SOURCE[0]}</code>"
        Transfer.total_down_size = getSize(BOT.SOURCE[0])
        Messages.download_name = ospath.basename(BOT.SOURCE[0])
    elif BOT.Mode.mode == "am-music":
        from colab_leecher import AM_PLAYLIST_URL

        if not AM_PLAYLIST_URL:
            TaskError.state = True
            TaskError.text = "Task Failed. Because: AM_PLAYLIST_URL is not set"
            logging.error(TaskError.text)
            return
        Messages.dump_task += (
            f"\n\n🎵 <code>{AM_PLAYLIST_URL}</code>\n\n"
            "📀 <i>All formats (ALAC / Atmos / AAC-LC 256 / AAC 128 / HE-AAC 64) "
            "will be downloaded to /music and uploaded to Telegram.</i>"
        )
        BOT.SOURCE = [AM_PLAYLIST_URL]
    else:
        for link in BOT.SOURCE:
            if is_telegram(link):
                ida = "💬"
            elif is_google_drive(link):
                ida = "♻️"
            elif is_s3(link):
                ida = "☁️"
            elif is_torrent(link):
                ida = "🧲"
                Messages.caution_msg = "\n\n⚠️<i><b> Torrents Are Strictly Prohibited in Google Colab</b>, Try to avoid Magnets !</i>"
            elif is_ytdl_link(link):
                ida = "🏮"
            elif is_terabox(link):
                ida = "🍑"
            elif is_mega(link):
                ida = "💾"
            else:
                ida = "🔗"
            code_link = f"\n\n{ida} <code>{link}</code>"
            if len(Messages.dump_task + code_link) >= 4096:
                src_text.append(Messages.dump_task)
                Messages.dump_task = code_link
            else:
                Messages.dump_task += code_link

    # Get the current date and time in the specified time zone
    cdt = datetime.now(pytz.timezone("Asia/Kolkata"))
    dt = cdt.strftime(" %d-%m-%Y")
    Messages.dump_task += f"\n\n<b>📆 Task Date » </b><i>{dt}</i>"

    # Detect iterative whole-bucket / prefix S3 mode and annotate the
    # dump message so users see at a glance that this is a long-running
    # batch with crash-resume support.
    iterate_mode = _should_iterate_s3(is_dir)
    if iterate_mode:
        Messages.dump_task += (
            "\n\n<b>🔁 Iterative bucket mode » </b>"
            "<i>processing one object at a time, with S3-persisted tracker resume</i>"
        )

    src_text.append(Messages.dump_task)

    if ospath.exists(Paths.WORK_PATH):
        shutil.rmtree(Paths.WORK_PATH)
        # makedirs(Paths.WORK_PATH)
        makedirs(Paths.down_path)
    else:
        makedirs(Paths.WORK_PATH)
        makedirs(Paths.down_path)
    Messages.link_p = str(DUMP_ID)[4:]

    try:
        system(f"aria2c -d {Paths.WORK_PATH} -o Hero.jpg {Aria2c.pic_dwn_url}")
    except Exception:
        Paths.HERO_IMAGE = Paths.DEFAULT_HERO

    MSG.sent_msg = await colab_bot.send_message(chat_id=DUMP_ID, text=src_text[0])

    if len(src_text) > 1:
        for lin in range(1, len(src_text)):
            MSG.sent_msg = await MSG.sent_msg.reply_text(text=src_text[lin], quote=True)

    Messages.src_link = f"https://t.me/c/{Messages.link_p}/{MSG.sent_msg.id}"
    Messages.task_msg += f"__[{BOT.Mode.type.capitalize()} {BOT.Mode.mode.capitalize()} as {BOT.Setting.stream_upload}]({Messages.src_link})__\n\n"

    await MSG.status_msg.delete()
    img = Paths.THMB_PATH if ospath.exists(Paths.THMB_PATH) else Paths.HERO_IMAGE
    MSG.status_msg = await colab_bot.send_photo(  # type: ignore
        chat_id=OWNER,
        photo=img,
        caption=Messages.task_msg
        + Messages.status_head
        + f"\n📝 __Starting DOWNLOAD...__"
        + sysINFO(),
        reply_markup=keyboard(),
    )

    # Iterative whole-bucket / prefix mode: skip the bulk pipeline
    # (calDownSize → get_d_name → downloadManager → Do_Leech/Do_*_Mirror)
    # and dispatch to the per-object handler instead. Each iteration
    # downloads one object, runs the user-selected pipeline (Regular /
    # Compress / Extract / UnDoubleZip — including the >2 GB sizeChecker
    # split for Telegram destinations), uploads, then deletes the local
    # files and moves to the next object. The tracker is persisted to
    # S3 after every entry so a Colab crash can be resumed by re-running
    # the same command.
    if iterate_mode:
        BotTimes.current_time = time()
        if BOT.Mode.mode == "leech":
            await iterate_s3_to_telegram(BOT.SOURCE[0], is_zip, is_unzip, is_dualzip)
            await SendLogs(True)
        else:  # s3-mirror — also validate destination is configured
            from colab_leecher.uploader.s3 import is_s3_configured

            if not is_s3_configured():
                await cancelTask(
                    "S3 is NOT CONFIGURED ! Set S3_ACCESS_KEY, S3_SECRET_KEY and "
                    "S3_BUCKET_NAME in the Colab cell, restart the bot and try again."
                )
                return
            await iterate_s3_to_s3(BOT.SOURCE[0], is_zip, is_unzip, is_dualzip)
            await SendLogs(False)
        return

    if BOT.Mode.mode != "am-music":
        await calDownSize(BOT.SOURCE)

        if not is_dir:
            await get_d_name(BOT.SOURCE[0])
        else:
            Messages.download_name = ospath.basename(BOT.SOURCE[0])
    else:
        from colab_leecher import AM_PLAYLIST_URL

        Messages.download_name = "Apple Music Playlist"
        _ = AM_PLAYLIST_URL

    if is_zip:
        Paths.down_path = ospath.join(Paths.down_path, Messages.download_name)
        if not ospath.exists(Paths.down_path):
            makedirs(Paths.down_path)

    BotTimes.current_time = time()

    if BOT.Mode.mode == "mirror":
        await Do_Mirror(BOT.SOURCE, BOT.Mode.ytdl, is_zip, is_unzip, is_dualzip)
    elif BOT.Mode.mode == "s3-mirror":
        await Do_S3_Mirror(BOT.SOURCE, BOT.Mode.ytdl, is_zip, is_unzip, is_dualzip)
    elif BOT.Mode.mode == "am-music":
        await Do_AM_Music(is_zip, is_unzip, is_dualzip)
    else:
        await Do_Leech(BOT.SOURCE, is_dir, BOT.Mode.ytdl, is_zip, is_unzip, is_dualzip)

async def Do_AM_Music(is_zip, is_unzip, is_dualzip):
    """Download the predefined Apple Music playlist in batches of 5 songs.

    For every batch: download all 5 formats into /music, upload the new
    tracks to Telegram, mirror the format logs to S3, then move to the
    next batch until the whole playlist is done.
    """
    from colab_leecher import AM_PLAYLIST_URL
    from colab_leecher.downlader.apple_music import (
        AM_MUSIC_PATH,
        AM_LOG_DIR,
        S3_LOG_PREFIX,
        am_download,
        am_completed_batches,
        fetch_playlist_songs,
        _am_files_snapshot,
        _am_log_key,
    )
    from colab_leecher.utility.handler import Leech

    Messages.download_name = "Apple Music Playlist"

    # 1) Resolve the full track list
    await MSG.status_msg.edit_text(
        text=Messages.task_msg
        + "<b>🎵 APPLE MUSIC » </b>\n⏳ __Reading playlist...__",
        reply_markup=keyboard(),
    )
    songs = fetch_playlist_songs(AM_PLAYLIST_URL)
    total_songs = len(songs)
    batch_size = 5
    batches = [
        songs[i : i + batch_size] for i in range(0, total_songs, batch_size)
    ]
    total_batches = len(batches)
    logging.info("AM task: %d songs -> %d batches of %d", total_songs, total_batches, batch_size)

    # 1b) Crash-resume: skip batches whose download logs are already in S3.
    #     Presence of ALL five format logs for a batch means a previous run
    #     finished that batch, so we must NOT download it again.
    completed = am_completed_batches()
    if completed:
        n_skip = sum(1 for b in range(1, total_batches + 1) if b in completed)
        logging.info("AM resume: skipping %d already-completed batch(es) found in S3", n_skip)
        await MSG.status_msg.edit_text(
            text=Messages.task_msg
            + f"<b>🎵 APPLE MUSIC » </b>\n🔁 <i>Resuming — {n_skip} batch(es) already done"
            f" (0/{total_batches} remaining).</i>",
            reply_markup=keyboard(),
        )

    done_files = set()
    for batch_no, batch in enumerate(batches, start=1):
        if batch_no in completed:
            logging.info("AM skip batch %d (already completed)", batch_no)
            continue
        before = _am_files_snapshot()

        # 2) Download this batch in ALL formats
        format_logs = await am_download(batch, batch_no, total_batches)

        # 3) Mirror each format's log to S3 for tracking (best effort)
        try:
            from colab_leecher.uploader.s3 import ensure_s3_client
            from colab_leecher import S3_BUCKET_NAME

            if S3_BUCKET_NAME:
                ensure_s3_client()
                for name, log_path in format_logs:
                    key = _am_log_key(name, suffix=f"-batch{batch_no:02d}")
                    ensure_s3_client().upload_file(log_path, S3_BUCKET_NAME, key)
                    logging.info("AM log %s mirrored to s3://%s/%s", name, S3_BUCKET_NAME, key)
        except Exception as e:
            logging.error(f"AM log mirror to S3 failed: {e}")

        # 4) Upload only the tracks created by this batch
        new_files = sorted(_am_files_snapshot() - before)
        done_files.update(new_files)
        if not new_files:
            logging.warning("Batch %d produced no new files — skipping upload", batch_no)
            continue

        await MSG.status_msg.edit_text(
            text=Messages.task_msg
            + f"<b>🎵 APPLE MUSIC » </b>\n📤 __Uploading batch {batch_no}/{total_batches} "
            f"({len(new_files)} files)...__",
            reply_markup=keyboard(),
        )
        Paths.down_path = AM_MUSIC_PATH
        for f in new_files:
            await Leech(ospath.dirname(f), False)

        logging.info("Batch %d/%d done — %d files uploaded", batch_no, total_batches, len(new_files))

    Transfer.total_down_size = getSize(AM_MUSIC_PATH)

    await MSG.status_msg.edit_text(
        text=Messages.task_msg + "<b>🎵 APPLE MUSIC » </b>\n✅ __All batches finished.__",
        reply_markup=keyboard(),
    )
    await SendLogs(True)

async def Do_Leech(source, is_dir, is_ytdl, is_zip, is_unzip, is_dualzip):
    if is_dir:
        for s in source:
            if not ospath.exists(s):
                logging.error("Provided directory does not exist !")
                await cancelTask("Provided directory does not exist !")
                return
            Paths.down_path = s
            if is_zip:
                await Zip_Handler(Paths.down_path, True, False)
                await Leech(Paths.temp_zpath, True)
            elif is_unzip:
                await Unzip_Handler(Paths.down_path, False)
                await Leech(Paths.temp_unzip_path, True)
            elif is_dualzip:
                await Unzip_Handler(Paths.down_path, False)
                await Zip_Handler(Paths.temp_unzip_path, True, True)
                await Leech(Paths.temp_zpath, True)
            else:
                if ospath.isdir(s):
                    await Leech(Paths.down_path, False)
                else:
                    Transfer.total_down_size = ospath.getsize(s)
                    makedirs(Paths.temp_dirleech_path)
                    shutil.copy(s, Paths.temp_dirleech_path)
                    Messages.download_name = ospath.basename(s)
                    await Leech(Paths.temp_dirleech_path, True)
    else:
        await downloadManager(source, is_ytdl)

        Transfer.total_down_size = getSize(Paths.down_path)

        # Renaming Files With Custom Name
        applyCustomName()

        # Preparing To Upload
        if is_zip:
            await Zip_Handler(Paths.down_path, True, True)
            await Leech(Paths.temp_zpath, True)
        elif is_unzip:
            await Unzip_Handler(Paths.down_path, True)
            await Leech(Paths.temp_unzip_path, True)
        elif is_dualzip:
            print("Got into un doubled zip")
            await Unzip_Handler(Paths.down_path, True)
            await Zip_Handler(Paths.temp_unzip_path, True, True)
            await Leech(Paths.temp_zpath, True)
        else:
            await Leech(Paths.down_path, True)

    await SendLogs(True)


async def Do_Mirror(source, is_ytdl, is_zip, is_unzip, is_dualzip):
    if not ospath.exists(Paths.MOUNTED_DRIVE):
        await cancelTask(
            "Google Drive is NOT MOUNTED ! Stop the Bot and Run the Google Drive Cell to Mount, then Try again !"
        )
        return

    if not ospath.exists(Paths.mirror_dir):
        makedirs(Paths.mirror_dir)

    await downloadManager(source, is_ytdl)

    Transfer.total_down_size = getSize(Paths.down_path)

    applyCustomName()

    cdt = datetime.now()
    cdt_ = cdt.strftime("Uploaded » %Y-%m-%d %H:%M:%S")
    mirror_dir_ = ospath.join(Paths.mirror_dir, cdt_)

    if is_zip:
        await Zip_Handler(Paths.down_path, True, True)
        shutil.copytree(Paths.temp_zpath, mirror_dir_)
    elif is_unzip:
        await Unzip_Handler(Paths.down_path, True)
        shutil.copytree(Paths.temp_unzip_path, mirror_dir_)
    elif is_dualzip:
        await Unzip_Handler(Paths.down_path, True)
        await Zip_Handler(Paths.temp_unzip_path, True, True)
        shutil.copytree(Paths.temp_zpath, mirror_dir_)
    else:
        shutil.copytree(Paths.down_path, mirror_dir_)

    await SendLogs(False)



async def Do_S3_Mirror(source, is_ytdl, is_zip, is_unzip, is_dualzip):
    """Mirror downloaded sources to a configurable S3 bucket.

    Mirrors `Do_Mirror` (Google Drive) but the destination is S3.
    Supports the full set of options exposed by other commands:
    Regular / Compress (zip) / Extract (unzip) / UnDoubleZip, plus the
    >2 GB pipeline (split or zip-split) when applicable upstream.
    """
    from colab_leecher.uploader.s3 import is_s3_configured

    if not is_s3_configured():
        await cancelTask(
            "S3 is NOT CONFIGURED ! Set S3_ACCESS_KEY, S3_SECRET_KEY and S3_BUCKET_NAME in the Colab cell, restart the bot and try again."
        )
        return

    await downloadManager(source, is_ytdl)

    Transfer.total_down_size = getSize(Paths.down_path)

    applyCustomName()

    if is_zip:
        await Zip_Handler(Paths.down_path, True, True)
        await S3_Mirror_Handler(Paths.temp_zpath, True)
    elif is_unzip:
        await Unzip_Handler(Paths.down_path, True)
        await S3_Mirror_Handler(Paths.temp_unzip_path, True)
    elif is_dualzip:
        await Unzip_Handler(Paths.down_path, True)
        await Zip_Handler(Paths.temp_unzip_path, True, True)
        await S3_Mirror_Handler(Paths.temp_zpath, True)
    else:
        await S3_Mirror_Handler(Paths.down_path, True)

    await SendLogs(False)
