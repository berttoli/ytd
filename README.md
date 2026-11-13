# ytd

## Overview

`ytd` is a small Python toolkit for downloading YouTube audio and video with `yt-dlp`.
It accepts either a video ID or a full URL. Audio can be converted to `wav` or `mp3` at
`44.1 kHz`, while video can be downloaded and merged into an `mp4` movie file.
The same entry point also runs the video research/digest pipeline.

The primary downloader includes:

- retry handling for flaky YouTube responses
- browser cookie support
- cookie-file support
- optional PO token and visitor-data passthrough
- native concurrent fragment downloads for better throughput on fragmented streams
- MP4 video downloads that prefer broadly compatible H.264 video and AAC audio

## Use from anywhere (people and agents)

`bin/ytd` is a launcher that finds this repo through its own symlink and runs `main.py` with the
project `.venv`. Link it onto your `PATH` once:

```bash
ln -sfn "$PWD/bin/ytd" ~/.local/bin/ytd
```

After that `ytd ...` replaces `python3 main.py ...` in every example below, from any directory.

Three things make it easy for a coding agent (Claude Code, Codex) to drive:

- `ytd transcript URL` prints the captions as clean text on stdout (title, chapter headings,
  `[mm:ss]` paragraphs) and writes nothing. Exit code 2 means the video has no captions.
- `ytd digest URL --out DIR --json` prints one JSON object on stdout with the status, absolute
  file paths, warnings and errors; every log line goes to stderr. The digest also writes
  `transcript.txt`, the reading copy.
- Download progress lines are dropped when stderr is not a terminal, so captured output stays short.

`skills/ytd/SKILL.md` tells an agent when and how to call the command. Link it into each agent's
skills folder:

```bash
ln -sfn "$PWD/skills/ytd" ~/.claude/skills/ytd
ln -sfn "$PWD/skills/ytd" ~/.codex/skills/ytd
ln -sfn "$PWD/skills/ytd" ~/.agents/skills/ytd
```

YouTube auto-captions repeat each line across cues; the caption parser now keeps only the new
lines, so transcripts are about a third of their former size.

## Requirements

- Python 3.11+
- `ffmpeg` available on your `PATH`
- `yt-dlp`

If you only want to use the downloader, `yt-dlp` and `ffmpeg` are the only pieces you need.

## Usage

The main entry point is `main.py`:

```bash
python3 main.py <video-id-or-url> [options]
```

Available flags:

- `-f, --format wav|mp3|mp4`
- `-o, --out-dir PATH`
- `--cookies PATH`
- `--cookies-from-browser BROWSER_SPEC`
- `--po-token VALUE` can be repeated
- `--visitor-data VALUE`
- `--concurrent-fragments N`
- `--clear-cache`
- `--verbose`
- `--save-transcript` writes `<video-id>.transcript.json` beside the media; if captions are
  available it also writes `<video-id>.subtitles.vtt`
- `--transcribe` uses faster-whisper when no captions are found; tune it with `--stt-model`,
  `--stt-device`, `--stt-compute-type`, and `--stt-language`
- `--postprocess-timeout SECONDS` bounds explicit ffmpeg audio conversion

Run `python3 main.py --help` for the full CLI reference.

## Examples

Download a video by ID as WAV:

```bash
python3 main.py dQw4w9WgXcQ
```

Download from a full URL as MP3 into a custom directory:

```bash
python3 main.py "https://www.youtube.com/watch?v=dQw4w9WgXcQ" \
  --format mp3 \
  --out-dir downloads
```

Download MP3 and save any available captions as transcript sidecars:

```bash
python3 main.py "https://www.youtube.com/watch?v=dQw4w9WgXcQ" \
  --format mp3 --out-dir downloads --save-transcript
```

Download an MP4 movie into the local platform's media directory:

```bash
python3 main.py "https://www.youtube.com/watch?v=dQw4w9WgXcQ" \
  --format mp4 \
  --out-dir /path/to/video-platform/media
```

For MP4 downloads, the script prefers H.264 video with AAC audio for broad playback
compatibility. If that combination is unavailable, it falls back to the best MP4-compatible
streams and uses `ffmpeg` to merge or remux them into one file.

Use cookies from a logged-in browser session:

```bash
python3 main.py dQw4w9WgXcQ --cookies-from-browser chrome
```

Use a saved `cookies.txt` file:

```bash
python3 main.py dQw4w9WgXcQ --cookies ~/Downloads/cookies.txt
```

Increase fragment concurrency for fragmented media:

```bash
python3 main.py dQw4w9WgXcQ --concurrent-fragments 8
```

Clear yt-dlp's local cache before the download:

```bash
python3 main.py dQw4w9WgXcQ --clear-cache
```

Pass PO token and visitor data when you already have them:

```bash
python3 main.py dQw4w9WgXcQ \
  --po-token web.player+TOKEN \
  --po-token mweb.gvs+TOKEN \
  --visitor-data VISITOR_DATA_VALUE
```

## Helper Script

`mp4_conversion.py` is a small ffmpeg helper for converting an existing local video file to WAV:

```bash
python3 mp4_conversion.py input.mp4 [output.wav]
```

## Chunking a video

`python3 main.py chunk` splits a local video into fixed-length pieces (3 minutes unless `-s` says otherwise):

```bash
python3 main.py chunk input.mp4                  # ./chunks/input_000.mp4, input_001.mp4, ...
python3 main.py chunk input.mp4 -s 60 -o parts   # 1-minute pieces in ./parts
python3 main.py chunk input.mp4 --exact          # re-encode, so every cut is exact
```

By default it copies the streams, so it's near-instant and loses nothing. Each cut lands on the next
keyframe, so pieces can run slightly long. `--exact` re-encodes to H.264/AAC with a keyframe at every
cut. It won't overwrite chunks already in the output folder.

## Cataloging show episodes

`ytd show URL` fetches one episode, files it, and records it in the catalog:

```bash
ytd show "https://www.youtube.com/watch?v=VIDEO_ID" --dry-run     # show the plan, write nothing
ytd show "https://www.youtube.com/watch?v=VIDEO_ID"               # download, file and catalog
ytd show "https://www.youtube.com/watch?v=VIDEO_ID" --upload      # also copy to the rclone remote
ytd show list                                                     # print the catalog
```

Show, season, episode and episode title are parsed from the video title and description (`S1E1`,
`Season 1 Episode 1`, and a quoted title after the episode code). Override any of them with
`--show`, `--season`, `--episode` and `--title`. A `--show` override is remembered in
`catalog/show-aliases.json`, so later episodes with the same title prefix need no flag.

Files land under the library root as:

```
Shows/<Show>/Season 01/<Show> - S01E01 - <Episode title> [<video-id>].mp4
                                                                    .en.vtt   subtitles, when captions exist
                                                                    .json     metadata and description
```

- The library root is `library/` in this repo (git-ignored), or `$YTD_LIBRARY`, or `--root`.
- `--quality` caps the height (default 1080). `--codec h264` plays everywhere but stops at 1080p on
  YouTube; `--codec av1` is smaller and goes to 4K.
- `--upload` copies the episode's files with `rclone copy` to `--remote` (or `$YTD_SHOWS_REMOTE`),
  which is the destination that holds the `Shows` folder, for example `gcs:my-bucket/Cinema`.
- `catalog/shows.jsonl` holds one line per episode (names, duration, resolution, codec, size,
  SHA-256, status `local` or `uploaded`, remote path). It is tracked by git; the media is not.
- A DRM-protected episode is refused. Re-running skips the download when the file exists
  (`--force` fetches again).

## Digest workflow

Run the research pipeline through `main.py`:

```bash
python3 main.py digest "https://www.youtube.com/watch?v=VIDEO_ID" \
  --out research/VIDEO_ID --audio mp3
```

This can download audio (`--audio`, or `--audio wav`) and/or video (`--video`) while
writing the research output to the same run directory. Without `--out`, the run directory is
`research/<video-id>` inside this repo, whatever directory you run from. The digest creates:

- `analysis.json`: metadata, chapters, linked resources, transcript evidence, and candidate actions
- `transcript.json` and, when captions exist, `subtitles.vtt`
- `summary.md`: extractive report with timestamps; metadata/chapter-only if captions are unavailable
- `agent-prompt.md`: instructions for a separate source-grounded analysis pass
- `manifest.json` and `README.md`: the current artifact inventory and pending downstream work

To have the local Codex CLI turn the evidence into publication drafts, add `--codex-post`:

```bash
python3 main.py digest "https://www.youtube.com/watch?v=VIDEO_ID" \
  --out research/VIDEO_ID --audio wav --transcribe --codex-post
```

This writes `linkedin-post.md` and `medium-article.md` after the transcript and overview
are saved. The Codex job is read-only and its prompt, raw response, and status are saved as
`codex-article-prompt.md`, `codex-article-response.txt`, and
`codex-article-response.json`. Use `--codex-search` only when you want the article pass to
consult the web for current context; the video evidence remains the source of truth.

The five files named in `agent-prompt.md` (`semantic-summary.md`, `implementation-plan.md`,
`codex-prompt.md`, `knowledge.json`, and `verification.md`) are downstream artifacts. They
appear only after an authorized analysis agent performs that pass; this CLI does not invent
semantic summaries or fact-checking.

For the supplied starting video, an offline rerun from the metadata already captured in this
repository is possible with:

```bash
python3 main.py digest "https://www.youtube.com/watch?v=VIDEO_ID" \
  --metadata-json youtube-output/analysis.json \
  --transcript youtube-output/transcript.json \
  --out research/VIDEO_ID
```

If captions are unavailable, `summary.md` still exposes the title, duration, chapters, and
description links, and `README.md`/`manifest.json` clearly mark the semantic artifacts as pending.

## Briefs and the library viewer

`ytd brief` writes a short AI brief next to each digest: an overview, key takeaways with
timestamps, a verdict (`use-now`, `try`, `watch` or `skip`) and recommendations tagged with the
areas work, organization, personal, technical, security, career and money.

```bash
ytd brief VIDEO_ID          # one video (ids under research/)
ytd brief --all                # every video without a brief; --force rewrites
ytd brief VIDEO_ID --dry-run   # print the prompts and the claude command; call nothing
```

- It needs `context.md` in the repo root: the owner's projects, goals, areas and standing rules.
  That file is in `.gitignore` and stays local.
- Briefs are written by headless Claude Code (`claude -p`), so the transcript and `context.md`
  are sent to Anthropic. Claude gets read-only tools (`Read`, `Grep`, `Glob`) confined to that one
  video folder by `--restricted`: no Bash, no web, no MCP servers, no writes. This code writes
  `brief.json` and `brief.md` itself.
- The answer is validated against a schema, and every timestamp must be a segment start in
  `transcript.json`. A failing answer is retried once with the errors, then saved as
  `brief.rejected.json` and no brief is written.
- Anything that goes against a standing rule in `context.md` is flagged, not recommended.
  Commands from a video are listed as untested and are never run; a first step that reuses one
  is rejected.
- `brief.json` records the model, the generation time, the prompt version, the cost and a hash
  of the `context.md` it used, so briefs written with an older context can be spotted.

`ytd serve` opens the viewer at http://127.0.0.1:4410: This week (pick up where you left off,
what needs your attention, what's waiting quietly), the library with filters and search across
every transcript, a page per video, and the seven areas. It binds 127.0.0.1 only, on ytd's
reserved range 4410–4419, and a busy port is an error. It reads `research/` and its one write is
`picks.json` (Pick, Later or Skip per recommendation; in `.gitignore`). The viewer's page is not
part of this repository yet: without a built `web/dist`, `ytd serve` still serves the JSON API and
says so on start.

Tests: `python3 -m unittest discover -s tests`. They use only the standard library, an invented
digest under `tests/fixtures`, and no network.

## Audio-to-text fallback

When YouTube has no usable captions, install the optional local Whisper engine:

```bash
# The project .venv already contains faster-whisper. For a fresh uv environment:
uv sync --extra transcription
```

Then download audio and ask the digest to transcribe it locally:

```bash
uv run --extra transcription python main.py digest "https://www.youtube.com/watch?v=VIDEO_ID" \
  --out research/VIDEO_ID --audio mp3 --transcribe --stt-model base
```

The first run may download the Whisper model. The generated segments are saved to
`transcript.json` and `subtitles.vtt`, with the source recorded as
`local_whisper:faster-whisper:<model>`. If you already have an audio file, use
`--transcribe-file path/to/audio.mp3` instead of `--audio`.

## Troubleshooting

If YouTube starts returning `403` errors or bot-check prompts:

- update `yt-dlp`
- try `--cookies-from-browser` while logged into YouTube
- try a different network/IP
- use `--clear-cache` if stale yt-dlp cache data seems likely

If the script reports that a video is DRM-protected, YouTube has not exposed a downloadable
audio or video stream. yt-dlp and ffmpeg cannot decrypt or bypass DRM; use an authorized
non-DRM source or the provider's official download/export option.
