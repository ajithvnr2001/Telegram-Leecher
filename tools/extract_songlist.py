#!/usr/bin/env python3
"""Extract an Apple Music song list into Colab Leecher's songlist.txt format.

Input: a links.txt file (or direct CLI args) containing Apple Music links:
  - https://music.apple.com/<cc>/artist/<name>/<id>[/full-albums]
    → ALL albums of the artist (oldest release first), with all their songs.
  - https://music.apple.com/<cc>/album/<name>/<id>
    → that single album's songs.
  - https://music.apple.com/<cc>/playlist/<name>/<id>  (or /song/, /music-video/)
    → the playlist's songs (public page parse, no auth).

Output (default ./songlist.txt), exactly the format `/amusic songs` consumes:

    # <source> — generated <date>
    Album Name (Year):
      https://music.apple.com/in/song/<adamID>
      ...

No auth and no third-party packages needed — uses the public iTunes Lookup
API (albums/tracks) and the public Apple Music pages (playlists).

Examples:
    python3 tools/extract_songlist.py /root/links.txt
    python3 tools/extract_songlist.py https://music.apple.com/in/artist/a-r-rahman/3249567/full-albums -o songlist.txt
    python3 tools/extract_songlist.py --artist 3249567 --country IN --limit-albums 5
"""

import argparse
import json
import re
import sys
import time
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
LOOKUP = "https://itunes.apple.com/lookup"


def get_json(url: str, retries: int = 3) -> dict:
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            return json.loads(urllib.request.urlopen(req, timeout=40).read())
        except Exception as e:
            if attempt == retries:
                raise
            time.sleep(1.5 * attempt)
            print(f"  retry {attempt}/{retries} ({e})", file=sys.stderr)
    return {}


def get_html(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "ignore")


def parse_am_url(url: str):
    """Return ("artist"|"album"|"playlist"|"song"|"music-video"|None, country, id)."""
    m = re.search(r"music\.apple\.com/(\w{2})/playlist/[^/]+/([\w.-]+)", url)
    if m:
        return "playlist", m.group(1).upper(), m.group(2)
    m = re.search(
        r"music\.apple\.com/(\w{2})/(artist|album|song|music-video)/[^/]+/(\d+)", url
    )
    if m:
        return m.group(2), m.group(1).upper(), m.group(3)
    return None, None, None


def artist_albums(artist_id: str, country: str) -> list:
    """All albums of an artist, oldest release first (public iTunes API)."""
    d = get_json(f"{LOOKUP}?id={artist_id}&entity=album&limit=200&country={country}")
    albums = [r for r in d.get("results", []) if r.get("collectionType") == "Album"]
    albums.sort(key=lambda a: (a.get("releaseDate") or "9999", a.get("collectionId")))
    return albums


def album_songs(collection_id: int, country: str) -> list:
    """Songs of one album in disc/track order."""
    d = get_json(f"{LOOKUP}?id={collection_id}&entity=song&country={country}")
    songs = [r for r in d.get("results", []) if r.get("kind") == "song" and r.get("trackId")]
    songs.sort(key=lambda s: (int(s.get("discNumber") or 1), int(s.get("trackNumber") or 0)))
    return songs


def playlist_songs(playlist_id: str, cc: str) -> list:
    """Songs of a playlist from its public page (serialized-server-data)."""
    url = f"https://music.apple.com/{cc.lower()}/playlist/_/{playlist_id}"
    html = get_html(url)
    m = re.search(r'serialized-server-data">(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError("playlist page: serialized-server-data not found")
    data = json.loads(m.group(1))
    sections = data["data"][0]["data"]["sections"]
    songs = []
    for sec in sections:
        if "track" not in str(sec.get("id", "")):
            continue
        for item in sec.get("items", []):
            cd = item.get("contentDescriptor") or {}
            ident = (cd.get("identifiers") or {}).get("storeAdamID")
            if ident and cd.get("kind") == "song":
                songs.append({"trackId": ident, "trackName": cd.get("name")})
    return songs


def read_sources(args) -> list:
    """Collect input URLs from positional args; fall back to the links file
    when no explicit URLs were given (and the file exists)."""
    urls = list(args.urls)
    if not urls and args.links and args.links != "-":
        try:
            with open(args.links, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("http") and "music.apple.com" in line:
                        urls.append(line)
        except FileNotFoundError:
            pass
    return urls


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Extract Apple Music albums/songs into Colab Leecher songlist.txt format.",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("urls", nargs="*", help="Apple Music artist/album/playlist URL(s)")
    ap.add_argument("--artist", help="artist store id (e.g. 3249567 for A.R. Rahman)")
    ap.add_argument("--country", default="IN", help="storefront (default IN)")
    ap.add_argument("--limit-albums", type=int, default=0, help="cap albums per artist (0 = all)")
    ap.add_argument("-l", "--links", default="/root/links.txt",
                    help="links.txt path — read only when no explicit URLs are given"
                         " (use -l=- to disable, default /root/links.txt)")
    ap.add_argument("-o", "--out", default="./songlist.txt", help="output file (default ./songlist.txt)")
    ap.add_argument("--sleep", type=float, default=0.15, help="pause between album lookups (default 0.15s)")
    args = ap.parse_args()

    # Build the work list: [(album_title, [(sort_key, song_url, song_name), ...]), ...]
    groups = []
    seen = set()  # global de-dupe by track id (same song on multiple albums)

    def add_group(title, songs):
        urls = []
        for s in songs:
            tid = str(s["trackId"])
            if tid in seen:
                continue
            seen.add(tid)
            urls.append(f"https://music.apple.com/in/song/{tid}")
        if urls:
            groups.append((title, urls))

    sources = read_sources(args)
    artist_ids = []
    if args.artist:
        artist_ids.append(args.artist)

    for url in sources:
        kind, cc, ident = parse_am_url(url)
        cc = cc or args.country
        if kind == "artist":
            if ident not in artist_ids:
                artist_ids.append(ident)
        elif kind == "album":
            hits = get_json(f"{LOOKUP}?id={ident}&country={cc}").get("results", [])
            alb = next((r for r in hits if r.get("collectionType") == "Album"), None)
            if alb is None and any(r.get("wrapperType") == "artist" for r in hits):
                print(f"  ⚠ {ident} resolves to an ARTIST in iTunes, not an album — "
                      "use --artist <id> or the artist URL instead; skipping.", file=sys.stderr)
            if alb:
                songs = album_songs(ident, cc)
                year = (alb.get("releaseDate") or "")[:4]
                add_group(f"{alb['collectionName']} ({year})", songs)
                print(f"  ✓ album {alb['collectionName']} — {len(songs)} songs")
            else:
                print(f"  ⚠ no album found for {ident}", file=sys.stderr)
        elif kind == "playlist":
            songs = playlist_songs(ident, cc)
            add_group(f"Playlist {ident}", songs)
            print(f"  ✓ playlist {ident} — {len(songs)} songs")
        elif kind in ("song", "music-video"):
            add_group(f"{kind} {ident}", [{"trackId": ident}])
        else:
            print(f"  ⚠ unrecognized source: {url}", file=sys.stderr)

    # Every artist URL contributes its full discography.
    for artist_id in artist_ids:
        albums = artist_albums(artist_id, args.country)
        if args.limit_albums:
            albums = albums[: args.limit_albums]
        print(f"Artist {artist_id}: {len(albums)} albums to extract")
        for i, alb in enumerate(albums, 1):
            aid = alb["collectionId"]
            name = alb["collectionName"]
            year = (alb.get("releaseDate") or "")[:4]
            songs = album_songs(aid, args.country)
            add_group(f"{name} ({year})", songs)
            if i % 20 == 0 or i == len(albums):
                print(f"  ... {i}/{len(albums)} albums done")
            time.sleep(max(args.sleep, 0))

    if not groups:
        print("Nothing found.", file=sys.stderr)
        return 1

    total = sum(len(u) for _t, u in groups)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(f"# {len(groups)} album group(s), {total} unique songs — "
                f"generated {time.strftime('%Y-%m-%d')}\n")
        for title, urls in groups:
            f.write(f"{title}:\n")
            for u in urls:
                f.write(f"  {u}\n")

    print(f"WROTE {args.out}: {len(groups)} album group(s), {total} unique songs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
