# Apple Music Downloader (`/amusic`)

`/amusic` downloads a predefined Apple Music **playlist** and/or an **artist's
full album catalog** in **all five audio formats plus Music Videos**, uploads
every track to Telegram, and mirrors each format's download log to S3.

- `/amusic` — download into `/music` and **upload everything to Telegram**.
- `/amusic local` — download into `/content` (the Colab disk) and **keep it
  local** — nothing is uploaded to Telegram. Files land under
  `/content/AM-DL downloads/`, `/content/AM-DL-Atmos downloads/`,
  `/content/AM-DL-AAC downloads/` and `/content/AM-DL-MV downloads/`.

Both sources are supported, and they can be set at the same time:

| Config field | Source | Processing |
|---|---|---|
| `AM_PLAYLIST_URL` | Playlist link | 5-song batches, then the playlist's MVs |
| `AM_ARTIST_URL` | Artist link (`…/artist/…`) | ALL albums oldest-first (one album per batch), then the artist-wide MVs |

Each source runs as its **own independent pass** and writes to a **separate S3
log keyspace**, so resuming a playlist never reads the artist's logs and vice
versa (see §1).

This document covers the end-to-end flow, the unified naming convention, batch
processing, configuration, and troubleshooting.

---

## 1. Overview

Sending `/amusic` to the bot triggers `Do_AM_Music` (playlist mode) and/or
`Do_AM_Artist` (artist mode) in `colab_leecher/utility/task_manager.py`. The
dispatcher runs the playlist pass first, then the artist pass, when both are
configured.

**Playlist mode** (`Do_AM_Music`):

1. Reads the playlist URL (passed explicitly as `am_url`) and
   resolves the full track list from the public playlist page — **no auth
   needed** to list songs.
2. Slices the track list into **batches of 5 songs**.
3. For each batch, downloads the 5 songs through all five formats.
4. Uploads only the **newly produced** files to Telegram.
5. Mirrors each format's log to `s3://<bucket>/music-logs/<format>-batchNN.log`.
6. Moves to the next batch until the whole playlist is done.
7. **Music videos** in the playlist (musicVideo items) are downloaded
   afterwards in their own batches at **max quality** (mv-max 2160,
   mv-audio-type atmos) — they land under `AM-DL-MV downloads/` as `.mp4`.

**Artist mode** (`Do_AM_Artist`):

1. Resolves the artist's **full album list** via the amp-api
   (`/v1/catalog/<cc>/artists/<id>/albums`, oldest release first).
2. Downloads **one album per pass** (the album URL expands to its own tracks)
   through all five formats.
3. Mirrors each album's logs under `s3://<bucket>/music-logs/album-<id>/`.
4. After the albums, downloads the artist's **music videos** in batches of 5
   at max quality, mirroring logs to `music-logs/artist-mv-batchNN.log`.
5. `AM_ALBUM_LIMIT` / `AM_MV_LIMIT` (0 = everything) cap how many albums/MVs
   are processed.

**Crash-resume & key isolation:** before running, each pass scans S3 for its
OWN log keys and skips anything already finished by a previous run. Resume is
two-layered:

1. **Batch/album layer** — coarse skip: a batch is done when all 5 format logs
   exist (`music-logs/<format>-batchNN.log`), an album when all 5
   `music-logs/album-<id>/<format>.log` exist, an MV batch when its
   `music-logs/[artist-]mv-batchNN.log` exists.
2. **Per-song layer** — every individual track gets its own marker the moment
   a pass finishes it. On a resume, a pass only re-runs the tracks that are
   STILL MISSING that format, so a crash mid-batch (Colab restart) never
   re-downloads (or re-uploads) the songs that already made it — and a track
   that failed in an otherwise-finished batch is retried, not skipped forever.

| Pass | Resume S3 keys (read) | Written keys |
|---|---|---|
| Playlist songs | `music-logs/<format>-batchNN.log` + `music-logs/song-<format>-<adamID>.log` | both |
| Playlist MVs | `music-logs/mv-batchNN.log` + `music-logs/mv-song-<adamID>.log` | both |
| Artist albums | `music-logs/album-<id>/…` + `music-logs/album-<id>/song-<format>-<adamID>.log` | both |
| Artist MVs | `music-logs/artist-mv-batchNN.log` + `music-logs/artist-mv-song-<adamID>.log` | both |
| **Songlist** | `music-logs/songlist-dedupe.log` (single appended `DONE <format> <adamID>` lines) | `music-logs/songlist-dedupe.log` + `music-logs/songlist/<format>.log` |

A per-song marker is written when a track's segment in the pass log shows
`Decrypted` (media written) **or** `no codec found` (Apple doesn't offer that
format for the track, e.g. a legacy song in the Atmos pass — permanently
unavailable, so it is marked done and never retried). Failed segments
(`Failed to dl`, token/network errors) stay unmarked and are retried on the
next run.

The scan regexes are strict (format-name whitelist, per-source prefixes), so a
playlist resume in a mixed bucket never picks up `album-*`, `mv-batch*` or
`artist-mv-batch*` keys, and an artist resume never picks up playlist song keys.

**Local mode:** `/amusic local` switches the working directory to `/content`
via `set_am_music_path()` (in `apple_music.py`) and skips the `Leech` upload
step — the resume logic, log mirroring and naming all work the same, the files
just stay on the Colab disk.

**Songlist mode** (`/amusic songs`, `Do_AM_Songlist`):

1. Reads an arbitrary song list from **`/content/songlist.txt`** — album
   headers (line ending in `:`) plus one Apple Music song URL (or bare adamID)
   per line; `#` comments and blanks are ignored, duplicates deduped
   globally (`fetch_songlist` in `apple_music.py`).
2. Downloads the **whole list in ALL five formats**, chunked into
   `AM_SONGLIST_CHUNK`-sized (25) am-downloader subprocess runs.
3. Resume state is **one single appended file**: every completed track×format
   appends a `DONE <format> <adamID>` line to
   `am-logs/songlist-dedupe.log`, which is continuously mirrored to
   `s3://<bucket>/music-logs/songlist-dedupe.log`. On startup the S3 copy is
   merged with the local one, so a Colab restart — even a fresh runtime —
   skips everything already done ("resume based on S3 log"). **Order is
   critical: download → upload to Telegram → log append.** A track is only
   marked done AFTER its files landed in Telegram — a crash between download
   and upload re-runs that chunk next boot instead of losing the files.
4. The am-downloader output of every chunk of the same format is appended
   into ONE cumulative log (`am-logs/<format>-songlist.log`, mirrored to
   `music-logs/songlist/<format>.log`).
5. New files are uploaded to Telegram after every chunk through the
   `on_new_files` hook (skipped in `local` mode).
6. **Auto mode**: `AM_SONGLIST_AUTO = True` in credentials (a `@param`
   checkbox in the notebook / constant in the cell) makes the bot start the
   songlist download by itself right after startup — no `/amusic` START press.
   The notebook cell can also pull the list from a `SONGLIST_URL`
   (http(s) or `s3://bucket/key`) before starting the bot.

**Generating a songlist:** `tools/extract_songlist.py` builds `songlist.txt`
from artist/album/playlist Apple Music links (no auth — public iTunes Lookup
API + public page parsing):

```bash
# from a links.txt containing any music.apple.com URLs (artist/album/playlist)
python3 tools/extract_songlist.py /path/to/links.txt -o songlist.txt
# or directly
python3 tools/extract_songlist.py "https://music.apple.com/in/artist/a-r-rahman/3249567/full-albums" -o songlist.txt
```

Then upload `songlist.txt` to `/content` on the Colab runtime (or serve it
via `S3`/http and point `SONGLIST_URL` at it).

```
/amusic
  ├─ [playlist pass]
  │    └─ fetch_playlist_songs(AM_PLAYLIST_URL)  → 99 songs
  │         └─ batches of 5
  │              └─ per batch:
  │                   ├─ am_download(batch)      → 5 formats × 5 songs
  │                   ├─ snapshot diff           → only new files
  │                   ├─ Leech(each new file)    → upload to Telegram
  │                   └─ mirror logs → music-logs/<fmt>-batchNN.log
  │    └─ fetch_playlist_mvs(AM_PLAYLIST_URL)    → N music videos
  │         └─ per batch: am_download_mvs → mirror music-logs/mv-batchNN.log
  └─ [artist pass]
       └─ fetch_artist_albums(AM_ARTIST_URL)     → ALL albums (oldest first)
            └─ per album:
                 ├─ am_download_album(url, id)   → 5 formats × album tracks
                 ├─ snapshot diff                → only new files
                 ├─ Leech(each new file)         → upload to Telegram
                 └─ mirror logs → music-logs/album-<id>/<fmt>.log
       └─ fetch_artist_mvs(AM_ARTIST_URL)        → artist-wide MVs
            └─ per batch: am_download_mvs → mirror music-logs/artist-mv-batchNN.log
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
| `AM_ARTIST_URL` | An artist link — ALL the artist's albums are downloaded oldest-first, one album per pass, plus the artist-wide MVs |
| `AM_ALBUM_LIMIT` | Artist mode: cap on how many albums are downloaded (0 = all) |
| `AM_MV_LIMIT` | Artist mode: cap on how many artist music videos are downloaded (0 = all) |
| `AM_MEDIA_TOKEN` | Your Apple Music `media-user-token` — required for lossless / Atmos / AAC from an active subscription |
| `AM_AUTH_TOKEN` | Optional pinned Authorization JWT; am-downloader falls back to it when its own token scrape returns empty (fixes all-MV "media-user-token may wrong or expired") |
| `S3_*` fields | Only needed to auto-fetch the am-downloader toolchain and to mirror logs; toolchain is served from `s3://<bucket>/am-tools/` |

Both URLs may be set at once — each source is processed in its own pass and
keeps its own S3 log keyspace (`music-logs/<format>-batchNN.log` /
`music-logs/mv-batchNN.log` for the playlist, `music-logs/album-<id>/…` /
`music-logs/artist-mv-batchNN.log` for the artist), so crash-resume for one
never touches the other's logs.

The decryption wrapper + am-downloader binaries are fetched once on the first
`/amusic` run into `/content/am-tools/` and reused afterwards (no re-download
on later runs or batches).

### Toolchain

`/amusic` needs three objects under `s3://<bucket>/am-tools/` (auto-fetched on
first run):

| Object | Purpose |
|---|---|
| `am-tools/am-downloader` | The downloader binary (song + MV passes) |
| `am-tools/wrapper-release.tar.gz` | The Android decryption wrapper (ports 10020/20020/30020) |
| `am-tools/mp4decrypt` | Bento4 binary required for the music-video pass |

If any of these are missing from the bucket the task stops with a clear error
pointing at the missing key — e.g. after the `am-tools/` prefix was wiped. To
recover, re-upload the objects (they match the ones described in this doc) and
run `/amusic` again.

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
