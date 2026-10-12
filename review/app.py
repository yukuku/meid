#!/usr/bin/env python3
import argparse
import contextlib
import datetime as dt
import difflib
import fcntl
import hashlib
import html
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import tempfile
import threading
import webbrowser

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
REVISION = '20261012'


def digest(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def blocks(text):
    matches = list(re.finditer(r'(?m)^mmdd (\d{4})[^\S\n]*\n', text))
    return [(m.group(1), m.start(), matches[i + 1].start() if i + 1 < len(matches) else len(text)) for i, m in enumerate(matches)]


def segments(block):
    marks = list(re.finditer(r'(?m)^\*([anpf])[^\S\n]*\n', block))
    result = []
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(block)
        part = block[mark.end():end]
        part = re.split(r'(?m)^===\s*$', part)[0]
        start = mark.end() + len(part) - len(part.lstrip())
        stop = mark.end() + len(part.rstrip())
        result.append({'kind': mark.group(1), 'start': start, 'end': stop, 'text': block[start:stop]})
    return result


def body(block):
    text = re.split(r'(?m)^\*a[^\S\n]*\n', block, maxsplit=1)[1]
    return '\n'.join(line for line in text.splitlines() if not line.startswith(('*', '==='))).strip()


class StyledText(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.chars, self.italics, self.depth = [], [], 0
        self.feed(source)
        self.text = ''.join(self.chars)

    def handle_starttag(self, tag, attrs):
        if tag in ('i', 'em'):
            self.depth += 1

    def handle_endtag(self, tag):
        if tag in ('i', 'em'):
            self.depth = max(0, self.depth - 1)

    def handle_data(self, text):
        self.chars.extend(text)
        self.italics.extend([self.depth > 0] * len(text))

    def render(self, start, end, ranges):
        output, previous = [], (False, False)
        for pos in range(start, end):
            state = (any(a <= pos < z for a, z in ranges), self.italics[pos])
            if state != previous:
                if previous[1]: output.append('</i>')
                if previous[0]: output.append('</strong>')
                if state[0]: output.append('<strong>')
                if state[1]: output.append('<i>')
                previous = state
            output.append(html.escape(self.chars[pos]))
        if previous[1]: output.append('</i>')
        if previous[0]: output.append('</strong>')
        return ''.join(output)


def occurrences(text, phrases):
    ranges = []
    for phrase in phrases:
        ranges.extend((m.start(), m.end()) for m in re.finditer(re.escape(phrase), text))
    return sorted(set(ranges))


def remap(old, new, ranges):
    if old == new: return ranges
    changes = difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes()
    result = []
    for a, z in ranges:
        parts = []
        for kind, x, y, u, v in changes:
            if kind == 'equal' and x < z and y > a:
                parts.append((u + max(a, x) - x, u + min(z, y) - x))
            elif kind != 'equal' and ((x < z and y > a) or (x == y and a <= x < z)):
                parts.append((u, v))
        if parts: result.append((min(x for x, _ in parts), max(y for _, y in parts)))
    return result


def excerpt(styled, ranges):
    if not ranges:
        return '<p class="small">This passage has been removed in your draft. Open the full entry below.</p>'
    windows = []
    for a, z in ranges:
        lo, hi = max(0, a - 150), min(len(styled.text), z + 150)
        if lo: lo = styled.text.rfind(' ', 0, lo) + 1
        if hi < len(styled.text):
            boundary = styled.text.find(' ', hi)
            if boundary >= 0: hi = boundary
        if windows and lo <= windows[-1][1]: windows[-1] = (windows[-1][0], max(hi, windows[-1][1]))
        else: windows.append((lo, hi))
    return ''.join('<p class="excerpt">' + ('… ' if a else '') + styled.render(a, z, ranges) + (' …' if z < len(styled.text) else '') + '</p>' for a, z in windows)


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists(): os.chmod(temp, path.stat().st_mode & 0o777)
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(temp): os.unlink(temp)


class Conflict(Exception):
    pass


class Review:
    def __init__(self, source, state):
        self.source, self.state = source.resolve(), state.resolve()
        self.state.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.records = json.loads((DATA / 'morning-revision-20261012.json').read_text())['findings']
        self.variants = json.loads((DATA / 'correction-variants.json').read_text())
        self.operations = json.loads((DATA / 'morning-edits-20261012.json').read_text())
        self.by_id = {f['id']: f for f in self.records}
        self.highlights = json.loads((DATA / 'english-highlights.json').read_text())
        self.before = self.index((DATA / 'meid-am.before-20261012.txt').read_text())
        self.applied = self.index((DATA / 'applied-original.txt').read_text())
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS reviews (id TEXT PRIMARY KEY, checked INTEGER NOT NULL, entry_hash TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS applications (id TEXT PRIMARY KEY, entry_hash TEXT NOT NULL)')
        self.token = secrets.token_urlsafe(32)

    @contextlib.contextmanager
    def db(self):
        connection = sqlite3.connect(self.state / 'progress.sqlite3')
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @contextlib.contextmanager
    def locked(self):
        with self.lock, (self.state / 'write.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try: yield
            finally: fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def index(text):
        result = {}
        for date, a, z in blocks(text):
            value = text[a:z]
            if date in result and result[date] != value:
                raise Conflict(f'Duplicate {date} entries differ. Resolve them in the source before editing this report.')
            result[date] = value
        return result

    def read(self):
        return self.source.read_bytes().decode('utf-8')

    def progress(self, current):
        with self.db() as db:
            return {ident: bool(checked) and ident in self.by_id and digest(current[self.by_id[ident]['date']]) == version for ident, checked, version in db.execute('SELECT id, checked, entry_hash FROM reviews')}

    def entry(self, date):
        current = self.index(self.read())
        if date not in current: raise ValueError('Unknown date')
        block = current[date]
        return {'date': date, 'version': digest(block), 'fields': [{'kind': s['kind'], 'text': s['text']} for s in segments(block)]}

    def proposal(self, ident, current):
        record = self.by_id[ident]
        block = current[record['date']]
        warnings = []
        for op in self.operations:
            if op['finding'] != int(ident[1:]): continue
            plain = ' '.join(StyledText(block).text.split())
            new = ' '.join(StyledText(op['new']).text.split())
            alternatives = self.variants.get(ident, {}).get(StyledText(op['new']).text, [])
            if new in plain or any(' '.join(option.split()) in plain for option in alternatives): continue
            if block.count(op['old']) == 1:
                block = block.replace(op['old'], op['new'], 1)
            else:
                warnings.append({'current_reference': op['old'], 'suggestion': op['new']})
        return block, warnings

    def edit(self, ident):
        record = self.by_id[ident]
        current_blocks = self.index(self.read())
        proposed, warnings = self.proposal(ident, current_blocks)
        entry = {'date': record['date'], 'version': digest(current_blocks[record['date']]),
                 'fields': [{'kind': s['kind'], 'text': s['text']} for s in segments(proposed)], 'warnings': warnings}
        original = StyledText(body(self.applied[record['date']])).text
        plain_fields = [StyledText(f['text']).text for f in entry['fields']]
        current = '\n'.join(plain_fields)
        ranges = remap(original, current, occurrences(original, [c['snippet'] for c in record['suggested_indonesian']]))
        offset = 0
        for field, plain in zip(entry['fields'], plain_fields):
            positions = []
            for match in re.finditer(r'<[^>]*>|&(?:\#x[0-9a-fA-F]+|\#\d+|[a-zA-Z]+);|.', field['text'], re.S):
                if match.group().startswith('<'): continue
                positions.extend([(match.start(), match.end())] * len(html.unescape(match.group())))
            field['highlights'] = []
            for start, end in ranges:
                a, z = max(0, start - offset), min(len(plain), end - offset)
                if a < z:
                    raw_a, raw_z = positions[a][0], positions[z - 1][1]
                    field['highlights'].append([len(field['text'][:raw_a].encode('utf-16-le')) // 2, len(field['text'][:raw_z].encode('utf-16-le')) // 2])
            offset += len(plain) + 1
        entry['finding'] = ident
        return entry

    def save(self, date, expected, fields, ident=None):
        with self.locked():
            original = self.source.read_bytes()
            text = original.decode('utf-8')
            current = self.index(text)
            if date not in current: raise ValueError('Unknown date')
            block = current[date]
            if digest(block) != expected: raise Conflict('This entry changed after you opened it. Your edits are still in the editor. Copy them, then reload the entry and merge the changes.')
            template = block
            if ident is not None:
                if ident not in self.by_id or self.by_id[ident]['date'] != date: raise ValueError('Invalid finding')
                template, _ = self.proposal(ident, current)
            spans = segments(template)
            if not isinstance(fields, list) or len(fields) != len(spans): raise ValueError('Paragraph structure does not match')
            new = template
            for span, field in reversed(list(zip(spans, fields))):
                if not isinstance(field, str) or not field.strip() or len(field) > 100000: raise ValueError('Each paragraph must contain text and be below 100,000 characters')
                if re.search(r'(?m)^(?:mmdd |===|\*)', field): raise ValueError('Entry and paragraph markers cannot be inserted in the editor')
                tags = re.findall(r'<[^>]*>', field)
                if any(tag not in ('<i>', '</i>') for tag in tags): raise ValueError('Only <i> and </i> formatting tags are supported')
                depth = 0
                for tag in tags:
                    depth += 1 if tag == '<i>' else -1
                    if depth < 0: raise ValueError('An italic closing tag has no matching opening tag')
                if depth: raise ValueError('Close each <i> tag with </i>')
                new = new[:span['start']] + field.strip() + new[span['end']:]
            if new != block:
                updated = text
                for d, a, z in reversed(blocks(text)):
                    if d == date: updated = updated[:a] + new + updated[z:]
                stamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
                backup = self.state / 'backups' / (stamp + '-' + digest(original)[:12] + '.txt')
                atomic_write(backup, original)
                if self.source.read_bytes() != original: raise Conflict('The source changed during save. No edit was written; retry after reloading.')
                atomic_write(self.source, updated.encode('utf-8'))
            with self.db() as db:
                db.execute('UPDATE applications SET entry_hash=? WHERE entry_hash=?', (digest(new), digest(block)))
                if ident is not None:
                    db.execute('INSERT INTO applications VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET entry_hash=excluded.entry_hash', (ident, digest(new)))
            return self.date_update(date)

    def column(self, record, kind, current):
        if kind == 'english':
            raw = (DATA / 'english' / (record['date'] + '.html')).read_text().split('<h2>Evening</h2>')[0]
            raw = re.sub(r'</p>|<br\s*/?>', '\n', raw)
            styled = StyledText(raw.strip())
            ranges = occurrences(styled.text, self.highlights[record['id']])
        else:
            raw = body(current[record['date']] if kind == 'current_indonesian' else self.proposal(record['id'], current)[0])
            styled = StyledText(raw)
            reference = StyledText(body(self.before[record['date']] if kind == 'current_indonesian' else self.applied[record['date']])).text
            ranges = occurrences(reference, [c['snippet'] for c in record[kind]])
            ranges = remap(reference, styled.text, ranges)
        full = styled.render(0, len(styled.text), [])
        notice = ''
        if kind == 'suggested_indonesian':
            for warning in self.proposal(record['id'], current)[1]:
                suggestion = StyledText(warning['suggestion'])
                notice += '<p class="notice">Your wording has changed; compare and edit manually before applying:</p><p>' + suggestion.render(0, len(suggestion.text), [(0, len(suggestion.text))]) + '</p>'
        return notice + excerpt(styled, ranges) + '<details><summary>Read full entry for context</summary><div class="full">' + full + '</div></details>'

    def resolved(self, record, current):
        def normalize(text):
            return ' '.join(StyledText(text).text.split())
        with self.db() as db:
            applied = db.execute('SELECT entry_hash FROM applications WHERE id=?', (record['id'],)).fetchone()
        if applied and applied[0] == digest(current[record['date']]): return True
        text = normalize(body(current[record['date']]))
        variants = self.variants.get(record['id'], {})
        snippets = [part['snippet'] for part in record['suggested_indonesian']]
        return bool(snippets) and all(any(normalize(option) in text for option in [snippet] + variants.get(snippet, [])) for snippet in snippets)

    def date_update(self, date):
        current = self.index(self.read())
        return {'date': date, 'version': digest(current[date]), 'columns': {f['id']: self.column(f, 'suggested_indonesian', current) for f in self.records if f['date'] == date}, 'current_columns': {f['id']: self.column(f, 'current_indonesian', current) for f in self.records if f['date'] == date}, 'checks': self.progress(current), 'resolved': [f['id'] for f in self.records if f['date'] == date and self.resolved(f, current)]}

    def mark(self, ident, checked, expected):
        if ident not in self.by_id or type(checked) is not bool: raise ValueError('Invalid finding or review status')
        with self.locked():
            current = self.index(self.read())
            version = digest(current[self.by_id[ident]['date']])
            if expected != version: raise Conflict('The entry changed. Reload the page before marking it reviewed.')
            with self.db() as db:
                db.execute('INSERT INTO reviews VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET checked=excluded.checked, entry_hash=excluded.entry_hash', (ident, checked, version))
            return {'checks': self.progress(current)}

    def import_progress(self, payload):
        checks = payload.get('checks')
        if payload.get('revision') != REVISION or not isinstance(checks, dict) or any(k not in self.by_id or type(v) is not bool for k, v in checks.items()): raise ValueError('Incompatible review backup')
        with self.locked():
            current = self.index(self.read())
            with self.db() as db:
                for ident, checked in checks.items():
                    if checked:
                        db.execute('INSERT INTO reviews VALUES (?, 1, ?) ON CONFLICT(id) DO UPDATE SET checked=1, entry_hash=excluded.entry_hash', (ident, digest(current[self.by_id[ident]['date']])))
            return {'checks': self.progress(current)}

    def page(self):
        current = self.index(self.read())
        template = (DATA / 'report-template.html').read_text()
        style = re.search(r'<style>(.*?)</style>', template, re.S).group(1)
        supplemental = ''.join(re.search(r'<section id="' + section + r'">.*?</section>', template, re.S).group(0) for section in ['glossary', 'style'])
        rows = []
        pending = [f for f in self.records if not self.resolved(f, current)]
        for f in pending:
            ident, date = f['id'], f['date']
            label = dt.datetime.strptime('2024' + date, '%Y%m%d').strftime('%d %B')
            rows.append(f'<tbody class="finding" id="{ident}" data-id="{ident}" data-date="{date}" data-month="{date[:2]}" data-version="{digest(current[date])}"><tr class="entryhead"><td colspan="3"><label><input type="checkbox" data-check="{ident}"> Reviewed</label> <strong>{ident} · {label}</strong><p>{html.escape(f["issue"])}</p><span class="small">{html.escape(f["editorial_note"])}</span></td></tr><tr>')
            for kind in ['english', 'current_indonesian', 'suggested_indonesian']:
                draft = kind == 'suggested_indonesian'
                rows.append('<td' + (' class="proposed"' if draft else '') + '>' + (f'<button class="edit" data-edit="{date}">Edit &amp; apply suggestion</button>' if draft else '') + '<div class="column">' + self.column(f, kind, current) + '</div></td>')
            rows.append('</tr></tbody>')
        initial = json.dumps({'checks': self.progress(current), 'token': self.token}).replace('<', '\\u003c')
        return (ROOT / 'page.html').read_text().replace('@@COUNT@@', str(len(pending))).replace('@@STYLE@@', style).replace('@@ROWS@@', ''.join(rows)).replace('@@SUPPLEMENTAL@@', supplemental).replace('@@INITIAL@@', initial).replace('@@SOURCE@@', html.escape(str(self.source)))


def handler(review):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, data, kind='application/json'):
            raw = json.dumps(data, ensure_ascii=False).encode() if kind == 'application/json' else data.encode()
            self.send_response(status)
            self.send_header('Content-Type', kind + '; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(raw)

        def allowed_host(self):
            return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')

        def do_GET(self):
            if not self.allowed_host(): return self.respond(403, {'error': 'Invalid host'})
            try:
                if self.path in ('/', '/review', '/review.html'): self.respond(200, review.page(), 'text/html')
                elif self.path == '/app.js': self.respond(200, (ROOT / 'app.js').read_text(), 'text/javascript')
                elif re.fullmatch(r'/api/entry/\d{4}', self.path): self.respond(200, review.entry(self.path.rsplit('/', 1)[1]))
                elif re.fullmatch(r'/api/edit/R\d{3}', self.path): self.respond(200, review.edit(self.path.rsplit('/', 1)[1]))
                elif self.path == '/api/export': self.respond(200, {'revision': REVISION, 'checks': review.progress(review.index(review.read()))})
                else: self.respond(404, {'error': 'Not found'})
            except Conflict as exc: self.respond(409, {'error': str(exc)})
            except (ValueError, KeyError) as exc: self.respond(400, {'error': str(exc)})
            except Exception: self.respond(500, {'error': 'Could not read the review data. Check the server terminal.'}); raise

        def do_POST(self):
            if not self.allowed_host() or self.headers.get('Origin') not in (f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}') or not secrets.compare_digest(self.headers.get('X-Review-Token', ''), review.token):
                return self.respond(403, {'error': 'Reload this local page before saving'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length < 1000000: raise ValueError('Invalid request size')
                value = json.loads(self.rfile.read(length))
                if not isinstance(value, dict): raise ValueError('Invalid request')
                if self.path == '/api/save': result = review.save(value['date'], value['version'], value['fields'])
                elif self.path == '/api/apply': result = review.save(review.by_id[value['id']]['date'], value['version'], value['fields'], value['id'])
                elif self.path == '/api/mark': result = review.mark(value['id'], value['checked'], value['version'])
                elif self.path == '/api/import': result = review.import_progress(value)
                else: return self.respond(404, {'error': 'Not found'})
                self.respond(200, result)
            except Conflict as exc: self.respond(409, {'error': str(exc)})
            except (ValueError, KeyError, TypeError) as exc: self.respond(400, {'error': str(exc)})
            except Exception: self.respond(500, {'error': 'Save failed. Your editor text has been kept. Check the server terminal.'}); raise
    return Handler


def main():
    parser = argparse.ArgumentParser(description='Review and edit Renungan Pagi locally.')
    parser.add_argument('--port', type=int, default=5173)
    parser.add_argument('--source', type=Path, default=ROOT.parent / 'materials/in/meid-am.txt')
    parser.add_argument('--state-dir', type=Path, default=ROOT / '.local')
    parser.add_argument('--open', action='store_true')
    args = parser.parse_args()
    review = Review(args.source, args.state_dir)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler(review))
    print(f'Review editor: http://127.0.0.1:{server.server_port}/\nSource: {review.source}\nProgress and backups: {review.state}', flush=True)
    if args.open: webbrowser.open(f'http://127.0.0.1:{server.server_port}/')
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == '__main__':
    main()
