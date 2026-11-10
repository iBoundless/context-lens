"""SQLite is confined to a private directory; transport headers never enter it."""

import json
import os
import sqlite3
import time
import threading
from functools import wraps
from pathlib import Path

from .analysis import ANALYSIS_VERSION, analyze, usage_from

CLIENTS = {'opencode', 'codex'}
DIRECTIONS = {'request', 'response', 'gap', 'notice'}


def locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return call


def validate_event(event):
    if not isinstance(event, dict):
        raise ValueError('event must be an object')
    if event.get('client') not in CLIENTS or event.get('direction') not in DIRECTIONS:
        raise ValueError('invalid client or direction')
    for key in ['event_id', 'session_id', 'request_id']:
        if not isinstance(event.get(key), str) or not 0 < len(event[key]) <= 256:
            raise ValueError('invalid ' + key)
    if event.get('transport', 'http') not in {'http', 'ws', 'semantic'}:
        raise ValueError('invalid transport')
    if 'kind' in event and event['kind'] not in {'primary', 'compaction', 'title', 'generate', 'unknown'}:
        raise ValueError('invalid kind')


class Store:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError('capture paths must not be symbolic links')
        os.chmod(path.parent, 0o700)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=DELETE;
            PRAGMA secure_delete=ON;
            CREATE TABLE IF NOT EXISTS sessions (
              client TEXT NOT NULL, session_id TEXT NOT NULL, recording INTEGER NOT NULL DEFAULT 0,
              seen REAL NOT NULL, PRIMARY KEY(client,session_id));
            CREATE TABLE IF NOT EXISTS events (
              seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
              client TEXT NOT NULL, session_id TEXT NOT NULL, request_id TEXT NOT NULL,
              direction TEXT NOT NULL, received REAL NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_session ON events(client,session_id,seq);
            CREATE INDEX IF NOT EXISTS events_request ON events(client,session_id,request_id,direction,seq);
            CREATE TABLE IF NOT EXISTS wave_index (
              seq INTEGER PRIMARY KEY, client TEXT NOT NULL, session_id TEXT NOT NULL,
              request_id TEXT NOT NULL, kind TEXT NOT NULL, input_tokens INTEGER,
              summary TEXT NOT NULL, analysis TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS wave_session ON wave_index(client,session_id,seq);
        ''')
        # Derived analysis is replaceable; raw captures and recording consent are not.
        with self.db:
            stale = self.db.execute('SELECT e.*,w.summary AS old_summary FROM events e '
                                    'LEFT JOIN wave_index w ON e.seq=w.seq '
                                    'WHERE e.direction IN ("request","gap") AND '
                                    '(w.seq IS NULL OR COALESCE(json_extract(w.summary,"$.analysis_version"),0) != ?) '
                                    'ORDER BY e.seq', (ANALYSIS_VERSION,))
            for row in stale:
                old = json.loads(row['old_summary']) if row['old_summary'] else {}
                self._index_request(dict(row), json.loads(row['data']))
                if old.get('usage') and old.get('capture') != 'gap':
                    current = self.db.execute('SELECT summary FROM wave_index WHERE seq=?', (row['seq'],)).fetchone()
                    usage = old['usage'] | (json.loads(current[0]).get('usage') or {})
                    self._update_usage(row['client'], row['session_id'], row['request_id'], usage)

    @locked
    def close(self):
        self.db.close()

    @locked
    def record(self, client, session_id, enabled):
        if client not in CLIENTS or not isinstance(session_id, str) or not 0 < len(session_id) <= 256:
            raise ValueError('invalid session')
        self.db.execute('INSERT INTO sessions VALUES (?,?,?,?) ON CONFLICT(client,session_id) '
                        'DO UPDATE SET recording=excluded.recording,seen=excluded.seen',
                        (client, session_id, int(enabled), time.time()))
        self.db.commit()

    @locked
    def is_recording(self, client, session_id):
        row = self.db.execute('SELECT recording FROM sessions WHERE client=? AND session_id=?',
                              (client, session_id)).fetchone()
        return bool(row and row[0])

    @locked
    def ingest(self, event):
        validate_event(event)
        client, session = event['client'], event['session_id']
        now = time.time()
        self.db.execute('INSERT INTO sessions VALUES (?,?,0,?) ON CONFLICT(client,session_id) '
                        'DO UPDATE SET seen=excluded.seen', (client, session, now))
        if not self.is_recording(client, session):
            self.db.commit()
            return False
        # Allowlist envelope fields. No Request, headers, URLs or arbitrary metadata.
        safe = {k: event[k] for k in ['event_id', 'client', 'session_id', 'request_id', 'direction',
                                     'transport', 'kind', 'model', 'agent', 'capture', 'payload'] if k in event}
        cursor = self.db.execute('INSERT OR IGNORE INTO events '
                                 '(event_id,client,session_id,request_id,direction,received,data) '
                                 'VALUES (?,?,?,?,?,?,?)',
                                 (safe['event_id'], client, session, safe['request_id'], safe['direction'],
                                  now, json.dumps(safe, ensure_ascii=False)))
        if cursor.rowcount:
            if safe['direction'] in {'request', 'gap'}:
                self._index_request({'seq': cursor.lastrowid, 'received': now}, safe)
            elif safe['direction'] == 'response':
                usage = usage_from(safe.get('payload'))
                if usage:
                    self._update_usage(client, session, safe['request_id'], usage)
        self.db.commit()
        return cursor.rowcount == 1

    def _index_request(self, row, data):
        analysis = analyze(data.get('payload'))
        if data['direction'] == 'gap':
            analysis['skills'] = []
            analysis['estimated_tokens'] = None
            analysis['context_completeness'] = 'capture-gap'
            analysis['warnings'].append('capture-gap')
            for block in analysis['blocks']:
                block['estimated_tokens'] = None
        summary = {'id': row['seq'], 'request_id': data['request_id'], 'received': row['received'],
                   'transport': data.get('transport', 'http'), 'kind': data.get('kind', 'unknown'),
                   'model': data.get('model', ''), 'agent': data.get('agent', ''),
                   'capture': 'gap' if data['direction'] == 'gap' else data.get('capture', 'complete'),
                   'usage': None, **{k: v for k, v in analysis.items() if k != 'blocks'}}
        totals = {}
        for block in analysis['blocks']:
            category, count = block['category'], block['estimated_tokens']
            previous = totals.get(category, 0)
            totals[category] = None if previous is None or count is None else previous + count
        summary['category_totals'] = totals
        summary['blocks'] = [{k: v for k, v in b.items() if k != 'content'} for b in analysis['blocks'][:500]]
        summary['blocks_truncated'] = len(analysis['blocks']) > 500
        self.db.execute('INSERT OR REPLACE INTO wave_index VALUES (?,?,?,?,?,?,?,?)',
                        (row['seq'], data['client'], data['session_id'], data['request_id'],
                         summary['kind'], None, json.dumps(summary, ensure_ascii=False),
                         json.dumps(analysis, ensure_ascii=False)))
        responses = self.db.execute('SELECT data FROM events WHERE client=? AND session_id=? AND request_id=? '
                                    'AND direction="response" ORDER BY seq',
                                    (data['client'], data['session_id'], data['request_id']))
        for reply in responses.fetchall():
            usage = usage_from(json.loads(reply[0]).get('payload'))
            if usage:
                self._update_usage(data['client'], data['session_id'], data['request_id'], usage)

    def _update_usage(self, client, session, request_id, usage):
        rows = self.db.execute('SELECT seq,summary FROM wave_index WHERE client=? AND session_id=? AND request_id=? '
                               'AND json_extract(summary,"$.capture") != "gap"',
                               (client, session, request_id)).fetchall()
        for row in rows:
            summary = json.loads(row['summary'])
            summary['usage'] = (summary.get('usage') or {}) | usage
            count = summary['usage'].get('input_tokens', summary['usage'].get('prompt_tokens'))
            self.db.execute('UPDATE wave_index SET summary=?,input_tokens=? WHERE seq=?',
                             (json.dumps(summary, ensure_ascii=False), count, row['seq']))

    @locked
    def sessions(self):
        return [dict(row) for row in self.db.execute('SELECT s.*, '
                '(SELECT COUNT(*) FROM events e WHERE e.client=s.client AND e.session_id=s.session_id '
                'AND direction="request") AS requests FROM sessions s ORDER BY seen DESC')]

    @locked
    def events(self, client, session):
        return [dict(row) | {'data': json.loads(row['data'])} for row in self.db.execute(
            'SELECT * FROM events WHERE client=? AND session_id=? ORDER BY seq', (client, session))]

    @locked
    def waves(self, client, session, *, before=None):
        rows = self.db.execute('SELECT summary FROM wave_index WHERE client=? AND session_id=? '
                                'AND seq<? ORDER BY seq DESC LIMIT 100',
                                (client, session, before if before is not None else 9223372036854775807)).fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]

    @locked
    def wave(self, seq):
        row = self.db.execute('SELECT summary,analysis FROM wave_index WHERE seq=?', (seq,)).fetchone()
        return (json.loads(row['summary']) | json.loads(row['analysis'])) if row else None

    @locked
    def overview(self, client, session):
        base = ('SELECT summary FROM wave_index WHERE client=? AND session_id=? '
                'AND kind IN ("primary","unknown") AND json_extract(summary,"$.capture") != "gap" ')
        first_row = self.db.execute(base + 'ORDER BY seq ASC LIMIT 1', (client, session)).fetchone()
        last_row = self.db.execute(base + 'ORDER BY seq DESC LIMIT 1', (client, session)).fetchone()
        first = json.loads(first_row[0]) if first_row else None
        latest = json.loads(last_row[0]) if last_row else None
        usage = self.db.execute('SELECT SUM(input_tokens),COUNT(input_tokens),COUNT(*) FROM wave_index '
                                'WHERE client=? AND session_id=?', (client, session)).fetchone()
        skill_totals, coverage = {}, {'request_count': 0, 'gap_count': 0,
                                     'incomplete_request_count': 0, 'server_state_unknown_count': 0}
        # Read compact metadata only, independent of wave/block preview limits.
        rows = self.db.execute('SELECT json_extract(summary,"$.skills"), '
                               'json_extract(summary,"$.capture"), json_extract(summary,"$.context_completeness") '
                               'FROM wave_index WHERE client=? AND session_id=?', (client, session))
        for skills, capture, completeness in rows:
            if capture == 'gap':
                coverage['gap_count'] += 1
                continue
            coverage['request_count'] += 1
            coverage['incomplete_request_count'] += int(capture != 'complete' or completeness == 'unknown-protocol')
            coverage['server_state_unknown_count'] += int(completeness == 'server-state-unknown')
            for skill in json.loads(skills or '[]'):
                key = (skill['name'], skill['kind'])
                entry = skill_totals.setdefault(key, {'name': key[0], 'kind': key[1], 'estimated_tokens': 0,
                    'request_count': 0, 'occurrences': 0, 'attributions': set()})
                count = skill['estimated_tokens']
                entry['estimated_tokens'] = (None if count is None or entry['estimated_tokens'] is None
                                             else entry['estimated_tokens'] + count)
                entry['request_count'] += 1
                entry['occurrences'] += skill['occurrences']
                entry['attributions'].update(skill['attributions'])
        return {'first': first, 'latest': latest, 'provider_input_sum': usage[0] or 0,
                'usage_reported_count': usage[1], 'total': usage[2], 'skill_coverage': coverage,
                'skill_totals': [entry | {'attributions': sorted(entry['attributions'])}
                                 for _, entry in sorted(skill_totals.items())]}

    @locked
    def detail(self, seq):
        row = self.db.execute('SELECT * FROM events WHERE seq=? AND direction IN ("request","gap")',
                              (seq,)).fetchone()
        if not row:
            return None
        responses = self.db.execute('SELECT data FROM events WHERE client=? AND session_id=? '
                                    'AND request_id=? AND direction="response" ORDER BY seq',
                                    (row['client'], row['session_id'], row['request_id']))
        replies, size, truncated = [], 0, False
        for reply in responses:
            size += len(reply[0].encode())
            if size > 8 * 1024 * 1024:
                truncated = True
                break
            replies.append(json.loads(reply[0]))
        return {'request': json.loads(row['data']), 'responses': replies, 'responses_truncated': truncated}

    @locked
    def delete_session(self, client, session):
        self.db.execute('DELETE FROM wave_index WHERE client=? AND session_id=?', (client, session))
        self.db.execute('DELETE FROM events WHERE client=? AND session_id=?', (client, session))
        self.db.execute('DELETE FROM sessions WHERE client=? AND session_id=?', (client, session))
        self.db.commit()

    @locked
    def prune(self, now=None):
        cutoff = (time.time() if now is None else now) - 7 * 86400
        self.db.execute('DELETE FROM events WHERE received<?', (cutoff,))
        self.db.execute('DELETE FROM wave_index WHERE seq NOT IN (SELECT seq FROM events)')
        self.db.execute('DELETE FROM sessions WHERE seen<?', (cutoff,))
        self.db.commit()
