# Extracting Apple Music links into `songlist.txt`

A complete guide to generating the song list that **`/amusic songs`** consumes:
which URL types are supported, how the extractor works internally, all options,
how to feed the result into Google Colab, and what to do when something breaks.

The extractor lives at **`tools/extract_songlist.py`** in this repo. It needs
**no login, no tokens and no third-party packages** — only Python 3, `urllib`
and two public data sources:

| Source | Used for |
|---|---|
| **iTunes Lookup API** (`itunes.apple.com/lookup`) | artist → album list, album → track list |
| **Apple Music web page** (`serialized-server-data` JSON) | playlists (private or editorial) |

Both are unauthenticated, so the extractor works from any machine that can
reach the open internet.

---

## 1. Quick start

```bash
# from a links file (one Apple Music URL per line — lines without a URL are ignored)
python3 tools/extract_songlist.py /root/links.txt -o songlist.txt

# or pass URLs directly
python3 tools/extract_songlist.py \
  "https://music.apple.com/in/artist/a-r-rahman/3249567/full-albums" \
  -o songlist.txt
```

Output (`songlist.txt`):

```
# 3 album group(s), 24 unique songs — generated 2026-08-04
Pudhiya Mugam (Original Motion Picture Soundtrack) (1993):
  https://music.apple.com/in/song/586976577
  https://music.apple.com/in/song/586976578
  ...
Roja (Original Motion Picture Soundtrack) (1992):
  ...
```

This is exactly the format `/amusic songs` parses:

- `# comment` lines are ignored
- a line **ending in `:`** is an album header (display only)
- every other non-empty line is either a full Apple Music URL **or a bare
  adamID** (`1234567`) that gets converted to `https://music.apple.com/in/song/1234567`
- the extractor **deduplicates globally**: a song that appears on two albums
  (soundtrack versions, singles collections) is kept only once, first-seen order

---

## 2. Input URL types

### 2.1 Artist URL → ALL albums (oldest first)
```
https://music.apple.com/in/artist/a-r-rahman/3249567
https://music.apple.com/in/artist/a-r-rahman/3249567/full-albums      # same thing
```
The `/full-albums` suffix doesn't matter — the extractor uses the
**iTunes Lookup API**:

```
GET https://itunes.apple.com/lookup?id=<artistID>&entity=album&limit=200&country=IN
GET https://itunes.apple.com/lookup?id=<albumID>&entity=song&country=IN
```

The first call returns every album (with `releaseDate`, `collectionId`,
`trackCount`); they're sorted **oldest-first** to match the artist mode of the
bot. The second call returns all tracks of one album, sorted by
`(discNumber, trackNumber)` — the same order am-downloader processes them.

> A.R. Rahman produces ~200 albums and ~1450 unique songs; full extraction
> takes roughly 2–3 minutes (0.15 s pause between album lookups).

### 2.2 Single album URL
```
https://music.apple.com/in/album/roja/4029439
```
One lookup for album metadata, one for its songs → one group in the output.

> ⚠️ **Artist-vs-album ID quirk**: some store IDs that *look* like album IDs
> actually resolve to an artist object in iTunes (e.g. Dil Se's `/album/.../4029995`).
> The extractor detects this and prints a friendly warning instead of crashing:
> `⚠ 4029995 resolves to an ARTIST in iTunes, not an album — use --artist ...`.
> When in doubt, just pass the artist URL; everything that artist released
> will be included anyway.

### 2.3 Playlist URL
```
https://music.apple.com/in/playlist/a-r-rahman-tamil-essentials/pl.1a59a1eb37624841876996cd2b22bff7
```
Playlist IDs are alphanumeric (`pl.…`). The extractor fetches the public
playlist page and parses the embedded `serialized-server-data` JSON (no
iTunes API for playlists). `musicVideo` items are skipped — this extractor's
output is **songs only** (videos belong to the existing playlist MV passes).

### 2.4 Single song URL / bare IDs
`/song/<name>/<id>` URLs are accepted as one-song groups; bare numeric lines
in the links file also work (they're converted to song URLs).

---

## 3. All options

```
python3 tools/extract_songlist.py -h
```

| Flag | Default | Meaning |
|---|---|---|
| `urls` (positional) | — | any number of artist/album/playlist/song URLs |
| `-l, --links <file>` | `/root/links.txt` | read URLs from a links file when **no positional URLs** are given (`-l=-` disables) |
| `--artist <id>` | — | artist by store ID (e.g. `3249567`) |
| `--country <cc>` | `IN` | iTunes storefront (`US`, `GB`, `JP`, ...) |
| `--limit-albums <n>` | `0` (all) | useful for smoke tests (`--limit-albums 2`) |
| `-o, --out <file>` | `./songlist.txt` | output path |
| `--sleep <sec>` | `0.15` | delay between album lookups (politeness) |

---

## 4. The internal extraction pipeline

```
links.txt / CLI URL(s)
        │
        ▼
parse_am_url(url) ───────► kind ∈ {artist, album, playlist, song}
        │
 ┌──────┴─────────────────────────────────────────┐
 ▼                                                ▼
artist / album                               playlist / song
itunes.apple.com/lookup                    public page HTML
  albums (sorted oldest-first)                 serialized-server-data JSON
    └ for each album:                            └ sections[*].items (kind==song)
        lookup entity=song                          storeAdamID → /song/ urls
          (sorted by disc/track)
 └──────────────────────────────┬────────────────────────────────┘
                                ▼
             global de-dupe by track id (first-seen wins)
                                ▼
                  songlist.txt (grouped, stable order)
```

Key implementation details:

- **Retries**: every lookup retries up to 3× with backoff on network errors.
- **Dedupe**: same `trackId` appears on multiple albums (film singles etc.) —
  one entry only.
- **Order**: albums oldest-first; tracks in disc/track order — deterministic,
  so re-running the extractor after adding music only appends new entries.
- **No auth**: don't be surprised that tokens are never needed; the API returns
  30-second previews with every response, we only use the IDs.

---

## 5. Feeding the result into Google Colab

`/amusic songs` (and the automatic mode) always read the file from a FIXED path:
```
/content/songlist.txt
```
Three ways to get it there:

1. **Manual upload** — in Colab, left panel → *Files* → upload icon →
   select `songlist.txt` (lands in `/content`).
2. **S3 URL** *(recommended — you already have the bucket)*:
   ```bash
   python3 -c "import boto3; boto3.client('s3', endpoint_url='https://s3.ap-northeast-1.wasabisys.com', region_name='ap-northeast-1', aws_access_key_id='…', aws_secret_access_key='…').upload_file('songlist.txt', 'musicapple', 'songlist.txt')"
   ```
   then set in the notebook/cell:
   ```python
   SONGLIST_URL = "s3://musicapple/songlist.txt"
   ```
   The cell downloads it to `/content/songlist.txt` *before* starting the bot.

   **Accepted `SONGLIST_URL` path formats** (verified):
   - `s3://<bucket>/<key>` — uses the cell's `S3_*` credentials → works for **private** buckets (the reliable one)
   - `https://<host>/<file.txt>` — plain direct-download URL (raw GitHub, gist raw, direct file hosts, Drive `uc?id=<ID>&export=download`)
   - ❌ plain Wasabi/S3 *console* URLs (`https://s3.<region>.wasabisys.com/<bucket>/<key>`) come back **403** when the object is private — use the `s3://` form instead
   - ❌ Google Drive / Dropbox *share-page* links (they return an HTML page, not the file) — convert to the direct-download form first

3. **Any http(s) URL** — also works for `SONGLIST_URL` (paste bin, raw gist, Drive direct link, ...).

Then either press **START** on `/amusic songs` — or tick `AM_SONGLIST_AUTO = True`
and the bot does it entirely by itself at startup.

---

## 6. Verifying a generated list

```bash
# sanity checks
head -5 songlist.txt
grep -c "music.apple.com/in/song/" songlist.txt     # total songs
python3 - <<'EOF'
import urllib.request
urls = [l.strip() for l in open("songlist.txt") if l.strip().startswith("http")]
u = urls[len(urls)//2]            # sample one from the middle
r = urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent":"Mozilla/5.0"}), timeout=15)
print("HTTP", r.status, u)        # must be 200
EOF
```

---

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `unrecognized source` | line isn't a music.apple.com URL | bare comments are fine; fix/remove the line |
| `404` on a playlist | wrong slug/id (playlist IDs start with `pl.`) | re-copy the *Share link* from Apple Music |
| `resolves to an ARTIST` | that "album" ID is actually an artist ID | pass the `--artist <id>` / artist URL |
| empty artist output | artist has >200 albums (iTunes limit) | pass several regional artist URLs, or re-run with different `--country` |
| slow / 429-ish errors | too many album lookups too fast | raise `--sleep` to `0.5` |
| songs with missing Atmos later | legacy/older tracks never had Atmos | *normal* — the bot marks them done via `no codec found` |

---

## 8. One-command full example (A.R. Rahman)

```bash
python3 tools/extract_songlist.py \
  "https://music.apple.com/in/artist/a-r-rahman/3249567/full-albums" \
  --country IN -o songlist.txt
# → 200 album groups, 1456 unique songs (verified 2026-08-04)
```
