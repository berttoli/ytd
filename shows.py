#!/usr/bin/env python3
"""`ytd show`: fetch one show episode, file it under Shows/<Show>/Season NN, and catalog it.

The media and its sidecars (metadata .json, subtitles .vtt) live under the library root and can
be copied to an rclone remote. The catalog (`catalog/shows.jsonl`, one line per episode) lives in
this repo and is what git tracks.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
CATALOG_DIR = Path(os.getenv("YTD_CATALOG_DIR") or REPO / "catalog")
CATALOG = CATALOG_DIR / "shows.jsonl"
ALIASES = CATALOG_DIR / "show-aliases.json"
DEFAULT_ROOT = REPO / "library"

_EPISODE_PATTERNS = (
    re.compile(r"\bS(\d{1,2})\s*[-.: ]?\s*E(\d{1,3})\b", re.I),
    re.compile(r"\bSeason\s+(\d{1,2})\W+Episode\s+(\d{1,3})\b", re.I),
)
_QUOTED = r"[\"“”']([^\"“”]{2,80})[\"“”']"


def parse_title(title: str, description: str = "") -> dict:
    """Pull show, season, episode and episode title out of a video title and description."""
    for pattern in _EPISODE_PATTERNS:
        match = pattern.search(title)
        if match:
            break
    else:
        return {"show": None, "season": None, "episode": None, "episode_title": None}
    show = title[: match.start()].strip(" -|:–—") or None
    code = re.escape(match.group(0))
    named = re.search(code + r"\s*[:\-–—]?\s*" + _QUOTED, description, re.I) or re.search(
        code + r"\s*[:\-–—]?\s*" + _QUOTED, title, re.I
    )
    return {
        "show": show,
        "season": int(match.group(1)),
        "episode": int(match.group(2)),
        "episode_title": named.group(1).strip() if named else None,
    }


def safe_name(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|]', "", text)).strip(" .")


def episode_stem(show: str, season: int, episode: int, episode_title: str | None, video_id: str) -> str:
    title = f" - {safe_name(episode_title)}" if episode_title else ""
    return f"{safe_name(show)} - S{season:02d}E{episode:02d}{title} [{video_id}]"


def format_selector(quality: int, codec: str) -> str:
    h = f"[height<={quality}]"
    h264 = f"bestvideo{h}[vcodec^=avc1]+bestaudio[ext=m4a]/best{h}[ext=mp4][vcodec^=avc1]"
    anything = f"bestvideo{h}+bestaudio/best{h}"
    if codec == "h264":
        return f"{h264}/{anything}"
    if codec == "av1":
        return f"bestvideo{h}[vcodec^=av01]+bestaudio[ext=m4a]/{h264}/{anything}"
    return anything


def load_catalog() -> list[dict]:
    if not CATALOG.exists():
        return []
    return [json.loads(line) for line in CATALOG.read_text(encoding="utf-8").splitlines() if line.strip()]


def save_entry(entry: dict) -> None:
    """Insert or replace the entry for this video, keeping the file sorted by show and episode."""
    rows = [row for row in load_catalog() if row.get("video_id") != entry["video_id"]] + [entry]
    rows.sort(key=lambda r: (str(r.get("show", "")).lower(), r.get("season") or 0, r.get("episode") or 0))
    CATALOG.parent.mkdir(parents=True, exist_ok=True)
    CATALOG.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def load_aliases() -> dict:
    return json.loads(ALIASES.read_text(encoding="utf-8")) if ALIASES.exists() else {}


def remember_alias(parsed_show: str, show: str) -> None:
    aliases = load_aliases()
    if aliases.get(parsed_show) == show:
        return
    aliases[parsed_show] = show
    ALIASES.parent.mkdir(parents=True, exist_ok=True)
    ALIASES.write_text(json.dumps(aliases, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def probe(path: Path) -> dict:
    """Resolution and codec of the first video stream; empty when ffprobe is unavailable."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height,codec_name", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout
        stream = (json.loads(out).get("streams") or [{}])[0]
        return {"width": stream.get("width"), "height": stream.get("height"), "vcodec": stream.get("codec_name")}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def upload(files: list[Path], local_dir: Path, remote_dir: str) -> None:
    """Copy exactly these files with rclone. A file list avoids glob trouble with [id] in names."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as handle:
        handle.write("".join(f"{path.name}\n" for path in files))
        listing = handle.name
    try:
        subprocess.run(["rclone", "copy", str(local_dir), remote_dir, "--files-from", listing], check=True)
    except FileNotFoundError as exc:
        raise SystemExit("Upload needs rclone on PATH.") from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"rclone copy to {remote_dir} failed with exit code {exc.returncode}.") from exc
    finally:
        os.unlink(listing)


def list_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="ytd show list", description="List cataloged episodes.")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    rows = load_catalog()
    if a.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("No episodes cataloged yet.")
        return 0
    for r in rows:
        size = f'{r["size_bytes"] / 1e9:.2f} GB' if r.get("size_bytes") else "-"
        res = f'{r["height"]}p {r.get("vcodec") or ""}'.strip() if r.get("height") else "-"
        print(f'{r["show"]}  S{r["season"]:02d}E{r["episode"]:02d}  {r.get("episode_title") or "":<28.28}  '
              f'{res:<10}  {size:>8}  {r["status"]:<8}  {r.get("remote") or ""}')
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "list":
        return list_main(argv[1:])

    p = argparse.ArgumentParser(
        prog="ytd show",
        description="Fetch one show episode, file it as Shows/<Show>/Season NN/<Show> - SxxEyy - <Title> [id].mp4 "
                    "with subtitles and a metadata file, and record it in catalog/shows.jsonl. "
                    "`ytd show list` prints the catalog.",
    )
    p.add_argument("url", help="YouTube URL or 11-character ID")
    p.add_argument("--show", help="Show name. Remembered for this title prefix, so later episodes need no flag")
    p.add_argument("--season", type=int)
    p.add_argument("--episode", type=int)
    p.add_argument("--title", help="Episode title")
    p.add_argument("--quality", type=int, choices=[480, 720, 1080, 1440, 2160], default=1080,
                   help="Maximum height (default: 1080)")
    p.add_argument("--codec", choices=["h264", "av1", "any"], default="h264",
                   help="h264 plays everywhere but stops at 1080p; av1 is smaller and goes to 4K (default: h264)")
    p.add_argument("--root", type=Path, default=Path(os.getenv("YTD_LIBRARY") or DEFAULT_ROOT),
                   help="Library root on disk (default: $YTD_LIBRARY or library/ in the ytd repo)")
    p.add_argument("--remote", default=os.getenv("YTD_SHOWS_REMOTE"),
                   help="rclone destination that holds the Shows folder, e.g. gcs:bucket/Cinema (default: $YTD_SHOWS_REMOTE)")
    p.add_argument("--upload", action="store_true", help="Copy the episode's files to --remote with rclone")
    p.add_argument("--dry-run", action="store_true", help="Show the plan and write nothing")
    p.add_argument("--force", action="store_true", help="Download again even if the file exists")
    p.add_argument("--cookies", type=Path)
    p.add_argument("--cookies-from-browser")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--json", action="store_true", help="Print one JSON result object on stdout; logs go to stderr")
    a = p.parse_args(argv)
    if a.upload and not a.remote:
        p.error("--upload needs --remote or $YTD_SHOWS_REMOTE")

    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError

    from main import _normalize_input_url, download_media

    url = _normalize_input_url(a.url)
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    if a.cookies:
        opts["cookiefile"] = str(a.cookies)
    if a.cookies_from_browser:
        from main import _parse_browser_spec
        opts["cookiesfrombrowser"] = _parse_browser_spec(a.cookies_from_browser)
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as exc:
        raise SystemExit(f"Could not read the video's metadata: {exc}") from exc

    formats = info.get("formats") or []
    if formats and all(f.get("has_drm") for f in formats if f.get("vcodec") not in (None, "none")):
        raise SystemExit("Every video format is DRM-protected; this episode cannot be fetched.")

    video = info["id"]
    title = info.get("title") or video
    parsed = parse_title(title, info.get("description") or "")
    show = a.show or load_aliases().get(parsed["show"] or "") or parsed["show"]
    season = a.season if a.season is not None else parsed["season"]
    episode = a.episode if a.episode is not None else parsed["episode"]
    episode_title = a.title or parsed["episode_title"]
    missing = [name for name, value in (("--show", show), ("--season", season), ("--episode", episode)) if value is None]
    if missing:
        raise SystemExit(f'Could not work out {", ".join(missing)} from the title "{title}". Pass them as flags.')

    stem = episode_stem(show, season, episode, episode_title, video)
    relative_dir = Path("Shows") / safe_name(show) / f"Season {season:02d}"
    local_dir = a.root / relative_dir
    media = local_dir / f"{stem}.mp4"
    remote_dir = f'{a.remote.rstrip("/")}/{relative_dir.as_posix()}' if a.remote else None

    entry = {
        "show": show, "season": season, "episode": episode, "episode_title": episode_title,
        "video_id": video, "url": f"https://www.youtube.com/watch?v={video}",
        "source_title": title, "channel": info.get("channel") or info.get("uploader"),
        "upload_date": info.get("upload_date"), "duration_seconds": info.get("duration"),
        "file": (relative_dir / media.name).as_posix(),
    }
    result = {"ok": True, "dry_run": a.dry_run, **entry, "local_path": str(media),
              "remote_dir": remote_dir, "quality": a.quality, "codec": a.codec}

    def emit(code: int = 0) -> int:
        if a.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            for key in ("show", "season", "episode", "episode_title", "local_path", "remote_dir", "status"):
                if result.get(key) is not None:
                    print(f"{key:>14}: {result[key]}")
        return code

    if a.dry_run:
        result["status"] = "planned"
        result["already_downloaded"] = media.exists()
        return emit()

    log = sys.stderr if a.json else sys.stdout
    with contextlib.redirect_stdout(log):
        local_dir.mkdir(parents=True, exist_ok=True)
        if media.exists() and not a.force:
            print(f"Already downloaded: {media.name}")
        else:
            staging = Path(tempfile.mkdtemp(prefix=".ytd-show-", dir=local_dir))
            try:
                fetched = download_media(
                    url, staging, "mp4", cookies_file=a.cookies, cookies_from_browser=a.cookies_from_browser,
                    verbose=a.verbose, format_selector=format_selector(a.quality, a.codec),
                )
                shutil.move(str(fetched), str(media))
            finally:
                shutil.rmtree(staging, ignore_errors=True)

        files = [media]
        from youtube_digest import _write_vtt, retrieve
        errors: list[str] = []
        _metadata, segments, source = retrieve(
            url, errors, cookies_file=a.cookies, cookies_from_browser=a.cookies_from_browser, verbose=a.verbose)
        if segments:
            subtitles = local_dir / f"{stem}.en.vtt"
            _write_vtt(subtitles, segments)
            files.append(subtitles)

        previous = next((row for row in load_catalog() if row.get("video_id") == video), {})
        if previous.get("file") != entry["file"]:
            previous = {}
        entry.update(probe(media))
        entry.update({
            "size_bytes": media.stat().st_size, "sha256": sha256(media),
            "subtitles": source if segments else None,
            "status": "local", "remote": None,
            # A re-run without --upload must not forget an earlier upload of the same file.
            **{k: v for k, v in previous.items() if k in ("status", "remote") and previous.get("status") == "uploaded"},
            "added": datetime.date.today().isoformat(),
        })
        sidecar = local_dir / f"{stem}.json"
        sidecar.write_text(json.dumps({**entry, "description": info.get("description")}, indent=2,
                                      ensure_ascii=False) + "\n", encoding="utf-8")
        files.append(sidecar)

        if a.upload:
            print(f"Uploading {len(files)} files to {remote_dir} ...", flush=True)
            upload(files, local_dir, remote_dir)
            entry.update({"status": "uploaded", "remote": f"{remote_dir}/{media.name}"})

        save_entry(entry)
        if parsed["show"] and a.show and parsed["show"] != a.show:
            remember_alias(parsed["show"], a.show)

    result.update(entry)
    result["files"] = [str(path) for path in files]
    return emit()


if __name__ == "__main__":
    raise SystemExit(main())
