#!/usr/bin/env python3
"""Download the tracks in a SoundCloud likes list as fully tagged audio files.

Usage:
    python cloudcrate.py USER_OR_URL [--out DIR] [--format mp3|m4a] [--limit N] [--delay SECONDS]

USER_OR_URL is required: a bare SoundCloud username or any profile / likes
URL. Likes are read from api-v2.soundcloud.com using the web player's
client_id, streamed through ffmpeg, then tagged with mutagen: title, artist,
album, date, genre, source URL and embedded cover art. Existing files are
skipped, so the script can be re-run to pick up new likes.
"""

import argparse
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests
from mutagen.id3 import APIC, COMM, ID3, ID3NoHeaderError, TALB, TCON, TDRC, TIT2, TPE1
from mutagen.mp4 import MP4, MP4Cover

API_BASE = "https://api-v2.soundcloud.com"
SITE_BASE = "https://soundcloud.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
LOCAL_FFMPEG = Path(__file__).parent / "ffmpeg-bin"

BANNER = r"""
  _____ _                 _  _____           _
 / ____| |               | |/ ____|         | |
| |    | | ___  _   _  __| | |     _ __ __ _| |_ ___
| |    | |/ _ \| | | |/ _` | |    | '__/ _` | __/ _ \
| |____| | (_) | |_| | (_| | |____| | | (_| | ||  __/
 \_____|_|\___/ \__,_|\__,_|\_____|_|  \__,_|\__\___|
"""

http = requests.Session()
http.headers["User-Agent"] = UA
_client_id = None


def ffmpeg_path():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    local = LOCAL_FFMPEG / "ffmpeg.exe"
    if local.exists():
        return str(local)
    sys.exit("ffmpeg not found. Install it, or drop ffmpeg.exe into ffmpeg-bin/ next to this script.")


def get_client_id(force=False):
    """The api-v2 client_id lives in the web player's JS bundles."""
    global _client_id
    if _client_id and not force:
        return _client_id
    cache = Path(__file__).with_suffix(".client_id")
    if not force and cache.exists():
        cached = cache.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[a-zA-Z0-9]{32}", cached):
            _client_id = cached
            return _client_id
    page = http.get(SITE_BASE + "/", timeout=30)
    page.raise_for_status()
    assets = set(re.findall(r'https://a-v2\.sndcdn\.com/assets/[^"\'<>]+\.js', page.text))
    for asset in assets:
        js = http.get(asset, timeout=30).text
        m = re.search(r'client_id\s*[:=]\s*["\']([a-zA-Z0-9]{32})["\']', js)
        if m:
            _client_id = m.group(1)
            cache.write_text(_client_id, encoding="utf-8")
            return _client_id
    raise RuntimeError("no client_id found in player bundles")


def sc_get(path, params=None, retried=False):
    url = path if path.startswith("http") else API_BASE + path
    params = dict(params or {})
    params["client_id"] = get_client_id()
    r = http.get(url, params=params, timeout=30)
    if r.status_code in (401, 403) and not retried:
        get_client_id(force=True)
        return sc_get(path, params, retried=True)
    r.raise_for_status()
    return r.json()


def normalize_profile(raw):
    """Accept a bare username, an @handle, or any profile / likes URL."""
    raw = raw.strip()
    if "://" not in raw and "soundcloud.com" not in raw.lower():
        return f"{SITE_BASE}/{raw.lstrip('@').strip('/').rstrip('.,!?')}"
    url = raw.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    return re.sub(r"/likes$", "", url)


def resolve_user(profile_url):
    data = sc_get("/resolve", {"url": profile_url})
    if data.get("kind") != "user":
        raise RuntimeError(f"{profile_url} is not a user profile")
    return data


def liked_tracks(user_id):
    path, params = f"/users/{user_id}/likes", {"limit": 200}
    while path:
        data = sc_get(path, params)
        for item in data.get("collection", []):
            if item.get("track"):
                yield item["track"]
        next_href = data.get("next_href") or ""
        parts = urlsplit(next_href)
        path = parts.path if next_href else ""
        params = {k: v[0] for k, v in parse_qs(parts.query).items()} if next_href else None


PRESET_ORDER = ("aac_160k", "aac_96k")


class DrmTrackError(RuntimeError):
    """The track only ships as DRM-encrypted HLS (Widevine/FairPlay/PlayReady)."""


# markers of licensed-DRM key delivery inside an exchanged HLS playlist
DRM_MARKERS = ("METHOD=SAMPLE-AES", "skd://", "com.microsoft.playready", "urn:uuid:edef8ba9")


def pick_stream(track):
    """Exchange the track's best HLS transcoding for a signed playlist URL.

    The track object is re-fetched so its track_authorization token is current;
    tokens minted while the likes list was paginated can go stale during long
    runs. Raises DrmTrackError when every rendition is DRM-encrypted. Returns
    (preset, url).
    """
    fresh = sc_get(f"/tracks/{track['id']}")
    transcodings = (fresh.get("media") or {}).get("transcodings") or []
    candidates = [tc for tc in transcodings if (tc.get("format") or {}).get("protocol", "").endswith("hls")]

    def rank(tc):
        proto = (tc.get("format") or {}).get("protocol", "")
        preset = tc.get("preset") or ""
        preset_rank = PRESET_ORDER.index(preset) if preset in PRESET_ORDER else len(PRESET_ORDER)
        return (0 if proto == "hls" else 1, preset_rank)

    attempts = []
    drm_confirmed = False
    for tc in sorted(candidates, key=rank):
        try:
            data = sc_get(tc["url"], {"track_authorization": fresh.get("track_authorization", "")})
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else "?"
            attempts.append(f"{tc.get('preset')} -> HTTP {code}")
            continue
        url = data.get("url")
        if not url:
            attempts.append(f"{tc.get('preset')} -> empty response")
            continue
        proto = (tc.get("format") or {}).get("protocol", "")
        if proto != "hls":
            # encrypted renditions may be plain AES-128 (ffmpeg decrypts those
            # natively) or licensed DRM, which nothing here can decode
            if drm_confirmed:
                continue
            playlist = http.get(url, timeout=30).text
            if any(marker in playlist for marker in DRM_MARKERS):
                drm_confirmed = True
                continue
        return tc.get("preset"), url
    if drm_confirmed:
        raise DrmTrackError()
    if attempts:
        raise RuntimeError("transcoding exchange failed: " + "; ".join(attempts))
    raise RuntimeError("no HLS transcodings on the track")


def encode_args(fmt, stream_key):
    if fmt == "mp3":
        return ["-f", "mp3", "-c:a", "libmp3lame", "-b:a", "320k"]
    # m4a: bit-perfect copy when the source is AAC, transcode for mp3/opus sources
    if "aac" in stream_key:
        return ["-f", "ipod", "-c:a", "copy"]
    return ["-f", "ipod", "-c:a", "aac", "-b:a", "160k"]


def download_stream(url, dest, fmt, stream_key, ffmpeg):
    tmp = dest.with_name(dest.name + ".part")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-user_agent", UA,
           "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "30",
           "-i", url, "-vn", "-map", "a"] + encode_args(fmt, stream_key) + [str(tmp)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        detail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "no output"
        raise RuntimeError(f"ffmpeg: {detail[:200]}")
    tmp.replace(dest)


def artwork_urls(track):
    urls = []
    for base in (track.get("artwork_url"), (track.get("user") or {}).get("avatar_url")):
        if not base:
            continue
        # upgrade the 100x100 thumbnail SoundCloud hands out to the largest variant
        for size in ("-original", "-t500x500"):
            urls.append(re.sub(r"-(?:t\d+x\d+|large|badge|small|mini|tiny)", size, base))
    return list(dict.fromkeys(urls))


def fetch_artwork(track):
    for url in artwork_urls(track):
        try:
            r = http.get(url, timeout=30)
        except requests.RequestException:
            continue
        if r.ok and len(r.content) > 1024:
            return r.content
    return None


def image_mime(data):
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    return None


def track_tags(track):
    user = track.get("user") or {}
    date = (track.get("release_date") or track.get("created_at") or "")[:10]
    title = track.get("title") or "Untitled"
    return {
        "title": title,
        "artist": user.get("username") or "Unknown artist",
        "album": title,
        "genre": track.get("genre") or "",
        "date": date,
        "url": track.get("permalink_url") or "",
    }


def tag_mp3(path, track):
    tags = ID3()
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        pass
    t = track_tags(track)
    tags.add(TIT2(encoding=3, text=t["title"]))
    tags.add(TPE1(encoding=3, text=t["artist"]))
    tags.add(TALB(encoding=3, text=t["album"]))
    if t["genre"]:
        tags.add(TCON(encoding=3, text=t["genre"]))
    if t["date"]:
        tags.add(TDRC(encoding=3, text=t["date"]))
    if t["url"]:
        tags.add(COMM(encoding=3, lang="eng", desc="", text=t["url"]))
    art = fetch_artwork(track)
    if art:
        mime = image_mime(art)
        if mime:
            tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=art))
    tags.update_to_v23()
    tags.save(path, v2_version=3)


def tag_m4a(path, track):
    audio = MP4(path)
    t = track_tags(track)
    audio["\xa9nam"] = [t["title"]]
    audio["\xa9ART"] = [t["artist"]]
    audio["\xa9alb"] = [t["album"]]
    if t["genre"]:
        audio["\xa9gen"] = [t["genre"]]
    if t["date"]:
        audio["\xa9day"] = [t["date"]]
    if t["url"]:
        audio["\xa9cmt"] = [t["url"]]
    art = fetch_artwork(track)
    if art:
        mime = image_mime(art)
        if mime == "image/png":
            audio["covr"] = [MP4Cover(art, MP4Cover.FORMAT_PNG)]
        elif mime == "image/jpeg":
            audio["covr"] = [MP4Cover(art, MP4Cover.FORMAT_JPEG)]
    audio.save()


def safe_name(name):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", name)
    return re.sub(r"\s+", " ", name).strip(" .")[:180] or "untitled"


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except AttributeError:
        pass

    ap = argparse.ArgumentParser(
        description="Download a SoundCloud likes list as tagged audio files.",
        epilog="examples:\n"
               "  python cloudcrate.py USER                         bare username or full URL\n"
               "  python cloudcrate.py USER --limit 5               first 5 tracks only\n"
               "  python cloudcrate.py USER --format m4a            bit-perfect AAC, no re-encode\n"
               "  python cloudcrate.py USER --out D:\\Music --delay 3",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("url", help="SoundCloud username or profile / likes URL to download from (required)")
    ap.add_argument("--out", default="downloads")
    ap.add_argument("--format", choices=("mp3", "m4a"), default="mp3",
                    help="mp3 = 320 kbps MP3 with ID3 art (default), m4a = keep the AAC stream untouched")
    ap.add_argument("--limit", type=int, default=0, help="only download the first N tracks")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="seconds to wait between track downloads (default 1)")
    args = ap.parse_args()
    print(BANNER)

    ffmpeg = ffmpeg_path()
    profile_url = normalize_profile(args.url)
    try:
        user = resolve_user(profile_url)
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else "?"
        if code == 404:
            sys.exit(f"profile not found: {profile_url}")
        sys.exit(f"SoundCloud returned HTTP {code} for {profile_url}")
    print(f"Downloading likes of {user['username']} ({SITE_BASE}/{user['permalink']})")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    report = out / "failed.txt"
    report.unlink(missing_ok=True)
    failures = []
    seen = set()
    n = 0
    interrupted = False

    try:
        for track in liked_tracks(user["id"]):
            if track["id"] in seen:
                continue
            seen.add(track["id"])
            if args.limit and n >= args.limit:
                break
            n += 1
            title = track.get("title") or "Untitled"
            if track.get("policy") == "SNIPPET" or not track.get("streamable", False):
                print(f"[{n}] skip (Go+/preview or not streamable): {title}")
                continue
            artist = (track.get("user") or {}).get("username") or "Unknown"
            dest = out / (safe_name(f"{artist} - {title}") + f".{args.format}")
            if dest.exists():
                print(f"[{n}] exists: {dest.name}")
                continue
            try:
                key, stream = pick_stream(track)
                download_stream(stream, dest, args.format, key, ffmpeg)
                (tag_mp3 if args.format == "mp3" else tag_m4a)(dest, track)
                print(f"[{n}] done: {dest.name}")
            except DrmTrackError:
                print(f"[{n}] skip (DRM-protected, only plays inside the web player): {title}")
                continue
            except Exception as exc:
                print(f"[{n}] FAILED: {title} ({exc})")
                failures.append(f"{title} | {track.get('permalink_url', '')}")
                with report.open("a", encoding="utf-8") as fh:
                    fh.write(failures[-1] + "\n")
            time.sleep(max(0.0, args.delay))
    except KeyboardInterrupt:
        interrupted = True
        print("\ninterrupted")

    if failures:
        print(f"\n{len(failures)} failed, see {report}")
    if interrupted:
        print(f"Stopped after {n} tracks; re-run the same command to resume. Output in {out.resolve()}")
    else:
        print(f"Finished: {n} liked tracks processed, output in {out.resolve()}")


if __name__ == "__main__":
    main()
