# How >2 GB Splitting & Upload Works

A deep dive into how Colab Leecher delivers files larger than Telegram's 2 GB per-upload limit — and why `/s3leech` behaves exactly like `/tupload` for big files.

---

## Telegram's hard limit

A standard Telegram bot can upload at most **2 GB (2000 MiB)** in a single file. Anything bigger must be split into multiple parts and uploaded separately. (Premium 4 GB uploads are not wired in yet.)

The threshold constant lives in one place:

```python
# colab_leecher/utility/converters.py
max_size = 2097152000   # 2 GB  (2000 MiB exactly)
```

---

## The single decision point: `sizeChecker`

Every file headed to Telegram passes through `handler.py::Leech`, which calls `sizeChecker(file_path, remove)` on each one:

```
sizeChecker(file):
  size = os.stat(file).st_size
  if size <= 2 GB:
      return False              # upload the file as-is
  # size > 2 GB → must split:
  if ext in {.zip .rar .7z .tar .gz}:
      splitArchive(file)        # raw byte volumes: file.001, file.002, …
  elif fileType == "video" and BOT.Options.is_split:
      splitVideo(file, 2000)    # lossless ffmpeg segments: file.part000.mp4, …
  else:
      archive(file, split=True) # split-zip volumes (zip -s 2000m / 7z -v2000m)
  return True                   # Leech then uploads each produced part
```

Because **all** Telegram-bound uploads share this function, `/tupload`, `/s3leech` (single object **and** whole-bucket iterate), and `/drupload` produce identical results for the same file.

---

## The three splitters

### 1. `splitVideo` — for video files (lossless, playable parts)

This is the preferred path for movies (`BOT.Options.is_split = True`, the default). It uses ffmpeg stream-copy:

```
ffmpeg -i input -c copy -map 0 -f segment -segment_time <T> -reset_timestamps 1 out.part%03d.ext
```

- **`-c copy`** = no re-encoding, no quality loss, fast. **This is NOT compression** — total size of parts ≈ original.
- **`-map 0`** = keep every audio/subtitle track.
- Each `.partNNN.ext` is independently playable.

**How the segment time is chosen (the important part):**

Older versions derived the segment time from ffprobe's reported `format.bit_rate`, which is frequently missing or under-reported — producing too-few, oversized parts. The current implementation instead:

1. Reads the **actual duration** and uses the **actual file size** → reliable effective bitrate.
2. Targets a fraction of the cap and **retries progressively smaller targets** to survive variable-bitrate (VBR) peaks (e.g. 4K AV1 movies):

   | Attempt | Target per part |
   |---|---|
   | 1 | 94 % of 2 GB |
   | 2 | 85 % |
   | 3 | 72 % |
   | 4 | 60 % |

3. After each attempt it **verifies every produced part is ≤ 2 GB**. The first attempt where all parts fit wins.
4. If even 60 % fails (pathological content), it falls back to a **guaranteed raw byte-split** (see below).

> Example: a 5.01 GiB, 2.5-hour 4K movie → typically 3 parts of ~1.7 GiB each, all under the cap.

### 2. `splitArchive` — raw byte split (guaranteed correctness)

Used for archive files and as the universal fallback. Reads the file in `max_size`-byte chunks and writes `file.001`, `file.002`, … Each part is exactly ≤ the cap.

Parts are **not individually usable** — reassemble with:

```bash
cat file.001 file.002 file.003 > file        # Linux/macOS
copy /b file.001 + file.002 + file.003 file  # Windows
```

### 3. `archive(split=True)` — split-zip volumes

Used for non-video files (and when you pick **Compress**). Runs `zip -s 2000m` (or `7z -v2000m` when a zip password is set) to produce multi-volume zips: `name.zip`, `name.z01`, `name.z02`, … Extract by pointing your unzip tool at the `.zip` volume.

---

## Upload + failure handling

`uploader/telegram.py::upload_file` uploads each part and now:

1. **Pre-checks** the part size against the 2 GB cap. If something slips through oversized, it logs `Upload SKIPPED — … exceeds Telegram's 2 GB limit` and records the name in `Transfer.failed_files` — it does **not** silently pretend success.
2. Returns `True`/`False` so callers know the outcome.
3. On any other upload error, records the failure too.

`handler.py::SendLogs` then appends a clear warning if `Transfer.failed_files` is non-empty:

```
⚠️ N FILE(S) FAILED TO UPLOAD »
   • <name>
These were NOT delivered. Check logs / retry.
```

So a run can never falsely report `COMPLETE` while data is missing.

---

## Interaction with whole-bucket iterate mode

In `iterate_s3_to_telegram`, an S3 object is marked **done** in the tracker **only if none of its parts failed**:

```
failed_before = len(Transfer.failed_files)
… run Leech (which splits + uploads) …
if len(Transfer.failed_files) > failed_before:
    # at least one part failed → do NOT track → next run retries this object
else:
    s3_track("downloaded", …)   # safe to mark done
```

Combined with the S3-persisted tracker, this means a crash or a failed part leaves the object for retry on the next run — never half-delivered-but-marked-done.

---

## Quick reference: which splitter runs?

| File | Upload type | Splitter | Output |
|---|---|---|---|
| `movie.mkv` (5 GB) | Regular | `splitVideo` | `movie.part000.mkv`, `.part001`, … (playable) |
| `archive.zip` (5 GB) | Regular | `splitArchive` | `archive.zip.001`, `.002`, … (reassemble) |
| `data.bin` (5 GB) | Regular | `archive(split)` | `data.bin.zip`, `.z01`, … (split-zip) |
| anything (5 GB) | Compress | `archive(split)` | `name.zip`, `.z01`, … |
| `pathological.mkv` | Regular | `splitVideo` → raw fallback | `.001`, `.002`, … if ffmpeg can't fit parts |

To S3 destinations (`/s3upload`) there is **no splitting** — boto3 multipart uploads the whole object (S3 supports up to 5 TiB), matching `/gdupload`.
