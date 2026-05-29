# Architecture

How Colab Leecher is wired together, end to end. Read this if you want to understand the flow before modifying the code.

---

## High-level flow

```
Telegram DM command (/tupload, /s3leech, …)
        │
        ▼
colab_leecher/__main__.py        ← command handlers, callback buttons
        │  sets BOT.Mode.mode / .type, collects BOT.SOURCE
        ▼
utility/task_manager.py          ← taskScheduler(): the orchestrator
        │
        ├── bulk path  → downloadManager() → Do_Leech / Do_Mirror / Do_S3_Mirror
        │
        └── iterate path → iterate_s3_to_telegram / iterate_s3_to_s3   (utility/s3_iter.py)
                 │
                 ▼
        downloaders (downlader/*.py)  →  local disk (Paths.down_path)
                 │
                 ▼
        processing (utility/converters.py: zip / unzip / sizeChecker split)
                 │
                 ▼
        uploaders (uploader/telegram.py  or  uploader/s3.py)
```

---

## Module map

| Module | Responsibility |
|---|---|
| `__init__.py` | Loads `credentials.json`, sets up the asyncio loop (Py3.12 fix), builds the Pyrogram `Client`. |
| `__main__.py` | All bot command handlers (`/tupload`, `/s3upload`, `/s3leech`, `/s3bucket`, `/s3prefix`, `/settings`, …), inline-button callbacks, and the task launcher. |
| `utility/task_manager.py` | `taskScheduler()` orchestrates a task: resets state, posts the dump message, decides bulk vs iterate, dispatches to the right `Do_*` / `iterate_*` function. |
| `utility/handler.py` | `Leech` (Telegram upload loop), `Zip_Handler`, `Unzip_Handler`, `S3_Mirror_Handler`, `SendLogs`, `cancelTask`. |
| `utility/converters.py` | `sizeChecker` (the 2 GB split decision), `splitVideo`, `splitArchive`, `archive` (zip), `extract` (unzip), `videoConverter`. |
| `utility/s3_iter.py` | Whole-bucket iterative engine: `is_multi_object_s3`, `iterate_s3_to_telegram`, `iterate_s3_to_s3`. |
| `utility/helper.py` | URI/type detection (`is_s3`, `parse_s3_uri`, `is_google_drive`, …), `applyCustomName`, status-bar helpers, `keyboard`, `sysINFO`. |
| `utility/variables.py` | All shared state classes: `BOT`, `Transfer`, `Paths`, `Messages`, `MSG`, `S3`, `BotTimes`. |
| `downlader/manager.py` | `downloadManager` routes each source URL to its downloader; `calDownSize`, `get_d_name`. |
| `downlader/{aria2,gdrive,mega,telegram,terabox,ytdl,s3}.py` | Per-source downloaders. |
| `uploader/telegram.py` | `upload_file` (per-file Telegram upload + 2 GB guard), `progress_bar`. |
| `uploader/s3.py` | S3 client, `s3_upload_file`, `S3_Mirror`, and the tracker (`s3_track`, remote persist/load, resume helpers). |

---

## Shared state (`utility/variables.py`)

These are module-level singletons mutated throughout a task. Key ones:

- **`BOT.Mode.mode`** — `"leech"`, `"mirror"` (Drive), `"s3-mirror"`, `"dir-leech"`.
- **`BOT.Mode.type`** — `"normal"`, `"zip"`, `"unzip"`, `"undzip"` (chosen via the inline buttons).
- **`BOT.SOURCE`** — list of source URIs/paths from the user's message.
- **`BOT.Options`** — `custom_name`, `zip_pswd`, `unzip_pswd`, `is_split`, `stream_upload`, …
- **`Transfer`** — running counters: `down_bytes`, `up_bytes`, `sent_file`, `sent_file_names`, `failed_files`, `total_down_size`.
- **`Paths`** — every working directory (`down_path`, `temp_zpath`, `temp_unzip_path`, …) and `s3_tracker`.
- **`S3`** — runtime S3 config (`access_key`, `secret_key`, `bucket`, `prefix`, `endpoint_url`, `region`, cached `client`).

---

## The two task paths

### Bulk path (default, all non-S3 sources + single S3 objects)

`taskScheduler` → `calDownSize` → `downloadManager` (downloads **everything** to `down_path`) → `Do_Leech` / `Do_Mirror` / `Do_S3_Mirror` → `Leech` / copy / `S3_Mirror`.

Good for: HTTP links, Drive, Telegram, YouTube, and single S3 objects. Peak disk = total of all sources.

### Iterate path (multi-object S3 sources)

`taskScheduler` detects `is_multi_object_s3(uri)` is `True` → `iterate_s3_to_telegram` or `iterate_s3_to_s3`. Processes **one object at a time**: clean workspace → download one → process → upload → track → repeat.

Good for: whole buckets / prefixes. Peak disk = largest single object. Crash-resumable via the S3-persisted tracker.

See [S3_GUIDE.md → Whole-bucket iterative mode](./S3_GUIDE.md#-whole-bucket-iterative-mode--crash-resume).

---

## The >2 GB split decision (one place)

Everything that uploads to Telegram funnels through `handler.py::Leech`, which calls `converters.py::sizeChecker` on every file:

```
sizeChecker(file) :
  if size <= 2 GB        → upload as-is
  elif archive ext       → splitArchive  (raw .001/.002 volumes)
  elif video & is_split  → splitVideo    (lossless ffmpeg -c copy segments)
  else                   → archive(split) (split-zip volumes)
```

This is shared by `/tupload`, `/s3leech` (bulk and iterate), and `/drupload` — so the >2 GB behaviour is identical everywhere. See [SPLIT_AND_UPLOAD.md](./SPLIT_AND_UPLOAD.md) for the deep dive.

---

## Adding a new downloader

1. Create `downlader/<name>.py` with an async `download(...)` that writes into `Paths.down_path`.
2. Add a detector in `utility/helper.py` (e.g. `is_<name>(link)`).
3. Wire the detector + dispatch into `downlader/manager.py` (`calDownSize`, `get_d_name`, `downloadManager`).
4. Optionally add an icon branch in `task_manager.py`'s source loop.

No change to the upload/split side is needed — that's shared.
