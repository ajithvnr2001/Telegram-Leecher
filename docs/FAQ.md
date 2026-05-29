# FAQ

Quick answers to common questions. For setup see [SETUP.md](./SETUP.md); for errors see [TROUBLESHOOTING.md](./TROUBLESHOOTING.md).

---

### Q. How do I download an entire S3 bucket to Telegram?

Send `/s3leech`, then the bucket URI with a trailing slash:

```
s3://your-bucket/
```

Pick **Regular**. The bot enters whole-bucket iterative mode and processes every object one at a time. A folder/prefix works too: `s3://your-bucket/movies/`.

---

### Q. Is the bot compressing my video when it splits it?

**No.** For a video chosen as **Regular**, splitting uses `ffmpeg -c copy` (stream copy) — no re-encoding, no quality loss. The parts together equal the original size. Compression only happens if you explicitly pick **Compress** (which zips). See [SPLIT_AND_UPLOAD.md](./SPLIT_AND_UPLOAD.md).

---

### Q. My 5 GB movie only produced 2 parts and didn't fully upload. Why?

That was a bug in older code where the splitter sized segments from an unreliable reported bitrate and produced oversized (>2 GB) parts that then failed to upload. It's fixed: the splitter now sizes from real file-size ÷ duration, retries smaller targets for VBR/4K content, verifies every part is under 2 GB, and reports any genuine failure instead of faking success. Make sure your Colab is running the latest `main`.

---

### Q. How do the split parts go back together?

- **Video parts** (`*.part000.mkv`, `*.part001.mkv`, …): each is independently playable; play them in sequence, or concat with ffmpeg.
- **Raw byte parts** (`*.001`, `*.002`, …): `cat file.001 file.002 … > file` (Linux/macOS) or `copy /b file.001 + file.002 file` (Windows).
- **Split-zip volumes** (`*.zip`, `*.z01`, …): open the `.zip` volume with any unzip tool that supports multi-volume archives (7-Zip, WinRAR).

---

### Q. Colab disconnected halfway through a big bucket. Do I lose progress?

No — if you configured `S3_BUCKET_NAME`. The tracker is mirrored to `s3://<bucket>/s3teletracker.json` after every object. Restart Colab, run the **same** `/s3leech s3://your-bucket/` again, and it skips everything already done and resumes. See [S3_GUIDE.md → crash-resume](./S3_GUIDE.md#-whole-bucket-iterative-mode--crash-resume).

---

### Q. How do I force re-processing a bucket I already leeched?

Delete the tracker object, then re-run:

```python
import boto3
boto3.client('s3', aws_access_key_id='KEY', aws_secret_access_key='SECRET',
             endpoint_url='ENDPOINT').delete_object(Bucket='your-bucket', Key='s3teletracker.json')
```

---

### Q. Which S3 providers are supported?

Any S3-compatible service: AWS, Wasabi, Backblaze B2, Cloudflare R2, DigitalOcean Spaces, MinIO, Storj, Linode, Scaleway, IDrive e2, Vultr, OVH, Yandex, Alibaba. Per-provider credentials/endpoint walkthroughs are in [S3_GUIDE.md](./S3_GUIDE.md).

---

### Q. Do I need an S3 bucket to use the bot?

No. S3 is optional. `/tupload`, `/gdupload`, `/ytupload`, `/drupload` work without any S3 config. The five `S3_*` form fields are only needed for `/s3upload` and `/s3leech`.

---

### Q. What's the difference between `/s3upload` and `/s3leech`?

- **`/s3leech`** = S3 → **Telegram** (download objects from a bucket and send them to your chat).
- **`/s3upload`** = any source → **S3** (download from HTTP/Drive/etc. or another bucket, then store in your S3 bucket).

---

### Q. Can I copy from one S3 bucket to another?

Yes. Set the destination with `/s3bucket dest-bucket`, then `/s3upload` with an `s3://source-bucket/...` URI. Whole buckets iterate one object at a time.

---

### Q. Where do mirrored files land in my bucket?

`/s3upload` (bulk) writes to `s3://<bucket>/<prefix>/Uploaded__<timestamp>/<file>`. Iterate mode preserves the source folder layout. Set a prefix with `/s3prefix folder/sub`.

---

### Q. Does the bot keep my Telegram bot token / S3 keys safe?

They're written to `credentials.json` inside the ephemeral Colab runtime and used locally only — never broadcast. The runtime is wiped when it stops. If you persist credentials to Drive, don't share that folder. Revoke any leaked key in your provider console.

---

### Q. The Colab cell crashes with "no current event loop". What do I do?

Run the latest notebook from `main` (it has the Python 3.12 event-loop fix and a post-clone safety patch). Fully disconnect/delete the runtime and re-open the notebook fresh so Colab isn't running a cached old cell. Details in [TROUBLESHOOTING.md](./TROUBLESHOOTING.md).

---

### Q. How big a bucket can I leech on free Colab?

Effectively unlimited total size — iterate mode bounds peak disk to the **largest single object** (plus a little headroom for zip/extract). A 1 TB bucket of ≤ a few-GB objects is fine on the ~84 GB free disk. The constraint is time (and Colab's session limits), not total bucket size.
