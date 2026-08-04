# copyright 2024 © Xron Trix | https://github.com/Xrontrix10


import pytz
import re
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
        from colab_leecher import AM_PLAYLIST_URL, AM_ARTIST_URL

        if not AM_PLAYLIST_URL and not AM_ARTIST_URL:
            TaskError.state = True
            TaskError.text = "Task Failed. Because: AM_PLAYLIST_URL and AM_ARTIST_URL are both unset"
            logging.error(TaskError.text)
            return
        am_src = "\n".join(
            f"🎵 <code>{u}</code>"
            for u in (AM_PLAYLIST_URL, AM_ARTIST_URL)
            if u
        )
        Messages.dump_task += (
            f"\n\n{am_src}\n\n"
            "💾 <i>LOCAL SAVE — files stay on the Colab disk, nothing is "
            "uploaded to Telegram.</i>"
            if BOT.Mode.am_local
            else f"\n\n{am_src}\n\n"
            "📀 <i>All formats (ALAC / Atmos / AAC-LC 256 / AAC 128 / HE-AAC 64) "
            "will be downloaded to /music and uploaded to Telegram.</i>"
        )
        BOT.SOURCE = [u for u in (AM_PLAYLIST_URL, AM_ARTIST_URL) if u]
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

    if BOT.Mode.mode not in ("am-music", "am-songlist"):
        await calDownSize(BOT.SOURCE)

        if not is_dir:
            await get_d_name(BOT.SOURCE[0])
        else:
            Messages.download_name = ospath.basename(BOT.SOURCE[0])
    else:
        from colab_leecher import AM_PLAYLIST_URL, AM_ARTIST_URL

        Messages.download_name = "Apple Music Playlist"
        _ = (AM_PLAYLIST_URL, AM_ARTIST_URL)

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
        from colab_leecher import AM_PLAYLIST_URL, AM_ARTIST_URL
        from colab_leecher.downlader import apple_music as am_mod

        # Both sources may be configured at once; each runs as its own pass and
        # keeps a separate S3 log keyspace, so resumes never cross-contaminate.
        playlist_url = AM_PLAYLIST_URL if am_mod.is_am_playlist(AM_PLAYLIST_URL) else ""
        artist_url = AM_ARTIST_URL if am_mod.is_am_artist(AM_ARTIST_URL) else (
            # Back-compat: an artist link left in AM_PLAYLIST_URL still routes
            # to artist mode when no dedicated AM_ARTIST_URL is set.
            AM_PLAYLIST_URL if am_mod.is_am_artist(AM_PLAYLIST_URL) else ""
        )

        if not playlist_url and not artist_url:
            TaskError.state = True
            TaskError.text = "Task Failed. Because: AM_PLAYLIST_URL and AM_ARTIST_URL are both unset"
            logging.error(TaskError.text)
            return
        if playlist_url:
            await Do_AM_Music(playlist_url, is_zip, is_unzip, is_dualzip)
        if artist_url:
            await Do_AM_Artist(artist_url, is_zip, is_unzip, is_dualzip)
    elif BOT.Mode.mode == "am-songlist":
        await Do_AM_Songlist(is_zip, is_unzip, is_dualzip)
    else:
        await Do_Leech(BOT.SOURCE, is_dir, BOT.Mode.ytdl, is_zip, is_unzip, is_dualzip)

async def Do_AM_Music(am_url, is_zip, is_unzip, is_dualzip):
    """Download the predefined Apple Music playlist in batches of 5 songs.

    For every batch: download all 5 formats into /music, upload the new
    tracks to Telegram, mirror the format logs to S3, then move to the
    next batch until the whole playlist is done. Music videos (if any)
    are downloaded afterwards, in their own batches, at max quality.
    """
    from colab_leecher.downlader import apple_music as am_mod
    from colab_leecher.utility.handler import Leech

    Messages.download_name = "Apple Music Playlist"
    local = bool(BOT.Mode.am_local)
    if local:
        # /amusic local → download to Colab disk instead of /music, and
        # skip the Telegram upload step entirely.
        am_mod.set_am_music_path(am_mod.AM_LOCAL_MUSIC_PATH)

    # 1) Resolve the full track list
    await MSG.status_msg.edit_text(
        text=Messages.task_msg
        + "<b>🎵 APPLE MUSIC » </b>\n⏳ __Reading playlist...__",
        reply_markup=keyboard(),
    )
    songs = am_mod.fetch_playlist_songs(am_url)
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
    completed = am_mod.am_completed_batches()
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
    n_fmts = len(am_mod.AM_FORMATS)
    for batch_no, batch in enumerate(batches, start=1):
        if batch_no in completed:
            # Coarse check passed, but the per-song markers are authoritative:
            # if any song still misses a format (failed in a previous run),
            # re-run the batch — am_download only passes the missing songs.
            done_map = am_mod._am_done_formats_for_urls(batch)
            if all(
                len(done_map.get(u, set())) >= n_fmts for u in batch
            ):
                logging.info("AM skip batch %d (already completed)", batch_no)
                continue
            logging.info("AM batch %d logs present but some songs lack formats — resuming", batch_no)
        before = am_mod._am_files_snapshot()

        # 2) Download this batch in ALL formats
        format_logs, pending_markers = await am_mod.am_download(batch, batch_no, total_batches)

        # 3) Upload only the tracks created by this batch (skip in local mode)
        new_files = sorted(am_mod._am_files_snapshot() - before)
        done_files.update(new_files)
        if not new_files:
            logging.warning("Batch %d produced no new files — nothing to upload", batch_no)
        elif local:
            await MSG.status_msg.edit_text(
                text=Messages.task_msg
                + f"<b>🎵 APPLE MUSIC » </b>\n💾 __Saved batch {batch_no}/{total_batches} "
                f"locally — {len(new_files)} files under <code>{am_mod.AM_MUSIC_PATH}</code>__",
                reply_markup=keyboard(),
            )
            logging.info("Batch %d/%d done — %d files saved locally", batch_no, total_batches, len(new_files))
        else:
            await MSG.status_msg.edit_text(
                text=Messages.task_msg
                + f"<b>🎵 APPLE MUSIC » </b>\n📤 __Uploading batch {batch_no}/{total_batches} "
                f"({len(new_files)} files)...__",
                reply_markup=keyboard(),
            )
            Paths.down_path = am_mod.AM_MUSIC_PATH
            for f in new_files:
                await Leech(ospath.dirname(f), False)
            logging.info("Batch %d/%d done — %d files uploaded", batch_no, total_batches, len(new_files))

        # 4) AFTER upload: mirror each format's log + write per-song markers
        #    to S3 (best effort). Order guarantees no song is marked done
        #    that was never uploaded.
        try:
            from colab_leecher.uploader.s3 import ensure_s3_client
            from colab_leecher import S3_BUCKET_NAME

            if S3_BUCKET_NAME:
                ensure_s3_client()
                for name, log_path in format_logs:
                    key = am_mod._am_log_key(name, suffix=f"-batch{batch_no:02d}")
                    ensure_s3_client().upload_file(log_path, S3_BUCKET_NAME, key)
                    logging.info("AM log %s mirrored to s3://%s/%s", name, S3_BUCKET_NAME, key)
        except Exception as e:
            logging.error(f"AM log mirror to S3 failed: {e}")
        am_mod.am_write_song_markers(pending_markers)

    # 5) Music videos — download at max quality in their own batches, resume
    #    off the mv-batchNN.log mirrors in S3.
    try:
        mvs = am_mod.fetch_playlist_mvs(am_url)
    except Exception as e:
        logging.warning(f"AM MV list failed ({e}) — skipping MV pass")
        mvs = []
    if mvs:
        mv_batches = [mvs[i : i + batch_size] for i in range(0, len(mvs), batch_size)]
        mv_total = len(mv_batches)
        logging.info("AM task: %d music videos -> %d batches of %d", len(mvs), mv_total, batch_size)
        completed_mv = am_mod.am_completed_mv_batches()
        for mv_no, mv_batch in enumerate(mv_batches, start=1):
            if mv_no in completed_mv:
                # Per-MV markers are authoritative; only skip if every video
                # really downloaded (failed ones get retried).
                done = am_mod._am_mv_done_for_urls(mv_batch)
                if all(am_mod._am_url_adam_id(u) in done for u in mv_batch):
                    logging.info("AM skip MV batch %d (already completed)", mv_no)
                    continue
                logging.info("AM MV batch %d logs present but some videos failed — resuming", mv_no)
            before = am_mod._am_files_snapshot()
            mv_log, mv_done_ids = await am_mod.am_download_mvs(mv_batch, mv_no, mv_total)

            new_files = sorted(am_mod._am_files_snapshot() - before)
            done_files.update(new_files)
            if not new_files:
                logging.warning("MV batch %d produced no new files — nothing to upload", mv_no)
            elif local:
                await MSG.status_msg.edit_text(
                    text=Messages.task_msg
                    + f"<b>🎵 APPLE MUSIC » {am_mod.AM_MV_FORMAT}</b>\n"
                    f"💾 __Saved MV batch {mv_no}/{mv_total} locally — "
                    f"{len(new_files)} files under <code>{am_mod.AM_MUSIC_PATH}</code>__",
                    reply_markup=keyboard(),
                )
                logging.info("MV batch %d/%d done — %d files saved locally", mv_no, mv_total, len(new_files))
            else:
                await MSG.status_msg.edit_text(
                    text=Messages.task_msg
                    + f"<b>🎵 APPLE MUSIC » {am_mod.AM_MV_FORMAT}</b>\n"
                    f"📤 __Uploading MV batch {mv_no}/{mv_total} ({len(new_files)} files)...__",
                    reply_markup=keyboard(),
                )
                Paths.down_path = am_mod.AM_MUSIC_PATH
                for f in new_files:
                    await Leech(ospath.dirname(f), False)
                logging.info("MV batch %d/%d done — %d files uploaded", mv_no, mv_total, len(new_files))

            # AFTER upload: mirror the batch log + write per-MV markers.
            try:
                from colab_leecher.uploader.s3 import ensure_s3_client
                from colab_leecher import S3_BUCKET_NAME

                if S3_BUCKET_NAME and mv_log:
                    key = am_mod._am_mv_log_key(mv_no, artist=False)
                    ensure_s3_client().upload_file(mv_log, S3_BUCKET_NAME, key)
                    logging.info("AM MV log mirrored to s3://%s/%s", S3_BUCKET_NAME, key)
            except Exception as e:
                logging.error(f"AM MV log mirror to S3 failed: {e}")
            am_mod.am_write_mv_markers(mv_done_ids, artist=False)

    if local:
        Transfer.total_down_size = sum(
            (ospath.getsize(f) for f in done_files if ospath.exists(f)), 0
        )
    else:
        Transfer.total_down_size = getSize(am_mod.AM_MUSIC_PATH)

    done_msg = (
        Messages.task_msg + "<b>🎵 APPLE MUSIC » </b>\n✅ __All batches finished.__"
    )
    if local:
        done_msg += (
            f"\n\n💾 <i>Saved locally under</i> <code>{am_mod.AM_MUSIC_PATH}</code>"
        )
    await MSG.status_msg.edit_text(
        text=done_msg,
        reply_markup=keyboard(),
    )
    # In local mode nothing was sent to Telegram, so there are no logs to mail.
    if not local:
        await SendLogs(True)
    BOT.Mode.am_local = False


async def Do_AM_Songlist(is_zip, is_unzip, is_dualzip):
    """Download an arbitrary song list (``/content/songlist.txt``) in ALL formats.

    Resume is single-file based: the appended log ``music-logs/songlist-dedupe.log``
    (one ``DONE <format> <adamID>`` line per completed track) survives a Colab
    restart via its S3 mirror, so re-running only downloads what's still missing.
    Downloads run in small chunks; uploads happen incrementally through the
    ``am_download_songlist`` on_new_files hook.
    """
    from colab_leecher.downlader import apple_music as am_mod
    from colab_leecher.utility.handler import Leech

    Messages.download_name = "Apple Music Songlist"
    local = bool(BOT.Mode.am_local)
    if local:
        # /amusic songs local → download to Colab disk, no Telegram upload
        am_mod.set_am_music_path(am_mod.AM_LOCAL_MUSIC_PATH)

    # 1) Parse the song list
    await MSG.status_msg.edit_text(
        text=Messages.task_msg
        + "<b>🎵 APPLE MUSIC » </b>\n⏳ __Reading songlist...__",
        reply_markup=keyboard(),
    )
    song_urls, groups = am_mod.fetch_songlist(am_mod.AM_SONGLIST_PATH)
    if not song_urls:
        raise RuntimeError(f"No songs found in {am_mod.AM_SONGLIST_PATH}")
    total_songs = len(song_urls)
    logging.info("AM songlist task: %d songs in %d album group(s)", total_songs, len(groups))

    async def _safe_edit(text):
        """Status edits are cosmetic — a MessageNotModified/400 must NEVER
        kill a multi-hour download/upload run."""
        try:
            await MSG.status_msg.edit_text(text=text, reply_markup=keyboard())
        except Exception as e:
            logging.warning(f"AM songlist status edit skipped: {e}")

    await _safe_edit(
        Messages.task_msg
        + f"<b>🎵 APPLE MUSIC » SONGLIST</b>\n📀 __{total_songs} songs from "
        f"{len(groups)} album group(s) — downloading in ALL formats "
        f"(resume: S3 dedupe log)...__",
    )

    done_files = set()

    async def _upload_chunk(fmt, new_files):
        done_files.update(new_files)
        if local:
            logging.info("AM songlist %s chunk done — %d files saved locally", fmt, len(new_files))
            return
        await _safe_edit(
            Messages.task_msg
            + f"<b>🎵 APPLE MUSIC » SONGLIST</b>\n"
            f"📤 __Uploading {len(new_files)} {fmt.upper()} files "
            f"({len(done_files)} so far)...__",
        )
        Paths.down_path = am_mod.AM_MUSIC_PATH
        for f in new_files:
            await Leech(ospath.dirname(f), False)

    await am_mod.am_download_songlist(song_urls, on_new_files=_upload_chunk)

    if local:
        Transfer.total_down_size = sum(
            (ospath.getsize(f) for f in done_files if ospath.exists(f)), 0
        )
    else:
        Transfer.total_down_size = getSize(am_mod.AM_MUSIC_PATH)

    done_msg = (
        Messages.task_msg
        + f"<b>🎵 APPLE MUSIC » </b>\n✅ __Songlist finished — {len(done_files)} files handled.__"
    )
    if local:
        done_msg += f"\n\n💾 <i>Saved locally under</i> <code>{am_mod.AM_MUSIC_PATH}</code>"
    await _safe_edit(done_msg)
    # In local mode nothing was sent to Telegram, so there are no logs to mail.
    if not local:
        await SendLogs(True)
    BOT.Mode.am_local = False


async def Do_AM_Artist(am_url, is_zip, is_unzip, is_dualzip):
    """Download every album of an Apple Music artist, one album per batch.

    Resolves the artist's full album list via amp-api (oldest release first),
    then for each album runs the 5-format pass like the playlist flow. Music
    videos are done artist-wide afterwards (albums don't expose an MV mapping
    in the Apple API). Resume is per-album off the ``music-logs/album-<id>/``
    S3 keys, and per-batch for the artist MV pass.

    The number of albums processed can be capped with ``AM_ALBUM_LIMIT`` and
    the music-video batch count with ``AM_MV_LIMIT`` (both 0 = everything).
    """
    from colab_leecher.downlader import apple_music as am_mod
    from colab_leecher.utility.handler import Leech

    Messages.download_name = "Apple Music Artist"
    local = bool(BOT.Mode.am_local)
    if local:
        am_mod.set_am_music_path(am_mod.AM_LOCAL_MUSIC_PATH)

    try:
        from colab_leecher import AM_ALBUM_LIMIT, AM_MV_LIMIT
    except ImportError:
        AM_ALBUM_LIMIT = 0
        AM_MV_LIMIT = 0

    # 1) Resolve the artist's full album list (oldest first).
    await MSG.status_msg.edit_text(
        text=Messages.task_msg
        + "<b>🎵 APPLE MUSIC » </b>\n⏳ __Resolving artist albums...__",
        reply_markup=keyboard(),
    )
    albums = am_mod.fetch_artist_albums(am_url, limit=AM_ALBUM_LIMIT)
    total_albums = len(albums)
    logging.info("AM artist: %d albums to process", total_albums)

    def album_id_of(url: str) -> str:
        m = re.search(r"/album/[^/]+/(\d+)", url)
        return m.group(1) if m else "?"

    # 1b) Crash-resume: skip albums whose 5 format logs are already in S3.
    completed_albums = am_mod.am_completed_albums()
    if completed_albums:
        n_skip = sum(1 for u in albums if album_id_of(u) in completed_albums)
        logging.info("AM resume: %d already-completed album(s) found in S3", n_skip)
        await MSG.status_msg.edit_text(
            text=Messages.task_msg
            + f"<b>🎵 APPLE MUSIC » </b>\n🔁 <i>Resuming — {n_skip} album(s) already done "
            f"({total_albums - n_skip}/{total_albums} remaining).</i>",
            reply_markup=keyboard(),
        )

    done_files = set()
    n_fmts = len(am_mod.AM_FORMATS)
    for album_no, album_url in enumerate(albums, start=1):
        album_id = album_id_of(album_url)
        if album_id in completed_albums:
            # Coarse check passed, but the per-song markers are authoritative:
            # if any track still misses a format (failed in a previous run),
            # re-run the album — am_download_album only passes missing tracks.
            track_urls = am_mod.fetch_album_tracks(album_url)
            if track_urls:
                done_map = am_mod._am_done_formats_for_urls(track_urls, album_id=album_id)
                if all(len(done_map.get(u, set())) >= n_fmts for u in track_urls):
                    logging.info("AM skip album %s (already completed)", album_id)
                    continue
                logging.info("AM album %s logs present but some tracks lack formats — resuming", album_id)
            else:
                logging.info("AM skip album %s (already completed)", album_id)
                continue
        before = am_mod._am_files_snapshot()

        # 2) Download this album in ALL formats (album URL expands to its tracks).
        format_logs, pending_markers = await am_mod.am_download_album(album_url, album_id, album_no, total_albums)

        # 3) Upload only the tracks created by this album (skip in local mode).
        new_files = sorted(am_mod._am_files_snapshot() - before)
        done_files.update(new_files)
        if not new_files:
            logging.warning("Album %d produced no new files — nothing to upload", album_no)
        elif local:
            await MSG.status_msg.edit_text(
                text=Messages.task_msg
                + f"<b>🎵 APPLE MUSIC » </b>\n💾 __Saved album {album_no}/{total_albums} "
                f"({album_id}) locally — {len(new_files)} files under "
                f"<code>{am_mod.AM_MUSIC_PATH}</code>__",
                reply_markup=keyboard(),
            )
            logging.info("Album %d/%d done — %d files saved locally", album_no, total_albums, len(new_files))
        else:
            await MSG.status_msg.edit_text(
                text=Messages.task_msg
                + f"<b>🎵 APPLE MUSIC » </b>\n📤 __Uploading album {album_no}/{total_albums} "
                f"({album_id}) — {len(new_files)} files...__",
                reply_markup=keyboard(),
            )
            Paths.down_path = am_mod.AM_MUSIC_PATH
            for f in new_files:
                await Leech(ospath.dirname(f), False)
            logging.info("Album %d/%d done — %d files uploaded", album_no, total_albums, len(new_files))

        # 4) AFTER upload: mirror each format's log + write per-song markers.
        try:
            from colab_leecher.uploader.s3 import ensure_s3_client
            from colab_leecher import S3_BUCKET_NAME

            if S3_BUCKET_NAME:
                client = ensure_s3_client()
                for name, log_path in format_logs:
                    key = am_mod._am_album_log_key(album_id, name)
                    client.upload_file(log_path, S3_BUCKET_NAME, key)
                    logging.info("AM log %s mirrored to s3://%s/%s", name, S3_BUCKET_NAME, key)
        except Exception as e:
            logging.error(f"AM album log mirror to S3 failed: {e}")
        am_mod.am_write_song_markers(pending_markers)

    # 5) Artist music videos — download at max quality in batches of 5, resume
    #    off the artist-mv-batchNN.log mirrors in S3.
    try:
        mvs = am_mod.fetch_artist_mvs(am_url, limit=AM_MV_LIMIT)
    except Exception as e:
        logging.warning(f"AM artist MV list failed ({e}) — skipping MV pass")
        mvs = []
    if mvs:
        mv_batches = [mvs[i : i + 5] for i in range(0, len(mvs), 5)]
        mv_total = len(mv_batches)
        logging.info("AM artist: %d music videos -> %d batches of 5", len(mvs), mv_total)
        completed_mv = am_mod.am_completed_artist_mv_batches()
        for mv_no, mv_batch in enumerate(mv_batches, start=1):
            if mv_no in completed_mv:
                done = am_mod._am_mv_done_for_urls(mv_batch, artist=True)
                if all(am_mod._am_url_adam_id(u) in done for u in mv_batch):
                    logging.info("AM skip artist MV batch %d (already completed)", mv_no)
                    continue
                logging.info("AM artist MV batch %d logs present but some videos failed — resuming", mv_no)
            before = am_mod._am_files_snapshot()
            mv_log, mv_done_ids = await am_mod.am_download_mvs(mv_batch, mv_no, mv_total, artist=True)

            new_files = sorted(am_mod._am_files_snapshot() - before)
            done_files.update(new_files)
            if not new_files:
                logging.warning("Artist MV batch %d produced no new files — nothing to upload", mv_no)
            elif local:
                await MSG.status_msg.edit_text(
                    text=Messages.task_msg
                    + f"<b>🎵 APPLE MUSIC » {am_mod.AM_MV_FORMAT}</b>\n"
                    f"💾 __Saved artist MV batch {mv_no}/{mv_total} locally — "
                    f"{len(new_files)} files under <code>{am_mod.AM_MUSIC_PATH}</code>__",
                    reply_markup=keyboard(),
                )
                logging.info("Artist MV batch %d/%d done — %d files saved locally", mv_no, mv_total, len(new_files))
            else:
                await MSG.status_msg.edit_text(
                    text=Messages.task_msg
                    + f"<b>🎵 APPLE MUSIC » {am_mod.AM_MV_FORMAT}</b>\n"
                    f"📤 __Uploading artist MV batch {mv_no}/{mv_total} ({len(new_files)} files)...__",
                    reply_markup=keyboard(),
                )
                Paths.down_path = am_mod.AM_MUSIC_PATH
                for f in new_files:
                    await Leech(ospath.dirname(f), False)
                logging.info("Artist MV batch %d/%d done — %d files uploaded", mv_no, mv_total, len(new_files))

            # AFTER upload: mirror the batch log + write per-MV markers.
            try:
                from colab_leecher.uploader.s3 import ensure_s3_client
                from colab_leecher import S3_BUCKET_NAME

                if S3_BUCKET_NAME and mv_log:
                    key = am_mod._am_mv_log_key(mv_no, artist=True)
                    ensure_s3_client().upload_file(mv_log, S3_BUCKET_NAME, key)
                    logging.info("AM artist MV log mirrored to s3://%s/%s", S3_BUCKET_NAME, key)
            except Exception as e:
                logging.error(f"AM artist MV log mirror to S3 failed: {e}")
            am_mod.am_write_mv_markers(mv_done_ids, artist=True)

    if local:
        Transfer.total_down_size = sum(
            (ospath.getsize(f) for f in done_files if ospath.exists(f)), 0
        )
    else:
        Transfer.total_down_size = getSize(am_mod.AM_MUSIC_PATH)

    done_msg = (
        Messages.task_msg + "<b>🎵 APPLE MUSIC » </b>\n✅ __All albums finished.__"
    )
    if local:
        done_msg += (
            f"\n\n💾 <i>Saved locally under</i> <code>{am_mod.AM_MUSIC_PATH}</code>"
        )
    await MSG.status_msg.edit_text(
        text=done_msg,
        reply_markup=keyboard(),
    )
    if not local:
        await SendLogs(True)
    BOT.Mode.am_local = False


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
