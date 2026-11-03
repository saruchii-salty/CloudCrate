# CloudCrate

> [!IMPORTANT]
> ### Copyright / Legal Disclaimer
>
> This tool is provided for **personal, private use only**. Downloading audio from SoundCloud may
> violate SoundCloud's Terms of Use, except where the platform explicitly offers an official
> download. By using this script you accept full responsibility for that use and for complying with
> SoundCloud's Terms of Use and the copyright laws of your jurisdiction.
>
> - Only download content you have legitimate access to, such as your own likes or your own uploads.
> - Do **not** redistribute, sell, or publicly re-share downloaded material. All uploaded audio
>   remains the property of its creators and rights holders. Nothing in this repository grants you
>   any rights to that content.
> - This project is not affiliated with, endorsed by, or connected to SoundCloud Limited in any way.
> - The software is provided "as is", without warranty of any kind, express or implied. The authors
>   and contributors are not liable for any misuse, account action, or legal consequences resulting
>   from its use.
>
> If you like a track, support the artist: stream it on SoundCloud, buy it, or see them live.

**CloudCrate** is a single-file Python script that downloads the tracks in a SoundCloud likes list
as fully tagged audio files: 320 kbps MP3 (or bit-perfect M4A) with title, artist, album, date,
genre, source URL and embedded cover art. The name comes from crate digging, the collector's hunt
for records. This one digs through your likes.

## Features

- Reads any public likes list, newest first, with no cap on total likes
- Picks the best available stream (AAC 160 kbps, falls back to AAC 96 kbps)
- Embeds ID3v2.3 tags and album art directly in the audio file, nothing stored sidecar
- Skips files already on disk, so re-running picks up new likes only
- Skips Go+ / preview-only tracks instead of saving 30-second clips
- Detects DRM-protected tracks (Widevine/FairPlay/PlayReady) and skips them with a clear note
- Logs failed tracks to `failed.txt` and keeps going
- Single file, no config, no account login required

## Dependencies

| Dependency | Why | Notes |
|---|---|---|
| Python | runs the script | 3.9 or newer (tested on 3.13) |
| [requests](https://pypi.org/project/requests/) | talks to the SoundCloud API | `pip install requests` |
| [mutagen](https://mutagen.readthedocs.io/) | writes ID3v2.3 / MP4 tags and cover art | `pip install mutagen` |
| [ffmpeg](https://ffmpeg.org/) | downloads the HLS stream and transcodes to MP3 | any recent build with libmp3lame (tested with 9.0) |

### Installing ffmpeg

The script looks for `ffmpeg` on your `PATH` first, then for `ffmpeg.exe` inside an `ffmpeg-bin/`
folder next to the script.

- **Windows**: `winget install Gyan.FFmpeg` (open a new terminal afterwards), or download the
  [essentials build](https://www.gyan.dev/ffmpeg/builds/) manually and drop `ffmpeg.exe` into
  `ffmpeg-bin/` beside the script.
- **macOS**: `brew install ffmpeg`
- **Debian/Ubuntu**: `sudo apt install ffmpeg`
- **Fedora**: `sudo dnf install ffmpeg`
- **Arch**: `sudo pacman -S ffmpeg`

## Setup

```bash
git clone https://github.com/saruchii-salty/CloudCrate.git
cd CloudCrate
pip install -r requirements.txt
```

## Usage

```bash
python cloudcrate.py USERNAME_OR_URL [options]
```

`USERNAME_OR_URL` is required: a bare username (`someuser`) or any profile or `/likes` URL. The
script reads only publicly visible likes.

### Flags

| Flag | Default | Description |
|---|---|---|
| `url` (positional, required) | none | SoundCloud username, or a profile/`/likes` URL to download from |
| `--out DIR` | `downloads` | Output folder, created if missing |
| `--format mp3\|m4a` | `mp3` | `mp3` re-encodes to 320 kbps with ID3 art. `m4a` copies the AAC stream bit-perfect, no quality loss, art stored as an MP4 cover atom |
| `--limit N` | all | Only process the first N liked tracks (includes skipped ones in the count) |
| `--delay SECONDS` | `1.0` | Pause between downloads. Raise it if you see 403/429 errors |
| `-h, --help` | | Show help with flag descriptions and examples |

### Examples

```bash
# everything from a likes list, default settings (bare username works)
python cloudcrate.py someuser

# test run: first 5 tracks only
python cloudcrate.py someuser --limit 5

# bit-perfect AAC instead of MP3
python cloudcrate.py https://soundcloud.com/someuser/likes --format m4a

# into a specific folder, gentle pacing
python cloudcrate.py someuser --out "D:/Music" --delay 3
```

## Output

Files are named `Artist - Title.mp3` (or `.m4a`) inside the output folder. Special characters are
sanitized for Windows filenames.

Embedded tags in MP3 mode (ID3v2.3, maximum player compatibility):

| Tag | Content |
|---|---|
| `TIT2` (title) | Track title |
| `TPE1` (artist) | Uploader's display name |
| `TALB` (album) | Track title, as SoundCloud singles have no album |
| `TDRC` (date) | Release date, or upload date as fallback |
| `TCON` (genre) | Track genre, when the uploader set one |
| `COMM` (comment) | Permalink to the original track page |
| `APIC` (cover) | Cover art, prefers the original resolution, falls back to 500x500, then the uploader's avatar |

M4A mode writes the same metadata as MP4 atoms (`nam`, `ART`, `alb`, `day`, `gen`, `cmt`, `covr`).

## Rate limits and numbers

Per [SoundCloud's API docs](https://developers.soundcloud.com/docs/api/rate-limits.html):

- Play-stream requests are capped at 15,000 per 24 hours per `client_id`. One download uses about
  two small API calls plus the CDN stream, and this quota is shared with the entire web player, so
  a personal archive is nowhere near it.
- No aggregate limit is enforced on other API calls.
- Likes pagination has no size cap; the 200-per-page value is a batch size, not a total.

The practical limit is pacing, not count. The default 1-second delay keeps the script polite. If
SoundCloud starts answering 403 or 429, raise `--delay` to 3 or more and retry, already-downloaded
tracks are skipped on the next run.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `ffmpeg not found` | Install ffmpeg via your package manager, or place `ffmpeg.exe` in `ffmpeg-bin/` next to the script |
| `no client_id found in player bundles` | SoundCloud changed its player assets. Delete the cached `cloudcrate.client_id` file and try again; if it persists, check for an updated version of this script |
| Repeated 403 / 429 | Too fast. Increase `--delay` and re-run |
| Track skipped: "Go+/preview or not streamable" | Only a 30-second preview exists for that track, so it is skipped rather than saved at preview length |
| Track skipped: "DRM-protected, only plays inside the web player" | The track ships as DRM-encrypted HLS (Widevine/FairPlay/PlayReady). Your browser plays it because its licensed DRM module fetches decryption keys during playback; no downloader can obtain those keys, so there is no legitimate way to save it |
| `profile not found: ...` | The username or URL is wrong, or the profile and its likes are private. Likes hidden in privacy settings are invisible to this script |
| `failed.txt` never appears | Failures are appended to it the moment they happen, so it exists while the run is still going, and it is cleared when a new run starts. Skips (Go+ preview, DRM, already-existing files) are not failures |
| Odd characters in the console output | A Windows codepage display quirk. The tags written into the files are Unicode and display correctly in players |

## Files this script creates

```
downloads/                          output folder
  Artist - Title.mp3                tagged audio
  failed.txt                        appended live as tracks fail; cleared at the start of each run
cloudcrate.client_id       cached API key, safe to delete
ffmpeg-bin/                         optional local ffmpeg location (not created by the script)
```

If you publish a fork, keep `downloads/`, `ffmpeg-bin/`, `*.client_id` and `__pycache__/` out of
git. A matching `.gitignore` ships with this repository.

## License

The script itself is released under the [MIT License](LICENSE). Downloaded audio is not covered by
that license and belongs to its respective rights holders.
