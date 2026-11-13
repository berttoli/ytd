"""Read-only access to the digested videos in research/.

Every function here only reads. The two writers in the library are `brief.py`
(brief.md, brief.json) and `serve.py` (picks.json).
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RESEARCH_ROOT = REPO_ROOT / 'research'
CONTEXT_PATH = REPO_ROOT / 'context.md'
VIDEO_ID = re.compile(r'[\w-]{11}')


def clock(seconds: float, hours: bool = False) -> str:
    """`mm:ss`, or `h:mm:ss` when the video runs an hour or more."""
    sec = int(float(seconds))
    h, rest = divmod(sec, 3600)
    m, s = divmod(rest, 60)
    return f'{h}:{m:02}:{s:02}' if (h or hours) else f'{m:02}:{s:02}'


def parse_clock(value: str) -> int:
    """`mm:ss` or `h:mm:ss` to whole seconds. Raises ValueError otherwise."""
    text = str(value).strip()
    if not re.fullmatch(r'\d{1,2}(:\d{2}){1,2}', text):
        raise ValueError(f'not a timestamp: {value!r}')
    parts = [int(p) for p in text.split(':')]
    if any(p >= 60 for p in parts[1:]):
        raise ValueError(f'not a timestamp: {value!r}')
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds


def video_dirs(root: Path = RESEARCH_ROOT) -> list[Path]:
    """Every folder under research/ (one level of grouping, e.g. shorts/) holding a digest."""
    found = []
    for analysis in sorted(root.glob('*/analysis.json')) + sorted(root.glob('*/*/analysis.json')):
        folder = analysis.parent
        if VIDEO_ID.fullmatch(folder.name):
            found.append(folder)
    return found


def find_video(video_id: str, root: Path = RESEARCH_ROOT) -> Path:
    if not VIDEO_ID.fullmatch(video_id or ''):
        raise ValueError(f'not a video id: {video_id!r}')
    for folder in video_dirs(root):
        if folder.name == video_id:
            return folder
    raise FileNotFoundError(f'no digest for {video_id} under {root}')


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def load_transcript(folder: Path) -> dict:
    data = read_json(folder / 'transcript.json', {}) or {}
    data.setdefault('segments', [])
    return data


def segment_seconds(transcript: dict) -> set[int]:
    """The whole seconds at which a transcript segment starts: the only citable timestamps."""
    return {int(float(s.get('start') or 0)) for s in transcript.get('segments', [])}


def clean_lines(segments: list[dict]) -> list[tuple[float, str]]:
    """Caption lines without the rolling repeats of YouTube auto-captions.

    Older digests store auto-captions as pairs: "A", "A B", "B", "B C", ...
    A segment that starts with the previous line keeps only its new part, and a
    segment equal to the previous line is dropped. Clean transcripts pass through.
    """
    lines: list[tuple[float, str]] = []
    last = ''
    for segment in segments:
        text = re.sub(r'\s+', ' ', html.unescape(str(segment.get('text') or ''))).strip()
        if not text or text == last:
            continue
        start = float(segment.get('start') or 0)
        if last and text.startswith(last + ' '):
            text = text[len(last):].strip()
            if not text or (lines and text == lines[-1][1]):
                continue
        lines.append((start, text))
        last = text
    return lines


def timed_text(segments: list[dict], chapters: list[dict] | None = None,
               paragraph_seconds: int = 20, long_video: bool = False) -> str:
    """The transcript as `[mm:ss] text` paragraphs; every marker is a real segment start."""
    chapters = sorted(chapters or [], key=lambda c: c.get('start_time') or 0)
    out: list[str] = []
    para: list[str] = []
    para_start: float | None = None
    current = -1

    def flush():
        nonlocal para, para_start
        if para:
            out.append(f'[{clock(para_start, long_video)}] ' + ' '.join(para))
        para, para_start = [], None

    for start, text in clean_lines(segments):
        chapter = max((i for i, c in enumerate(chapters) if (c.get('start_time') or 0) <= start), default=-1)
        if chapter != current:
            flush()
            current = chapter
            if chapter >= 0:
                out.append(f'\n## Chapter: {chapters[chapter].get("title", "")}')
        if para_start is not None and start - para_start >= paragraph_seconds:
            flush()
        if para_start is None:
            para_start = start
        para.append(text)
    flush()
    return '\n'.join(out).strip() + '\n'


def video_summary(folder: Path, root: Path = RESEARCH_ROOT) -> dict:
    """What the viewer lists for one video: metadata, transcript facts and brief status."""
    analysis = read_json(folder / 'analysis.json', {}) or {}
    meta = analysis.get('metadata') or {}
    stats = analysis.get('statistics') or {}
    retrieval = analysis.get('retrieval') or {}
    brief = read_json(folder / 'brief.json')
    rel = folder.relative_to(root).as_posix()
    upload = str(meta.get('upload_date') or '')
    return {
        'id': folder.name,
        'path': rel,
        'group': rel.split('/')[0] if '/' in rel else '',
        'title': meta.get('title') or folder.name,
        'channel': meta.get('channel') or meta.get('uploader') or '',
        'url': analysis.get('url') or f'https://www.youtube.com/watch?v={folder.name}',
        'upload_date': f'{upload[:4]}-{upload[4:6]}-{upload[6:8]}' if len(upload) == 8 else '',
        'duration': meta.get('duration') or 0,
        'words': stats.get('words') or 0,
        'transcript_status': retrieval.get('status') or ('ok' if (folder / 'transcript.json').exists() else 'missing'),
        'transcript_source': retrieval.get('transcript_source') or '',
        'chapters': len(analysis.get('chapters') or []),
        'has_brief': isinstance(brief, dict),
        'brief_failed': (folder / 'brief.rejected.json').exists() and not isinstance(brief, dict),
        'verdict': brief.get('verdict') if isinstance(brief, dict) else None,
        'areas': sorted({a for r in (brief or {}).get('recommendations', []) for a in r.get('areas', [])})
                 if isinstance(brief, dict) else [],
        'generated_at': brief.get('generated_at') if isinstance(brief, dict) else None,
        'model': (brief.get('generator') or {}).get('model') if isinstance(brief, dict) else None,
        'context_sha256': (brief.get('generator') or {}).get('context_sha256') if isinstance(brief, dict) else None,
        'recommendations': len(brief.get('recommendations', [])) if isinstance(brief, dict) else 0,
    }
