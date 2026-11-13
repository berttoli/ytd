"""Split a video into fixed-length chunks with ffmpeg.

Fast by default: a stream copy, with no re-encoding and no loss of quality. Each cut lands on the
first keyframe at or after its mark, so chunks can run a little long. --exact re-encodes to
H.264/AAC with a keyframe forced at every mark, so each chunk is exactly the length asked for.

    python3 main.py chunk input.mp4 [-o chunks] [-s 180] [--exact]

Merged from an older ~/scripts/video.sh (2025), which re-encoded every chunk and sought after
opening the file each time.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def _existing_chunks(out_dir: Path, stem: str, ext: str) -> list[Path]:
    if not out_dir.is_dir():
        return []
    return sorted(p for p in out_dir.iterdir() if p.name.startswith(stem + "_") and p.suffix == ext)


def chunk_video(source: Path, out_dir: Path, seconds: float = 180.0, exact: bool = False) -> list[Path]:
    if not source.is_file():
        raise FileNotFoundError(f"Input file not found: {source}")
    if not seconds > 0:
        raise ValueError("The chunk length must be more than 0 seconds")
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg isn't on PATH")

    ext = ".mp4" if exact else (source.suffix or ".mp4")
    stem = source.stem
    found = _existing_chunks(out_dir, stem, ext)
    if found:
        raise FileExistsError(f"{out_dir} already holds chunks of {source.name} ({found[0].name}); choose another --out-dir")
    out_dir.mkdir(parents=True, exist_ok=True)

    seg = f"{seconds:g}"
    pattern = out_dir / f"{stem.replace('%', '%%')}_%03d{ext}"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(source), "-map", "0:v?", "-map", "0:a?"]
    if exact:
        cmd += ["-c:v", "libx264", "-c:a", "aac", "-force_key_frames", f"expr:gte(t,n_forced*{seg})"]
    else:
        cmd += ["-c", "copy"]
    cmd += ["-f", "segment", "-segment_time", seg, "-reset_timestamps", "1", str(pattern)]

    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"ffmpeg failed with exit code {exc.returncode}: {exc.stderr.strip()}") from exc
    return _existing_chunks(out_dir, stem, ext)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ytd chunk", description="Split a video into fixed-length chunks.")
    p.add_argument("input", type=Path, help="the video to split")
    p.add_argument("-o", "--out-dir", type=Path, default=Path("chunks"), help="where the chunks go (default: ./chunks)")
    p.add_argument("-s", "--seconds", type=float, default=180.0, help="chunk length in seconds (default: 180)")
    p.add_argument("--exact", action="store_true", help="re-encode for exact cuts (slower)")
    args = p.parse_args(argv)
    try:
        chunks = chunk_video(args.input, args.out_dir, args.seconds, args.exact)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"chunk: {exc}", file=sys.stderr)
        return 1
    for c in chunks:
        print(f"Created {c}")
    print(f"{len(chunks)} chunk{'' if len(chunks) == 1 else 's'} in {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
