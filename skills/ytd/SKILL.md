---
name: ytd
description: Get a YouTube video's transcript, audio, video, or research digest with the local `ytd` command. Use when the user gives a YouTube URL or video ID and wants it transcribed, summarized, reviewed, or downloaded (wav/mp3/mp4).
---

# ytd

`ytd` is on PATH and works from any directory. It takes a full YouTube URL or an 11-character video ID. Logs go to stderr; results go to stdout.

## Read a video (most common)

```bash
ytd transcript "<url>"
```

Prints clean text on stdout: title, channel, duration, chapter headings, and `[mm:ss]` paragraphs. Writes no files. A 25-minute video is about 22 KB. Add `-o FILE` to save it, or `--json` for raw timed segments.

Exit 2 means the video has no captions. Then transcribe locally with Whisper:

```bash
ytd digest "<url>" --out <dir> --audio --transcribe --json
```

## Keep the artifacts

```bash
ytd digest "<url>" --json
```

Without `--out`, files go to `research/<video-id>` inside the ytd repo, so every video keeps its own folder. `--json` prints one object on stdout: `ok`, `exit_code`, `title`, `out_dir`, `transcript.status`, `files` (absolute paths), `media`, `warnings`, `errors`, and `read_first`. Read the file at `read_first` (`transcript.txt`); the other files (`analysis.json`, `subtitles.vtt`, `summary.md`) are supporting evidence. `summary.md` is a keyword extract, not a real summary: write the summary yourself from `transcript.txt`.

Pass `--out <dir>` to write somewhere else. Add `--audio [mp3|wav]` or `--video` to keep media in the same folder.

Exit codes: 0 ok, 2 no transcript (with `--require-transcript`), 3 a media download failed.

## Download media only

```bash
ytd "<url>" -f mp3 -o <dir>     # wav (default), mp3, or mp4
ytd chunk <file.mp4> -s 180     # split a local video into pieces
```

## Catalog a show episode

```bash
ytd show "<url>" --dry-run --json   # the plan: parsed show, season, episode, target path
ytd show "<url>" --json             # download, file under Shows/<Show>/Season NN, add to the catalog
ytd show list                       # what is cataloged
```

Show, season and episode are parsed from the title; pass `--show`, `--season`, `--episode` or `--title` when the dry run gets them wrong. `--upload` copies the files to the rclone remote in `$YTD_SHOWS_REMOTE` (or `--remote`). Run the dry run first and show the user the parsed names before a real fetch: episodes are large and the upload writes to their bucket.

## When YouTube blocks the request

A 403 or a "confirm you're not a bot" message: retry with `--cookies-from-browser chrome`. This reads the user's browser cookies, so ask them before the first use. A 429 listed under `warnings` while `transcript.status` is `ok` needs no action.

## Notes

- Auto-captions misspell names. Check repo, product, and people names against the video description (`ytd transcript --json`, field `metadata.description`) before quoting them.
- Downloads can take minutes for long videos; run them in the background when that matters.
