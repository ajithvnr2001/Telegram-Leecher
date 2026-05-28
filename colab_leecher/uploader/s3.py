# copyright 2024 © Xron Trix | https://github.com/Xrontrix10
"""S3 / Wasabi uploader.

Uploads files (or whole folders) from local disk to a configurable
S3-compatible bucket. Mirrors the structure of
`colab_leecher/uploader/telegram.py`:

- ``s3_upload_file``  : single file upload + live progress + tracker write.
- ``S3_Mirror``       : uploads everything under ``folder_path`` to S3,
                         honoring the same Telegram-Leecher conventions
                         (split big files via the existing 2 GB sizeChecker
                         pipeline so the >2 GB experience matches the
                         other commands).

Big-file handling: ``boto3``'s ``TransferConfig`` automatically performs
multipart uploads above ``multipart_threshold``. We set 64 MiB threshold
and 64 MiB chunks to keep memory usage low while remaining fast on
Colab. AWS S3 caps a single object at 5 TiB and a single PUT at 5 GiB,
so multipart is required for >5 GiB anyway.
"""

import os
import json
import shutil
import logging
import pathlib
from os import path as ospath, makedirs
from datetime import datetime
from time import time as _time
from asyncio import get_event_loop, sleep
from natsort import natsorted

from colab_leecher.utility.helper import (
    getSize,
    getTime,
    keyboard,
    shortFileName,
    sizeUnit,
    speedETA,
    status_bar,
    sysINFO,
)
from colab_leecher.utility.variables import (
    BOT,
    BotTimes,
    Messages,
    MSG,
    Paths,
    S3,
    Transfer,
)


# ---------------------------------------------------------------------------
# client helpers
# ---------------------------------------------------------------------------

def _load_config_into_S3():
    """Populate ``S3.*`` from credentials.json on first use."""
    try:
        import colab_leecher as _cl  # avoid circular import at module load
    except Exception:
        return
    S3.access_key = S3.access_key or getattr(_cl, "S3_ACCESS_KEY", "") or ""
    S3.secret_key = S3.secret_key or getattr(_cl, "S3_SECRET_KEY", "") or ""
    S3.endpoint_url = S3.endpoint_url or getattr(_cl, "S3_ENDPOINT_URL", "") or ""
    S3.bucket = S3.bucket or getattr(_cl, "S3_BUCKET_NAME", "") or ""
    S3.region = S3.region or getattr(_cl, "S3_REGION", "") or "us-east-1"


def is_s3_configured() -> bool:
    _load_config_into_S3()
    return bool(S3.access_key and S3.secret_key and S3.bucket)


def ensure_s3_client():
    """Return a cached boto3 S3 client built from current ``S3.*`` config."""
    _load_config_into_S3()
    if S3.client is not None:
        return S3.client
    if not (S3.access_key and S3.secret_key):
        raise RuntimeError(
            "S3 not configured. Set S3_ACCESS_KEY and S3_SECRET_KEY in the Colab cell."
        )
    try:
        import boto3  # noqa: WPS433 (lazy import — boto3 is optional)
    except ImportError as e:
        raise RuntimeError(
            "boto3 is not installed. Add `boto3` to requirements.txt and reinstall."
        ) from e

    kwargs = dict(
        service_name="s3",
        aws_access_key_id=S3.access_key,
        aws_secret_access_key=S3.secret_key,
        region_name=S3.region or "us-east-1",
    )
    if S3.endpoint_url:
        kwargs["endpoint_url"] = S3.endpoint_url
    S3.client = boto3.client(**kwargs)
    return S3.client


# ---------------------------------------------------------------------------
# tracker (s3teletracker.json)
# ---------------------------------------------------------------------------

def s3_track(direction: str, file_name: str, bucket: str, key: str, size: int):
    """Append a transfer entry to ``s3teletracker.json``.

    `direction` is one of ``"uploaded"`` (local→S3) or ``"downloaded"``
    (S3→local). Failures here are non-fatal: tracker is best-effort.
    """
    path = Paths.s3_tracker
    data = {"uploaded": [], "downloaded": []}
    try:
        if ospath.exists(path):
            with open(path, "r") as f:
                data = json.load(f)
    except Exception as e:
        logging.warning(f"S3 tracker read failed (resetting): {e}")
        data = {"uploaded": [], "downloaded": []}
    data.setdefault(direction, [])
    data[direction].append(
        {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "file_name": file_name,
            "bucket": bucket,
            "key": key,
            "size": int(size),
            "size_human": sizeUnit(int(size)),
            "endpoint": S3.endpoint_url or "aws",
        }
    )
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logging.warning(f"S3 tracker write failed: {e}")


# ---------------------------------------------------------------------------
# upload primitives
# ---------------------------------------------------------------------------

def _build_transfer_config():
    try:
        from boto3.s3.transfer import TransferConfig
    except ImportError:
        return None
    return TransferConfig(
        multipart_threshold=64 * 1024 * 1024,
        multipart_chunksize=64 * 1024 * 1024,
        max_concurrency=4,
        use_threads=True,
    )


async def s3_upload_file(file_path: str, real_name: str, key_prefix: str = "") -> int:
    """Upload one local file to S3 with live progress + tracker.

    Returns the uploaded file size in bytes.
    """
    client = ensure_s3_client()
    bucket = S3.bucket
    if not bucket:
        raise RuntimeError("S3 destination bucket not set. Use /s3bucket <name>.")

    file_size = ospath.getsize(file_path)
    # Compose S3 key: <S3.prefix>/<key_prefix>/<filename>
    parts = [p.strip("/") for p in (S3.prefix, key_prefix, real_name) if p]
    key = "/".join(parts)

    BotTimes.task_start = datetime.now()
    Messages.status_head = (
        f"<b>📤 UPLOADING TO S3 » </b>\n"
        f"\n<b>🏷️ Name » </b><code>{real_name}</code>"
        f"\n<b>🪣 Bucket » </b><code>{bucket}</code>"
        f"\n<b>🔑 Key » </b><code>{key}</code>\n"
    )

    progress = {"bytes": 0}

    def _cb(n):
        progress["bytes"] += n

    cfg = _build_transfer_config()

    def _do():
        if cfg is not None:
            client.upload_file(file_path, bucket, key, Callback=_cb, Config=cfg)
        else:
            client.upload_file(file_path, bucket, key, Callback=_cb)

    loop = get_event_loop()
    fut = loop.run_in_executor(None, _do)

    while not fut.done():
        done_now = progress["bytes"]
        # Aggregate across files for global percentage if total_down_size set.
        total = max(file_size, 1)
        speed_string, eta, percentage = speedETA(BotTimes.task_start, done_now, total)
        await status_bar(
            Messages.status_head,
            speed_string,
            percentage,
            getTime(eta),
            sizeUnit(done_now),
            sizeUnit(file_size),
            "S3 ☁️",
        )
        await sleep(1)

    await fut  # raises on failure

    s3_track("uploaded", real_name, bucket, key, file_size)
    return file_size


# ---------------------------------------------------------------------------
# folder-level mirror (used by the /s3upload command)
# ---------------------------------------------------------------------------

async def S3_Mirror(folder_path: str, remove: bool):
    """Mirror everything in ``folder_path`` to S3.

    Layout in S3:
        ``<S3.prefix>/<task-folder>/<relative-path-of-file>``

    The task folder name is timestamped so repeated runs don't collide,
    matching the behaviour of ``Do_Mirror`` for Google Drive.
    """
    if not is_s3_configured():
        raise RuntimeError(
            "S3 not configured. Provide S3_ACCESS_KEY, S3_SECRET_KEY and S3_BUCKET_NAME."
        )

    cdt = datetime.now().strftime("Uploaded__%Y-%m-%d_%H-%M-%S")
    files = [str(p) for p in pathlib.Path(folder_path).glob("**/*") if p.is_file()]
    if not files:
        logging.warning("S3_Mirror: nothing to upload, folder empty.")
        return

    total = sum(ospath.getsize(f) for f in files)
    Transfer.total_down_size = total

    for file_path in natsorted(files):
        rel = ospath.relpath(file_path, folder_path)
        # Use forward slashes in S3 keys regardless of host OS.
        rel = rel.replace(os.sep, "/")
        # Trim outrageously long names (mirrors Leech behaviour).
        new_path = shortFileName(file_path)
        if new_path != file_path:
            os.rename(file_path, new_path)
            file_path = new_path
            rel = ospath.relpath(file_path, folder_path).replace(os.sep, "/")

        file_name = ospath.basename(file_path)
        BotTimes.current_time = _time()
        Messages.status_head = (
            f"<b>📤 UPLOADING TO S3 » </b>\n\n<code>{file_name}</code>\n"
        )
        try:
            MSG.status_msg = await MSG.status_msg.edit_text(
                text=Messages.task_msg
                + Messages.status_head
                + "\n⏳ __Starting.....__"
                + sysINFO(),
                reply_markup=keyboard(),
            )
        except Exception as d:
            logging.error(f"Error updating status bar (S3 mirror): {d}")

        # Compose key: <prefix>/<task>/<rel>
        parts = [p.strip("/") for p in (S3.prefix, cdt, ospath.dirname(rel)) if p]
        key_dir = "/".join(parts)

        size_uploaded = await s3_upload_file(file_path, file_name, key_prefix=key_dir)
        Transfer.up_bytes.append(size_uploaded)

        if remove and ospath.exists(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                logging.warning(f"Could not remove {file_path}: {e}")

    if remove and ospath.exists(folder_path):
        try:
            shutil.rmtree(folder_path)
        except Exception as e:
            logging.warning(f"Could not remove folder {folder_path}: {e}")
