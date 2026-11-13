"""`ytd serve`: the local library viewer.

A stdlib HTTP server bound to 127.0.0.1 only, on a fixed port (no drifting to the
next one). It serves the built page from web/dist, a read-only JSON API over
research/, and exactly one write: picks.json (Pick, Later or Skip per recommendation).
Media files are never served.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import mimetypes
import os
import re
import socket
import sys
import tempfile
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import research

HOST = '127.0.0.1'
DEFAULT_PORT = 4410          # ytd uses 4410-4419
DEV_PORT = 4411              # the Vite dev server, which proxies /api here
WEB_DIST = research.REPO_ROOT / 'web' / 'dist'
PICKS_PATH = research.REPO_ROOT / 'picks.json'
CHOICES = ('pick', 'later', 'skip')
MAX_BODY = 4096
SEARCH_LIMIT = 200
_picks_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


class Library:
    """The API's data: reads research/, writes picks.json. No other writes."""

    def __init__(self, root: Path = research.RESEARCH_ROOT, picks_path: Path = PICKS_PATH,
                 context_path: Path = research.CONTEXT_PATH):
        self.root = root
        self.picks_path = picks_path
        self.context_path = context_path
        self._lines_cache: dict[str, tuple[float, list]] = {}

    # ----- reads
    def folders(self) -> dict[str, Path]:
        return {f.name: f for f in research.video_dirs(self.root)}

    def picks(self) -> dict:
        data = research.read_json(self.picks_path, None)
        if not isinstance(data, dict) or not isinstance(data.get('picks'), dict):
            return {'schema_version': '1.0', 'updated_at': None, 'picks': {}}
        return data

    def context_info(self) -> dict:
        try:
            raw = self.context_path.read_bytes()
        except OSError:
            return {'present': False}
        return {'present': True, 'sha256': hashlib.sha256(raw).hexdigest()}

    def library(self) -> dict:
        videos, recommendations = [], []
        for vid, folder in self.folders().items():
            summary = research.video_summary(folder, self.root)
            summary['words'] = self.word_count(folder)
            videos.append(summary)
            brief = research.read_json(folder / 'brief.json')
            if isinstance(brief, dict):
                for rec in brief.get('recommendations', []):
                    recommendations.append(self._rec_row(vid, summary, brief, rec))
        videos.sort(key=lambda v: (v['upload_date'] or '', v['id']), reverse=True)
        context = self.context_info()
        briefs = [v for v in videos if v['has_brief']]
        for v in briefs:
            v['brief_context_current'] = (v.get('context_sha256') == context.get('sha256')) if context.get('present') else None
        return {
            'generated_at': now_iso(),
            'videos': videos,
            'recommendations': recommendations,
            'picks': self.picks()['picks'],
            'context': context,
            'counts': {
                'videos': len(videos),
                'briefs': len(briefs),
                'waiting': len(videos) - len(briefs),
                'no_transcript': sum(1 for v in videos if v['transcript_status'] != 'ok'),
                'rejected': sum(1 for v in videos if v['brief_failed']),
                'recommendations': len(recommendations),
                'briefs_on_older_context': sum(1 for v in briefs if v.get('brief_context_current') is False),
            },
            'models': sorted({v['model'] for v in briefs if v['model']}),
            'source': 'research/',
        }

    @staticmethod
    def _rec_row(vid: str, summary: dict, brief: dict, rec: dict) -> dict:
        return {
            'key': f'{vid}:{rec.get("id")}',
            'video_id': vid,
            'video_title': summary['title'],
            'channel': summary['channel'],
            'verdict': brief.get('verdict'),
            **{k: rec.get(k) for k in ('id', 'title', 'areas', 'why', 'project', 'first_step', 'size',
                                       'needs_yes', 'conflicts', 'evidence', 'flagged', 'check_before_use')},
        }

    def lines(self, folder: Path) -> list[dict]:
        path = folder / 'transcript.json'
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return []
        cached = self._lines_cache.get(folder.name)
        if cached and cached[0] == mtime:
            return cached[1]
        transcript = research.load_transcript(folder)
        rows = [{'s': int(start), 'text': text} for start, text in research.clean_lines(transcript['segments'])]
        self._lines_cache[folder.name] = (mtime, rows)
        return rows

    def word_count(self, folder: Path) -> int:
        """Words in the cleaned transcript (analysis.json counts rolling caption repeats twice)."""
        return sum(len(line['text'].split()) for line in self.lines(folder))

    def video(self, vid: str) -> dict | None:
        folder = self.folders().get(vid)
        if not folder:
            return None
        analysis = research.read_json(folder / 'analysis.json', {}) or {}
        meta = analysis.get('metadata') or {}
        brief = research.read_json(folder / 'brief.json')
        rejected = research.read_json(folder / 'brief.rejected.json')
        picks = {k: v for k, v in self.picks()['picks'].items() if k.startswith(f'{vid}:')}
        manifest = research.read_json(folder / 'manifest.json', {}) or {}
        context = self.context_info()
        summary = research.video_summary(folder, self.root)
        summary['words'] = self.word_count(folder)
        return {
            'summary': summary,
            'description': str(meta.get('description') or '')[:5000],
            'chapters': [{'s': int(c.get('start_time') or 0), 'title': c.get('title', '')}
                         for c in analysis.get('chapters') or []],
            'links': [u for u in analysis.get('resources') or [] if isinstance(u, str) and u.startswith(('https://', 'http://'))],
            'brief': brief if isinstance(brief, dict) else None,
            'brief_rejected': {'errors': rejected.get('errors', [])[:10], 'at': rejected.get('at')}
                              if isinstance(rejected, dict) and not isinstance(brief, dict) else None,
            'brief_context_current': (brief.get('generator', {}).get('context_sha256') == context.get('sha256'))
                                     if isinstance(brief, dict) and context.get('present') else None,
            'deep_pass_pending': [n for n in manifest.get('downstream_pending', [])
                                  if not (folder / n).exists()],
            'lines': self.lines(folder),
            'picks': picks,
        }

    def search(self, query: str, limit: int = SEARCH_LIMIT) -> dict:
        q = re.sub(r'\s+', ' ', query or '').strip()
        if len(q) < 2:
            return {'query': q, 'results': [], 'truncated': False}
        needle = q.lower()
        results, truncated = [], False
        for vid, folder in self.folders().items():
            summary = None
            rows = self.lines(folder)
            for i, line in enumerate(rows):
                if needle in line['text'].lower():
                    if summary is None:
                        summary = research.video_summary(folder, self.root)
                    context = ' '.join(r['text'] for r in rows[max(0, i - 1):i + 2])  # a line either side
                    results.append({'video_id': vid, 'title': summary['title'], 's': line['s'], 'text': context})
                    if len(results) >= limit:
                        truncated = True
                        break
            if truncated:
                break
        return {'query': q, 'results': results, 'truncated': truncated}

    # ----- the one write
    def set_pick(self, body: dict) -> dict:
        if not isinstance(body, dict):
            raise ValueError('expected a JSON object')
        vid, rec_id, choice = body.get('video_id'), body.get('rec_id'), body.get('choice')
        if choice is not None and choice not in CHOICES:
            raise ValueError(f'choice must be one of {CHOICES} or null')
        folder = self.folders().get(vid) if isinstance(vid, str) else None
        if not folder:
            raise ValueError('unknown video')
        brief = research.read_json(folder / 'brief.json')
        recs = {r.get('id'): r for r in (brief or {}).get('recommendations', [])} if isinstance(brief, dict) else {}
        rec = recs.get(rec_id)
        if not rec:
            raise ValueError('unknown recommendation')
        key = f'{vid}:{rec_id}'
        with _picks_lock:
            data = self.picks()
            if choice is None:
                data['picks'].pop(key, None)
            else:
                data['picks'][key] = {
                    'video_id': vid, 'rec_id': rec_id, 'choice': choice, 'at': now_iso(),
                    'title': rec.get('title'), 'areas': rec.get('areas'), 'project': rec.get('project'),
                    'flagged': bool(rec.get('flagged')), 'video_title': brief.get('title'),
                    'brief_generated_at': brief.get('generated_at'),
                }
            data['updated_at'] = now_iso()
            data['schema_version'] = '1.0'
            self._write_atomic(data)
        return data['picks'].get(key) or {'video_id': vid, 'rec_id': rec_id, 'choice': None}

    def _write_atomic(self, data: dict) -> None:
        self.picks_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='.picks.', suffix='.json', dir=self.picks_path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
                fh.write('\n')
            os.replace(tmp, self.picks_path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


SECURITY_HEADERS = {
    'Content-Security-Policy': "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                               "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer',
    'X-Frame-Options': 'DENY',
    'Cross-Origin-Resource-Policy': 'same-origin',
}


def make_handler(library: Library, dist: Path, port: int):
    allowed_hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
    allowed_origins = {f'http://{h}' for h in allowed_hosts} | {f'http://127.0.0.1:{DEV_PORT}', f'http://localhost:{DEV_PORT}'}
    dist = dist.resolve()

    class Handler(BaseHTTPRequestHandler):
        server_version = 'ytd-serve'
        sys_version = ''

        def log_message(self, fmt, *args):  # one short line per request, on stderr
            sys.stderr.write(f'{self.command} {self.path.split("?")[0]} {args[1] if len(args) > 1 else ""}\n')

        def _host_ok(self) -> bool:
            # Refuse other Host names (DNS rebinding) unless the request comes through the dev proxy.
            host = (self.headers.get('Host') or '').lower()
            return host in allowed_hosts or host in {f'127.0.0.1:{DEV_PORT}', f'localhost:{DEV_PORT}'}

        def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(status)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            for k, v in {**SECURITY_HEADERS, **(extra or {})}.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)

        def _json(self, status: int, data):
            self._send(status, json.dumps(data, ensure_ascii=False).encode('utf-8'), 'application/json; charset=utf-8')

        def _error(self, status: int, message: str):
            self._json(status, {'error': message})

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not self._host_ok():
                return self._error(HTTPStatus.MISDIRECTED_REQUEST, 'unknown host')
            url = urlparse(self.path)
            path = url.path
            try:
                if path == '/api/library':
                    return self._json(200, library.library())
                if path == '/api/picks':
                    return self._json(200, library.picks())
                if path == '/api/search':
                    return self._json(200, library.search(parse_qs(url.query).get('q', [''])[0]))
                m = re.fullmatch(r'/api/video/([\w-]{11})', path)
                if m:
                    data = library.video(m.group(1))
                    return self._json(200, data) if data else self._error(404, 'no such video')
                if path == '/api/health':
                    return self._json(200, {'ok': True})
                if path.startswith('/api/'):
                    return self._error(404, 'no such endpoint')
            except Exception as exc:  # keep the server up; report without internals
                sys.stderr.write(f'error: {exc!r}\n')
                return self._error(500, 'the library could not be read')
            return self._static(path)

        def _static(self, path: str):
            rel = path.lstrip('/') or 'index.html'
            target = (dist / rel).resolve()
            if dist not in target.parents and target != dist / 'index.html':
                return self._error(404, 'not found')
            if not target.is_file():
                if '.' in Path(rel).name:
                    return self._error(404, 'not found')
                target = dist / 'index.html'  # hash routes all land on the page
            if not target.is_file():
                body = (b'<!doctype html><meta charset="utf-8"><title>ytd library</title>'
                        b'<p>The page is not built yet. Run <code>npm run build</code> in <code>web/</code>.</p>')
                return self._send(503, body, 'text/html; charset=utf-8')
            ctype = mimetypes.guess_type(target.name)[0] or 'application/octet-stream'
            if ctype.startswith('text/') or ctype in ('application/javascript', 'application/json'):
                ctype += '; charset=utf-8'
            self._send(200, target.read_bytes(), ctype)

        def do_POST(self):
            if not self._host_ok():
                return self._error(HTTPStatus.MISDIRECTED_REQUEST, 'unknown host')
            origin = self.headers.get('Origin')
            if origin is not None and origin not in allowed_origins:
                return self._error(403, 'cross-origin writes are refused')
            if self.headers.get('Sec-Fetch-Site') not in (None, 'same-origin', 'none'):
                return self._error(403, 'cross-site writes are refused')
            if urlparse(self.path).path != '/api/picks':
                return self._error(404, 'no such endpoint')
            if not (self.headers.get('Content-Type') or '').startswith('application/json'):
                return self._error(415, 'send JSON')
            try:
                length = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                length = -1
            if length <= 0 or length > MAX_BODY:
                return self._error(413, 'body too large or empty')
            try:
                body = json.loads(self.rfile.read(length).decode('utf-8'))
                return self._json(200, {'pick': library.set_pick(body)})
            except (ValueError, UnicodeDecodeError) as exc:
                return self._error(400, str(exc))

        def do_PUT(self):
            self._error(405, 'read-only')

        do_DELETE = do_PATCH = do_PUT

    return Handler


class StrictServer(ThreadingHTTPServer):
    daemon_threads = True


def port_in_use(port: int) -> bool:
    """True when something already listens on this port on loopback (IPv4 or IPv6), on any bind address."""
    for family, addr in ((socket.AF_INET, HOST), (socket.AF_INET6, '::1')):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.5)
                if probe.connect_ex((addr, port)) == 0:
                    return True
        except OSError:
            continue
    return False


def make_server(port: int = DEFAULT_PORT, library: Library | None = None, dist: Path = WEB_DIST) -> ThreadingHTTPServer:
    # SO_REUSEADDR (the default) lets a restart bind past TIME_WAIT; the probe keeps a held port an error.
    if port and port_in_use(port):
        raise OSError(errno.EADDRINUSE, 'Address already in use')
    library = library or Library()
    server = StrictServer((HOST, port), None)
    server.RequestHandlerClass = make_handler(library, dist, server.server_address[1])
    return server


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog='ytd serve',
                                description=f'Open the local library viewer on {HOST} (never wider). '
                                            'Reads research/; writes only picks.json.')
    p.add_argument('--port', type=int, default=DEFAULT_PORT,
                   help=f'Port in ytd\'s reserved range 4410-4419 (default {DEFAULT_PORT}). A busy port is an error.')
    p.add_argument('--research', type=Path, default=research.RESEARCH_ROOT, help=argparse.SUPPRESS)  # tests
    p.add_argument('--picks', type=Path, default=PICKS_PATH, help=argparse.SUPPRESS)  # tests
    args = p.parse_args(argv)
    if not 4410 <= args.port <= 4419:
        p.error('use a port in ytd\'s reserved range 4410-4419')
    try:
        server = make_server(args.port, Library(args.research, args.picks))
    except OSError as exc:
        print(f'ytd serve: {HOST}:{args.port} is not available ({exc.strerror}). '
              'Not trying another port; stop whatever holds it, or pick one in 4410-4419.', file=sys.stderr)
        return 1
    if not (WEB_DIST / 'index.html').is_file():
        print('ytd serve: web/dist is not built; the API works, the page shows how to build it.', file=sys.stderr)
    print(f'ytd library: http://{HOST}:{args.port}/  (Ctrl-C stops it)', file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
