# Apple Music Downloader (`/amusic`)

`/amusic` downloads a predefined Apple Music playlist in **all five audio
formats**, uploads every track to Telegram, and mirrors each format's download
log to S3.

- `/amusic` — download into `/music` and **upload everything to Telegram**.
- `/amusic local` — download into `/content` (the Colab disk) and **keep it
  local** — nothing is uploaded to Telegram. Files land under
  `/content/AM-DL downloads/`, `/content/AM-DL-Atmos downloads/` and
  `/content/AM-DL-AAC downloads/`.

This document covers the end-to-end flow, the unified naming convention, batch
processing, configuration, and troubleshooting.

---

## 1. Overview

Sending `/amusic` to the bot triggers `Do_AM_Music` in
`colab_leecher/utility/task_manager.py`, which:

1. Reads the playlist URL from `AM_PLAYLIST_URL` (set in the notebook) and
   resolves the full track list from the public playlist page — **no auth
   needed** to list songs.
2. Slices the track list into **batches of 5 songs**.
3. For each batch, downloads the 5 songs through all five formats.
4. Uploads only the **newly produced** files to Telegram.
5. Mirrors each format's log to `s3://<bucket>/music-logs/<format>-batchNN.log`.
6. Moves to the next batch until the whole playlist is done.

**Crash-resume:** before the first batch, the bot scans S3 for existing
`music-logs/<format>-batchNN.log` objects. Any batch that already has **all five
format logs** in S3 is considered finished by a previous run and is **skipped**
on the next `/amusic`, so a Colab restart never re-downloads completed batches.

**Local mode:** `/amusic local` switches the working directory to `/content`
via `set_am_music_path()` (in `apple_music.py`) and skips the `Leech` upload
step — the resume logic, log mirroring and naming all work the same, the files
just stay on the Colab disk.

```
/amusic
  └─ fetch_playlist_songs(AM_PLAYLIST_URL)     → 99 songs
       └─ batches of 5
            └─ per batch:
                 ├─ am_download(batch)         → 5 formats × 5 songs
                 ├─ snapshot diff              → only new files
                 ├─ Leech(each new file)       → upload to Telegram
                 └─ mirror logs → S3 music-logs/
```

---

## 2. The five formats & unified naming convention

Every song yields **5 files** — one per format — and **all formats share the
same naming convention**:

```
{SongNumber}.{SongName}.{FORMAT}.{VARIANT}.m4a
```

| Format | am-downloader flags | Example output file |
|---|---|---|
| ALAC (Apple Lossless) | *(default run)* | `01.KangalIrandal.ALAC.Lossless.m4a` |
| Dolby Atmos (E-AC-3) | `--atmos` | `01.KangalIrandal.ATMOS.Dolby.m4a` |
| AAC-LC 256 | `--aac --aac-type aac-lc` | `01.KangalIrandal.AAC.256Kbps.m4a` |
| AAC 128 | `--aac --aac-type aac-128` | `01.KangalIrandal.AAC.128Kbps.m4a` |
| HE-AAC 64 | `--aac --aac-type he-aac-64` | `01.KangalIrandal.AAC.64Kbps.m4a` |

The format strings live in `AM_FORMATS` (`colab_leecher/downlader/apple_music.py`):

```python
AM_FORMATS = [
    ("ALAC", [], "{SongNumer}.{SongName}.ALAC.Lossless"),
    ("ATMOS", ["--atmos"], "{SongNumer}.{SongName}.ATMOS.Dolby"),
    ("AAC-LC-256", ["--aac", "--aac-type", "aac-lc"], "{SongNumer}.{SongName}.AAC.256Kbps"),
    ("AAC-128", ["--aac", "--aac-type", "aac-128"], "{SongNumer}.{SongName}.AAC.128Kbps"),
    ("HE-AAC-64", ["--aac", "--aac-type", "he-aac-64"], "{SongNumer}.{SongName}.AAC.64Kbps"),
]
```

Each tuple is `(name, extra am-downloader args, song-file-format)`. The
`{SongNumer}` and `{SongName}` placeholders are resolved by am-downloader
itself; the `.m4a` extension is appended by the downloader.

> **Note:** `{SongNumer}` is the track position **within the batch** (01–05),
> matching the batch size of 5.

### Why a unified convention?

All five formats land with the **same pattern** (`song.format.variant.ext`),
so a batch directory looks like:

```
AM-DL downloads/A.R.Rahman/Album/01.KangalIrandal.ALAC.Lossless.m4a
AM-DL-Atmos downloads/A.R.Rahman/Album/01.KangalIrandal.ATMOS.Dolby.m4a
AM-DL-AAC downloads/A.R.Rahman/Album/01.KangalIrandal.AAC.256Kbps.m4a
AM-DL-AAC downloads/A.R.Rahman/Album/01.KangalIrandal.AAC.128Kbps.m4a
AM-DL-AAC downloads/A.R.Rahman/Album/01.KangalIrandal.AAC.64Kbps.m4a
```

Every format is immediately identifiable from the file name alone.

---

## 3. Where files land

All output goes under `/music` (`AM_MUSIC_PATH` in `apple_music.py`):

| Format | Folder (relative to `/music`) |
|---|---|
| ALAC | `AM-DL downloads/` |
| Atmos | `AM-DL-Atmos downloads/` |
| AAC (all three variants) | `AM-DL-AAC downloads/` |

Inside each folder am-downloader recreates the artist → album structure from
the `artist-folder-format` / `album-folder-format` settings in
`/music/config.yaml` (written by `_write_am_config()`).

Per-format run logs are kept in `/music/am-logs/`:

```
alac-batch01.log
atmos-batch01.log
aac-lc-256-batch01.log
aac-128-batch01.log
he-aac-64-batch01.log
```

---

## 4. Configuration

| Notebook field | Used for |
|---|---|
| `AM_PLAYLIST_URL` | The playlist to download (song list is parsed from the public page) |
| `AM_MEDIA_TOKEN` | Your Apple Music `media-user-token` — required for lossless / Atmos / AAC from an active subscription |
| `S3_*` fields | Only needed to auto-fetch the am-downloader toolchain and to mirror logs; toolchain is served from `s3://<bucket>/am-tools/` |

The decryption wrapper + am-downloader binaries are fetched once on the first
`/amusic` run into `/content/am-tools/` and reused afterwards (no re-download
on later runs or batches).

---

## 5. How it works under the hood

1. **`fetch_playlist_songs()`** — GETs the playlist page, extracts the
   `serialized-server-data` blob, and collects every `storeAdamID` song URL.
2. **`ensure_am_tools()`** — downloads `am-downloader` and
   `wrapper-release.tar.gz` from S3 (or the public fallback URL) if missing.
3. **`start_am_wrapper()`** — launches the Android decryption wrapper on ports
   `10020` (decrypt), `20020` (m3u8), `30020` (account info); retries up to 4
   times because the first devToken fetch is transiently flaky on Colab.
4. **`_write_am_config()`** — writes `/music/config.yaml` with the media token,
   folder formats and per-format ports.
5. **`_run_am_pass()`** — runs `am-downloader <extra_args> --song-file-format <fmt> <urls...>`
   with cwd `/music`, streaming output to `/music/am-logs/<format>-batchNN.log`.
6. **Snapshot diff** — files present before the batch are subtracted from
   files after, so **only new tracks** are uploaded (covers / artwork are also
   uploaded since they're new files under `/music`).
7. **S3 log mirror** — every format log is copied to
   `s3://<bucket>/music-logs/<format>-batchNN.log`.
8. **Resume scan** — `am_completed_batches()` lists the `music-logs/` prefix;
   a batch with all five `<format>-batchNN.log` keys present is skipped on
   re-run (see `colab_leecher/downlader/apple_music.py`).

---

## 6. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `RuntimeError: Failed to download <s3.../am-downloader>` | S3 keys missing from `credentials.json` → the tool falls back to the public URL, which is 403 for private buckets. Add `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET_NAME` (+ endpoint/region). |
| `AM wrapper failed to start after 4 attempts` | Transient devToken failure. Re-run `/amusic`; the wrapper start retries automatically. Check `/content/am-tools/wrapper.log`. |
| Atmos batch shows `0/5` with `no codec found` | The track has no Atmos variant on Apple's CDN — expected for many songs; other formats still complete. |
| `'NoneType' object has no attribute 'id'` | Button pressed on a deleted status message. Fixed by guarding `callback_query.message` (see git history `bd764b8`). |
| No files uploaded for a batch | All tracks already existed from an earlier run (snapshot diff = empty). That is by design. |
