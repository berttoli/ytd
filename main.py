#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from yt_dlp import DownloadError, YoutubeDL


_BROWSER_SPEC_RE = re.compile(
    r"""(?x)
    (?P<name>[^+:]+)
    (?:\s*\+\s*(?P<keyring>[^:]+))?
    (?:\s*:\s*(?!:)(?P<profile>.+?))?
    (?:\s*::\s*(?P<container>.+))?
    """
)


class _YtdLogger:
    """Preserve yt-dlp output while retaining warnings for error diagnosis."""

    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.errors: list[str] = []

    @property
    def drm_protected(self) -> bool:
        messages = (*self.warnings, *self.errors)
        return any("drm protected" in message.lower() for message in messages)

    def debug(self, message: str) -> None:
        # yt-dlp sends normal informational output through debug() too.
        # Per-chunk progress is noise when the output is captured (agents, CI,
        # log files): keep only the final line unless a person is watching.
        if (
            message.startswith("[download]")
            and "%" in message
            and "100%" not in message
            and not sys.stderr.isatty()
        ):
            return
        print(message, file=sys.stderr)

    def info(self, message: str) -> None:
        print(message, file=sys.stderr)

    def warning(self, message: str) -> None:
        self.warnings.append(message)
        print(f"WARNING: {message}", file=sys.stderr)

    def error(self, message: str) -> None:
        self.errors.append(message)
        print(f"ERROR: {message}", file=sys.stderr)


def _is_403(err: Exception) -> bool:
    message = str(err).lower()
    return (
        "http error 403" in message
        or "403 forbidden" in message
        or "status code: 403" in message
    )


def _is_bot_check(err: Exception) -> bool:
    message = str(err).lower()
    return (
        "confirm you're not a bot" in message
        or "sign in to confirm" in message
        or "captcha" in message
    )


def _normalize_input_url(video_id_or_url: str) -> str:
    value = video_id_or_url.strip()
    if not value:
        raise ValueError("You must provide a YouTube video ID or URL.")

    if "://" in value:
        return value

    if (
        value.startswith(("www.", "m.", "music."))
        or "youtube.com" in value
        or "youtu.be" in value
    ):
        return f"https://{value}"

    return f"https://www.youtube.com/watch?v={value}"


def _parse_browser_spec(spec: str) -> tuple[str, str | None, str | None, str | None]:
    match = _BROWSER_SPEC_RE.fullmatch(spec.strip())
    if match is None:
        raise ValueError(
            "Invalid --cookies-from-browser value. "
            "Expected browser or browser[+keyring][:profile][::container]."
        )

    browser = match.group("name").strip().lower()
    keyring = match.group("keyring")
    profile = match.group("profile")
    container = match.group("container")
    return (
        browser,
        profile.strip() if profile else None,
        keyring.strip().upper() if keyring else None,
        container.strip() if container else None,
    )


def _split_csv_values(values: list[str] | None, env_value: str | None = None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        out.extend(item.strip() for item in value.split(",") if item.strip())
    if env_value:
        out.extend(item.strip() for item in env_value.split(",") if item.strip())
    return out


def _build_extractor_args(
    *,
    po_tokens: list[str],
    visitor_data: str | None,
    has_cookies: bool,
) -> dict[str, dict[str, list[str]]] | None:
    use_visitor_data = bool(visitor_data and not has_cookies)
    if not po_tokens and not use_visitor_data:
        return None

    youtube_args: dict[str, list[str]] = {}

    if po_tokens:
        # `default` keeps us aligned with yt-dlp's current release defaults,
        # while `web` and `mweb` give the token a chance to match the client
        # it was minted for.
        youtube_args["player_client"] = ["default", "web", "mweb"]
        youtube_args["po_token"] = po_tokens
    else:
        youtube_args["player_client"] = ["default"]

    if use_visitor_data:
        youtube_args["player_skip"] = ["webpage", "configs"]
        youtube_args["visitor_data"] = [visitor_data]

    return {"youtube": youtube_args}


def _format_options(fmt: str) -> dict[str, Any]:
    if fmt == "mp4":
        return {
            # Prefer the most widely supported MP4 codecs, then gracefully fall
            # back when a video is only available in newer codec combinations.
            "format": (
                "bestvideo[ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]"
                "/best[ext=mp4][vcodec^=avc1]"
                "/bestvideo[ext=mp4]+bestaudio[ext=m4a]"
                "/best[ext=mp4]"
                "/bestvideo+bestaudio/best"
            ),
            "merge_output_format": "mp4",
            "postprocessors": [
                {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}
            ],
        }

    return {
        # Audio conversion is performed explicitly after yt-dlp finishes. This
        # gives ffmpeg a timeout and prevents a silent postprocessor hang.
        "format": "bestaudio/best",
    }


def _convert_audio(source: Path, output_dir: Path, fmt: str, timeout: int) -> Path:
    target = output_dir / f"{source.stem}.{fmt}"
    codec_args = (
        ["-ar", "44100", "-c:a", "pcm_s16le"]
        if fmt == "wav"
        else ["-ar", "44100", "-c:a", "libmp3lame", "-q:a", "0"]
    )
    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source), "-vn", *codec_args, str(target),
    ]
    print(f"Converting audio with ffmpeg ({timeout}s timeout): {target.name}", flush=True)
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SystemExit("Audio conversion needs ffmpeg on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise SystemExit(
            f"ffmpeg did not finish within {timeout} seconds while creating {target.name}."
        ) from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else "unknown ffmpeg error"
        raise SystemExit(f"ffmpeg failed while creating {target.name}: {detail}")
    return target


def download_media(
    video_id_or_url: str,
    output_dir: Path,
    fmt: str = "wav",
    cookies_file: Path | None = None,
    cookies_from_browser: str | None = None,
    verbose: bool = False,
    po_tokens: list[str] | None = None,
    visitor_data: str | None = None,
    concurrent_fragments: int = 4,
    clear_cache: bool = False,
    postprocess_timeout: int = 600,
    format_selector: str | None = None,
) -> Path:
    url = _normalize_input_url(video_id_or_url)
    logger = _YtdLogger()

    output_dir.mkdir(parents=True, exist_ok=True)
    existing_outputs = {
        p.resolve()
        for p in output_dir.glob(f"*.{fmt}")
        if p.is_file()
    }

    raw_dir: Path | None = None
    download_dir = output_dir
    if fmt in {"wav", "mp3"}:
        raw_dir = Path(tempfile.mkdtemp(prefix=".ytd-raw-", dir=output_dir))
        download_dir = raw_dir

    base_opts: dict[str, Any] = {
        "outtmpl": str(download_dir / "%(title).200B [%(id)s].%(ext)s"),
        "noplaylist": True,
        "quiet": False,
        "no_warnings": False,
        "ignoreerrors": False,
        "verbose": verbose,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,
        "socket_timeout": 30,
        "retry_sleep_functions": {"http": lambda n: min(2**n, 8)},
        "force_ipv4": True,
        "prefer_ffmpeg": True,
        "logger": logger,
    }
    base_opts.update(_format_options(fmt))
    if format_selector:
        base_opts["format"] = format_selector

    if concurrent_fragments > 1:
        base_opts["concurrent_fragment_downloads"] = concurrent_fragments

    if cookies_file:
        base_opts["cookiefile"] = str(cookies_file)
    if cookies_from_browser:
        base_opts["cookiesfrombrowser"] = _parse_browser_spec(cookies_from_browser)

    extractor_args = _build_extractor_args(
        po_tokens=po_tokens or [],
        visitor_data=visitor_data,
        has_cookies=bool(cookies_file or cookies_from_browser),
    )
    if extractor_args is not None:
        base_opts["extractor_args"] = extractor_args

    if verbose and extractor_args is not None:
        print(f"Using extractor args: {extractor_args}")

    try:
        print("Starting media download...", flush=True)
        with YoutubeDL(base_opts) as ydl:
            if clear_cache:
                ydl.cache.remove()
            if fmt in {"wav", "mp3"}:
                info = ydl.extract_info(url, download=True)
                source = Path(ydl.prepare_filename(info))
            else:
                ydl.download([url])
    except DownloadError as e:
        if raw_dir is not None:
            shutil.rmtree(raw_dir, ignore_errors=True)
        if logger.drm_protected:
            raise SystemExit(
                "This YouTube video is DRM-protected. YouTube exposed only thumbnail/image "
                "formats, so yt-dlp has no downloadable audio or video stream.\n"
                "The script cannot decrypt or bypass DRM. Use a non-DRM source that you are "
                "authorized to download, or obtain the video through the provider's official "
                "download/export option."
            ) from e

        if _is_bot_check(e):
            raise SystemExit(
                "YouTube is requiring a bot-check or login on this IP.\n"
                "Try one of the following:\n"
                "  1) pass --cookies-from-browser with a logged-in browser\n"
                "  2) update yt-dlp to the latest release\n"
                "  3) try a different network/IP\n"
                f"\nRaw error: {e}"
            ) from e

        if _is_403(e):
            raise SystemExit(
                "YouTube returned HTTP 403.\n"
                "Try updating yt-dlp, using browser cookies, adding a matching PO token,\n"
                "or rerunning with --clear-cache if you suspect stale yt-dlp cache data.\n"
                f"\nRaw error: {e}"
            ) from e

        raise SystemExit(f"Download failed: {e}") from e
    except Exception:
        if raw_dir is not None:
            shutil.rmtree(raw_dir, ignore_errors=True)
        raise

    try:
        if fmt in {"wav", "mp3"}:
            if not source.exists():
                raw_candidates = [p for p in raw_dir.iterdir() if p.is_file() and p.suffix not in {".part", ".ytdl"}]
                if not raw_candidates:
                    raise SystemExit("Download finished but the source audio was not found.")
                source = max(raw_candidates, key=lambda p: p.stat().st_mtime)
            return _convert_audio(source, output_dir, fmt, max(1, postprocess_timeout))

        candidates = [p for p in output_dir.glob(f"*.{fmt}") if p.is_file()]
        new_candidates = [p for p in candidates if p.resolve() not in existing_outputs]
        selected = new_candidates or candidates
        if not selected:
            raise SystemExit("Download finished but output not found.")
        return max(selected, key=lambda p: p.stat().st_mtime)
    finally:
        if raw_dir is not None:
            shutil.rmtree(raw_dir, ignore_errors=True)


def download_audio(
    video_id_or_url: str,
    output_dir: Path,
    fmt: str = "wav",
    **kwargs: Any,
) -> Path:
    """Backward-compatible wrapper for callers using the original function."""
    if fmt not in {"wav", "mp3"}:
        raise ValueError("download_audio() only supports wav and mp3")
    return download_media(video_id_or_url, output_dir, fmt, **kwargs)


def main(argv: list[str] | None = None) -> int:
    # Keep the original downloader syntax working, while exposing the research
    # pipeline from the same executable: `python main.py digest URL ...`.
    args_argv = list(sys.argv[1:] if argv is None else argv)
    if args_argv and args_argv[0].lower() == "digest":
        from youtube_digest import main as digest_main

        return digest_main(args_argv[1:])
    if args_argv and args_argv[0].lower() == "transcript":
        from youtube_digest import transcript_main

        return transcript_main(args_argv[1:])
    if args_argv and args_argv[0].lower() == "show":
        from shows import main as show_main

        return show_main(args_argv[1:])
    if args_argv and args_argv[0].lower() == "chunk":
        from video_chunks import main as chunk_main

        return chunk_main(args_argv[1:])
    if args_argv and args_argv[0].lower() == "brief":
        from ytd_library.brief import main as brief_main

        return brief_main(args_argv[1:])
    if args_argv and args_argv[0].lower() == "serve":
        from ytd_library.serve import main as serve_main

        return serve_main(args_argv[1:])

    p = argparse.ArgumentParser(
        prog="ytd",
        description="Download YouTube audio or MP4 video.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "other commands (each has its own --help):\n"
            "  ytd transcript URL          print the captions as clean text on stdout\n"
            "  ytd digest URL --out DIR    write transcript + research artifacts; add --json\n"
            "  ytd show URL                file a show episode and add it to the catalog\n"
            "  ytd chunk FILE              split a local video into fixed-length pieces\n"
            "  ytd brief ID | --all        write an AI brief (brief.md, brief.json) per digested video\n"
            "  ytd serve                   open the local library viewer on 127.0.0.1:4410"
        ),
    )
    p.add_argument("id", help="YouTube video ID or full URL")
    p.add_argument("-f", "--format", choices=["wav", "mp3", "mp4"], default="wav")
    p.add_argument("-o", "--out-dir", type=Path, default=Path.cwd())

    p.add_argument("--cookies", type=Path, default=None, help="Path to cookies.txt")
    p.add_argument(
        "--cookies-from-browser",
        default=None,
        help=(
            "Load cookies from a browser spec, for example "
            "chrome, firefox:Default, or brave+KEYRING:Profile"
        ),
    )
    p.add_argument(
        "--po-token",
        action="append",
        default=[],
        help=(
            "Optional YouTube PO token. Can be repeated or comma-separated; "
            "for example web.player+TOKEN or mweb.gvs+TOKEN"
        ),
    )
    p.add_argument("--visitor-data", default=None, help="Optional YouTube visitor_data value")
    p.add_argument(
        "--concurrent-fragments",
        type=int,
        default=4,
        help="Parallel fragment downloads for fragmented streams (default: 4)",
    )
    p.add_argument(
        "--postprocess-timeout",
        type=int,
        default=600,
        help="Seconds allowed for explicit ffmpeg audio conversion (default: 600)",
    )
    p.add_argument(
        "--save-transcript",
        action="store_true",
        help="Save captions beside the downloaded media",
    )
    p.add_argument(
        "--transcribe",
        action="store_true",
        help="Use local faster-whisper if captions are unavailable",
    )
    p.add_argument("--stt-model", default="base", help="faster-whisper model name or local path")
    p.add_argument("--stt-device", default="auto", help="faster-whisper device (default: auto)")
    p.add_argument("--stt-compute-type", default="default", help="faster-whisper compute type")
    p.add_argument("--stt-language", help="Optional language code passed to faster-whisper")
    p.add_argument(
        "--transcript-dir",
        type=Path,
        help="Directory for --save-transcript artifacts (default: --out-dir)",
    )
    p.add_argument(
        "--clear-cache",
        action="store_true",
        help="Clear yt-dlp filesystem cache before downloading",
    )
    p.add_argument("--verbose", action="store_true", help="Enable yt-dlp verbose debug output")

    args = p.parse_args(args_argv)

    po_tokens = _split_csv_values(args.po_token, os.getenv("YTDLP_PO_TOKEN"))
    visitor_data = args.visitor_data or os.getenv("YTDLP_VISITOR_DATA")

    print(f"Fetching: {args.id}")
    print(f"Output format: {args.format}")

    path = download_media(
        args.id,
        args.out_dir,
        args.format,
        cookies_file=args.cookies,
        cookies_from_browser=args.cookies_from_browser,
        verbose=args.verbose,
        po_tokens=po_tokens,
        visitor_data=visitor_data,
        concurrent_fragments=max(1, args.concurrent_fragments),
        clear_cache=args.clear_cache,
        postprocess_timeout=max(1, args.postprocess_timeout),
    )

    print(f"\nSaved: {path.resolve()}")
    if args.save_transcript or args.transcribe:
        from youtube_digest import save_transcript_artifacts, transcribe_audio, write_transcript_artifacts

        transcript_dir = args.transcript_dir or args.out_dir
        print("Fetching transcript captions...", flush=True)
        transcript = save_transcript_artifacts(
            args.id,
            transcript_dir,
            cookies_file=args.cookies,
            cookies_from_browser=args.cookies_from_browser,
            po_tokens=po_tokens,
            visitor_data=visitor_data,
            verbose=args.verbose,
        )
        if args.transcribe and transcript["segments"] == 0:
            print("No captions found; falling back to faster-whisper...", flush=True)
            segments, source = transcribe_audio(
                path,
                model_name=args.stt_model,
                device=args.stt_device,
                compute_type=args.stt_compute_type,
                language=args.stt_language,
            )
            transcript = write_transcript_artifacts(
                args.id,
                transcript_dir,
                segments,
                source,
                transcript["errors"],
            )
        print(f"Transcript saved: {transcript['transcript'].resolve()} ({transcript['segments']} segments)")
        if transcript["subtitles"]:
            print(f"Subtitles saved: {transcript['subtitles'].resolve()}")
        for error in transcript["errors"]:
            print(f"Transcript warning: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
