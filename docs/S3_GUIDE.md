# S3 / Wasabi / B2 Deep Dive

Everything about the S3-compatible storage integration: how it works, every supported provider, all the commands and options, the tracker file format, and the multipart pipeline for >2 GB files.

---

## What you can do

| Direction | Command | Pipeline |
|---|---|---|
| any source → S3 | `/s3upload` | URLs/links/paths → local download → upload to S3 |
| S3 → Telegram | `/s3leech` | S3 object/prefix → local → upload to Telegram |
| S3 → S3 | `/s3upload` with `s3://...` source | source object → local → upload to destination bucket |
| S3 → Google Drive | `/gdupload` with `s3://...` source | source object → local → mirror to Drive |
| S3 inside any other command | (auto) | `s3://` URIs are accepted by `/tupload`, `/gdupload`, `/ytupload` too |

All transfers honor the same options as the rest of the bot: **Regular / Compress / Extract / UnDoubleZip**, custom names, zip & unzip passwords, the `/settings` menu (caption, thumbnail, prefix/suffix, video conversion), and the **Cancel** button.

---

## Configuration (Colab cell)

Five form fields (all live under the **☁️ S3 / Wasabi / B2 Configuration** section):

```text
S3_ACCESS_KEY    # access key
S3_SECRET_KEY    # secret key
S3_BUCKET_NAME   # default destination bucket
S3_ENDPOINT_URL  # leave empty for AWS, fill for Wasabi/B2/MinIO/etc.
S3_REGION        # bucket region (default: us-east-1)
```

When the cell starts you'll see one of:

```text
☁️  S3 enabled → bucket=<my-bucket> region=<ap-northeast-1> endpoint=<https://s3.ap-northeast-1.wasabisys.com>

⚠️  S3 config is partially set — /s3upload and /s3leech will be unavailable until
    S3_ACCESS_KEY, S3_SECRET_KEY and S3_BUCKET_NAME are all filled in.
```

If the line is missing entirely it means S3 is fully unconfigured and the four `/s3*` commands will refuse to run with a clear error message.

---

## Provider quick-start

### AWS S3

```text
S3_ENDPOINT_URL =                                    # leave empty
S3_REGION       = us-east-1                          # match your bucket
```

IAM policy minimum (replace `<bucket>`):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload"],
      "Resource": "arn:aws:s3:::<bucket>/*"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::<bucket>"
    }
  ]
}
```

### Wasabi

```text
S3_ENDPOINT_URL = https://s3.<region>.wasabisys.com  # e.g. https://s3.ap-northeast-1.wasabisys.com
S3_REGION       = <region>                           # e.g. ap-northeast-1
```

Region list: <https://docs.wasabi.com/docs/what-are-the-service-urls-for-wasabi-s-different-regions>. The endpoint must match the region where the bucket lives.

### Backblaze B2 (S3-compatible API)

```text
S3_ENDPOINT_URL = https://s3.<region>.backblazeb2.com  # e.g. https://s3.us-west-002.backblazeb2.com
S3_REGION       = <region>                             # e.g. us-west-002
```

Use **Application Keys** in the B2 console (the master key works but isn't recommended).

### Cloudflare R2

```text
S3_ENDPOINT_URL = https://<account-id>.r2.cloudflarestorage.com
S3_REGION       = auto
```

### DigitalOcean Spaces

```text
S3_ENDPOINT_URL = https://<region>.digitaloceanspaces.com
S3_REGION       = <region>
```

### MinIO / self-hosted

```text
S3_ENDPOINT_URL = https://your-minio.example.com
S3_REGION       = us-east-1
```

---

## URI grammar

| URI | Meaning |
|---|---|
| `s3://bucket/path/to/file.ext` | single object |
| `s3://bucket/folder/` | every object under the prefix (folder mode) |
| `s3:///key/in/default/bucket` | three slashes → use `S3_BUCKET_NAME` as bucket |
| `s3://bucket` (no trailing slash) | every object in the bucket (be careful) |

Folder mode preserves the directory tree under `Paths.down_path` and uploads/leeches each file as you'd expect.

---

## Worked examples

### Example 1 — Mirror a direct download to Wasabi

```text
/s3upload

https://example.com/release-1.0.zip
[release-final.zip]
```

→ pick **Regular** → file lands at `s3://<bucket>/Uploaded__YYYY-MM-DD_HH-MM-SS/release-final.zip` and a JSON entry shows up in `s3teletracker.json`.

### Example 2 — Leech a Wasabi object back to Telegram

```text
/s3leech

s3://my-bucket/Uploaded__2026-05-28_19-45-21/release-final.zip
```

→ pick **Regular** → file is uploaded to your Telegram DM. A `downloaded` entry is appended to the tracker.

### Example 3 — Leech a whole prefix (folder) to Telegram

```text
/s3leech

s3://my-bucket/photos/vacation-2025/
```

→ pick **Compress** → entire folder is downloaded, zipped (with `/zipaswd` if set), and uploaded to Telegram.

### Example 4 — Copy an object from one bucket to another

```text
/s3bucket destination-bucket
/s3upload

s3://source-bucket/path/to/file.mkv
```

→ pick **Regular** → object is downloaded from `source-bucket` and uploaded to `destination-bucket`. Tracker logs both directions.

### Example 5 — Set a destination prefix once, mirror many things

```text
/s3prefix archive/2026-Q2
/s3upload

https://example.com/a.iso
https://example.com/b.iso
https://example.com/c.iso
```

→ all three end up at `s3://<bucket>/archive/2026-Q2/Uploaded__<timestamp>/...`. The prefix persists for the rest of the bot session until you change/clear it.

### Example 6 — Extract an archive stored in S3 and put the contents in Drive

```text
/gdupload

s3://my-bucket/backups/2026-05-01.tar.gz
(archive_password)
```

→ pick **Extract** → archive is downloaded, extracted (with the `/unzipaswd` password from the parens line), and the contents are mirrored to `Drive/Colab Leecher Uploads/...`.

---

## Tracker file (`s3teletracker.json`)

Path: `/content/Telegram-Leecher/s3teletracker.json`

Two top-level arrays — `uploaded` (local→S3) and `downloaded` (S3→local). Each entry:

```json
{
  "timestamp":  "2026-05-28T19:45:21",
  "file_name":  "release-final.zip",
  "bucket":     "my-bucket",
  "key":        "Uploaded__2026-05-28_19-45-21/release-final.zip",
  "size":       2147483648,
  "size_human": "2.00 GB",
  "endpoint":   "https://s3.ap-northeast-1.wasabisys.com"
}
```

### Tips for using the tracker

- The file is appended to in real time, so you can `cat` it during a transfer to verify progress is recorded.
- If the file becomes corrupt (manual edit, etc.) the bot resets it and continues — only the corrupt session's entries are lost.
- Trackers persist across bot restarts inside the same Colab session. To preserve across sessions, copy the file into Drive (e.g. `cp /content/Telegram-Leecher/s3teletracker.json /content/drive/MyDrive/`).

---

## Multipart and big files (>2 GB)

There are **two different "big file" mechanisms** in the codebase, and S3 hooks into both correctly depending on the destination:

### Telegram destination — *physical split into <2 GB parts*
Telegram's bot API caps a single upload at 2 GB. Both `/tupload` and `/s3leech` therefore invoke `colab_leecher/utility/converters.py::sizeChecker` on each file before upload; anything >2 GB is split into `.001 / .002 / …` (raw), `.partNNN.<ext>` (video segment) or split-zip volumes via `zip -s 2000m` / `7z -v2000m`. Each part is then uploaded to Telegram individually.

### Object-storage destination — *multipart upload, no physical split*
S3 (and Drive) accept the whole object, so there's no need to fragment. `/s3upload` and `/gdupload` send the whole file. For S3, the bot uses boto3's `TransferConfig`:

| Parameter | Value |
|---|---|
| `multipart_threshold` | 64 MiB |
| `multipart_chunksize` | 64 MiB |
| `max_concurrency` | 4 |
| `use_threads` | True |

Anything over 64 MiB is uploaded/downloaded as parallel 64 MiB parts. S3 supports up to 10 000 parts per object, so at 64 MiB chunks a single command can move objects up to ~640 GB; boto3 auto-resizes chunk size for objects beyond that, up to S3's 5 TiB ceiling.

Progress is reported via the same `status_bar` used by every other downloader/uploader, so the **Cancel ❌** button keeps working. Cancelling stops the asyncio task immediately; the in-flight chunk in the boto3 worker thread completes silently in the background and is then discarded (boto3 calls `AbortMultipartUpload` on exception so partial uploads don't accumulate).

### What happens to a 5 GB file in each command (worked example)

| Command | Mode | Pipeline | Result |
|---|---|---|---|
| `/tupload` | Regular | `Leech` → `sizeChecker` splits → 3× upload | 3 parts in Telegram |
| `/tupload` | Compress | `Zip_Handler(is_split=True)` → 3× upload | 3 split-zip volumes in Telegram |
| `/s3leech` | Regular | `s3_Download` (boto3 multipart) → `Leech` → `sizeChecker` splits → 3× upload | 3 parts in Telegram |
| `/s3leech` | Compress | `s3_Download` → `Zip_Handler(is_split=True)` → 3× upload | 3 split-zip volumes in Telegram |
| `/gdupload` | Regular | download → `shutil.copytree` → Drive | 1 file in Drive |
| `/gdupload` | Compress | `Zip_Handler(is_split=True)` → Drive | 3 split-zip volumes in Drive |
| `/s3upload` | Regular | download → `s3_upload_file` (boto3 multipart) | **1 object** in S3 (~80 internal parts of 64 MiB) |
| `/s3upload` | Compress | `Zip_Handler(is_split=True)` → upload each volume | 3 separate split-zip objects in S3 |
| `/s3upload` | Extract | download → unzip → upload each extracted file | as many S3 objects as files in the archive |
| `/s3upload` | UnDoubleZip | download → unzip → re-zip (split) → upload | 3 split-zip objects in S3 |

The takeaway: `/s3leech` reuses the exact same split pipeline as `/tupload` (Telegram's 2 GB cap is honored), and `/s3upload` mirrors `/gdupload` (whole file via multipart in Regular mode, split-zip volumes in Compress mode).

---

## Behavior matrix

| Scenario | Result |
|---|---|
| `/s3upload` with no S3 creds | Bot replies with a configuration message and refuses the command. |
| `/s3leech s3://other-bucket/...` while `S3_BUCKET_NAME=my-bucket` | Source URI bucket wins; the configured default is only used by `/s3upload` and by `s3:///key` short-form URIs. |
| `/s3leech s3:///foo` with no `S3_BUCKET_NAME` | Bot raises a clear error: "No S3 bucket specified and no default S3_BUCKET_NAME configured." |
| Object > 2 GB | Multipart kicks in automatically; Telegram split or zip happens after the local download per the regular Settings rules. |
| Source includes a mix of `s3://` and `https://` URIs | Each is dispatched to the right downloader and aggregated together. |
| `/s3leech s3://bucket/folder/` | Whole prefix is mirrored under `down_path/<folder-name>/...`, then leeched/uploaded as a folder per your selected option. |
| Tracker file write fails | A warning is logged but the transfer is **not** aborted (tracker is best-effort). |

---

## Cost & rate considerations

- Multipart uploads are billed per part on most providers — the bot uses 64 MiB parts which keeps the part count low even for 100 GB+ files.
- Wasabi charges egress; AWS charges per request and per GB. Re-running `/s3upload` for the same file uploads it again — there is no idempotent dedup.
- The tracker is the easiest way to audit what cost you what: every entry has the size in bytes and a human-readable form.

---

## Security notes

- `credentials.json` (in `/content/Telegram-Leecher/`) contains your bot token + S3 keys. Treat it as sensitive.
- Colab runtimes are ephemeral — once the runtime stops, that file is gone. If you persist it via Drive, make sure that Drive folder isn't shared.
- The bot itself never broadcasts credentials. It only writes them to the local credentials file at startup.
- You can revoke an exposed key in your provider's console; the bot will fail with `InvalidAccessKeyId` until you update the cell and restart.

---

## Quick command reference

```text
/s3upload                 # mirror downloads → S3 (with all standard options)
/s3leech                  # leech S3 objects/prefixes → Telegram
/s3bucket <name>          # change destination bucket (session-scoped)
/s3prefix <folder/sub>    # set or clear destination key prefix
```

For more, see [COMMANDS.md](./COMMANDS.md) and [TROUBLESHOOTING.md](./TROUBLESHOOTING.md).
