"""Bounded, asynchronous SQLite duration distributions; no HTTP payload capture."""
import asyncio
from collections import Counter
from concurrent.futures import Future
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
from queue import Empty, Full, Queue
import sqlite3
from threading import Thread
from time import time,perf_counter
from ._version import __version__

TOOL_VERSION = __version__
SCHEMA_VERSION = 1
MAX_SESSIONS = 100
APPLICATION_ID = 0x4154554E
BATCH_SECONDS = .02
BATCH_LIMIT = 64


def collect_events(first,queue,*,clock=perf_counter):
    """Coalesce a bounded interval of events; exports/close preserve FIFO barriers."""
    batch=[first];deadline=clock()+BATCH_SECONDS
    while len(batch)<BATCH_LIMIT:
        remaining=deadline-clock()
        if remaining<=0:break
        try:following=queue.get(timeout=remaining)
        except Empty:break
        if following[0]!='event':return batch,following
        batch.append(following[1])
    return batch,None


class StorageUnavailable(OSError):
    """A sanitized storage failure; forwarding can continue."""


@lru_cache(maxsize=1)
def source_digest():
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.rglob("*.py")):
        digest.update(path.relative_to(Path(__file__).parent).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def utc(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def config_identity(config):
    data = config.model_dump()
    data["metrics"]["storage_path"] = str(Path(config.metrics.storage_path).resolve())
    identity_data=dict(data)
    if config.tuning.model_dump()==type(config.tuning)().model_dump():identity_data.pop("tuning")
    digest = hashlib.sha256(json.dumps(identity_data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    data['upstream'] = config.upstream.split(':', 1)[0] + '://{upstream}'
    data['listen'] = 'loopback'
    data['metrics']['storage_path'] = '{local_storage}'
    return digest, data


def connect_readonly(path):
    connection = None
    try:
        connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=.05)
        connection.execute('PRAGMA query_only=ON')
        if (connection.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION or
            connection.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID):
            raise StorageUnavailable('Unsupported telemetry schema')
        connection.execute("BEGIN")
        return connection
    except (sqlite3.Error, StorageUnavailable):
        if connection is not None:
            connection.close()
        raise StorageUnavailable('Telemetry storage unavailable') from None


def history(connection, digest):
    rows = connection.execute('SELECT id, started, ended, health FROM sessions WHERE config_digest=? ORDER BY started DESC, id DESC LIMIT ?',
                              (digest, MAX_SESSIONS)).fetchall()
    return [dict(session_id=row[0], started_at=utc(row[1]), ended_at=utc(row[2]) if row[2] else None,
                 shutdown_recorded=row[2] is not None, storage=json.loads(row[3])) for row in rows]


def report(connection, digest, session_id=None):
    if session_id is None:
        found = connection.execute('SELECT id FROM sessions WHERE config_digest=? ORDER BY started DESC, id DESC LIMIT 1', (digest,)).fetchone()
        if found is None:
            raise StorageUnavailable('No retained telemetry session for this configuration')
        session_id = found[0]
    meta = connection.execute('SELECT started, ended, config, health, tool_version, source_digest FROM sessions WHERE id=? AND config_digest=?',
                              (session_id, digest)).fetchone()
    if meta is None:
        raise StorageUnavailable('Session not found for this configuration')
    # Store exact duration frequencies, not event payloads or full event timestamps.
    # Weighted nearest rank stays exact for the retained minute buckets.
    sql = '''WITH histogram AS (
      SELECT stream,method,route,duration,SUM(n) n FROM samples WHERE session=? GROUP BY stream,method,route,duration
    ), ranked AS (
      SELECT *,SUM(n) OVER(PARTITION BY stream,method,route ORDER BY duration ROWS UNBOUNDED PRECEDING) cumulative,
      SUM(n) OVER(PARTITION BY stream,method,route) total FROM histogram
    ), quantiles AS (
      SELECT stream,method,route,MIN(CASE WHEN cumulative>=total*.5 THEN duration END) p50,
      MIN(CASE WHEN cumulative>=total*.95 THEN duration END) p95,
      MIN(CASE WHEN cumulative>=total*.99 THEN duration END) p99 FROM ranked GROUP BY stream,method,route
    ) SELECT s.stream,s.method,s.route,SUM(s.n),SUM(s.n*s.duration)/SUM(s.n),q.p50,q.p95,q.p99,
      SUM(CASE WHEN s.status>=400 THEN s.n ELSE 0 END),SUM(CASE WHEN s.status>=500 THEN s.n ELSE 0 END),
      SUM(CASE WHEN s.cache='HIT' THEN s.n ELSE 0 END),MIN(s.minute),MAX(s.minute),
      SUM(CASE WHEN s.cache='MISS' THEN s.n ELSE 0 END),SUM(CASE WHEN s.cache='BYPASS' THEN s.n ELSE 0 END)
      FROM samples s JOIN quantiles q USING(stream,method,route) WHERE s.session=? GROUP BY s.stream,s.method,s.route'''
    routes = []
    for r in connection.execute(sql, (session_id, session_id)):
        routes.append(dict(boundary=r[0], method=r[1], route=r[2], count=r[3], mean_ms=r[4], p50_ms=r[5], p95_ms=r[6],
                           p99_ms=r[7], errors=r[8], server_errors=r[9], cache_hits=r[10], cache_misses=r[13], cache_bypasses=r[14],
                           error_rate=r[8]/r[3], first_bucket_utc=utc(r[11]*60), last_bucket_utc=utc(r[12]*60)))
    health = json.loads(meta[3])
    health['sampling_may_be_incomplete'] = bool(health.get('sampling_incomplete') or meta[1] is None or health.get('queue_depth'))
    health['durability_boundary'] = 'last committed writer checkpoint; pending or abrupt-exit observations may be absent'
    return dict(schema_version=1, tool_version=meta[4], session_id=session_id, config_digest=digest,
                effective_config=json.loads(meta[2]), effective_mode=(json.loads(meta[2])['mode'] if 'tuning' in json.loads(meta[2]) else 'cache' if json.loads(meta[2])['mode']=='cache' else 'observe'), started_at=utc(meta[0]),
                ended_at=utc(meta[1]) if meta[1] else None, shutdown_recorded=meta[1] is not None,
                generated_at=utc(time()), source_sha256=meta[5], workload='observed proxy traffic; no replay or benchmark',
                aggregation=dict(bucket_seconds=60, percentiles='exact weighted nearest rank over retained duration frequencies',
                                 boundaries={'proxy':'handling through downstream completion', 'upstream':'active upstream I/O excluding admission and downstream send waits'}),
                sampling=health, routes=routes, tuning_decisions=decision_history(connection,session_id))


def decision_history(connection,session_id):
    if not connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='decisions'").fetchone():return []
    rows=connection.execute('SELECT data FROM decisions WHERE session=? ORDER BY sequence', (session_id,))
    return [json.loads(row[0]) for row in rows]


class TelemetryStorage:
    def __init__(self, config, session_id, *, clock=time, transport_counters=None):
        self.config, self.session_id, self.clock = config, session_id, clock
        self.transport_counters = transport_counters if transport_counters is not None else {}
        self.path = Path(config.metrics.storage_path)
        self.digest, self.redacted_config = config_identity(config)
        self.queue = Queue(maxsize=config.metrics.writer_queue_capacity)
        self.thread = None
        self.started_at = clock()
        self.accepting = False
        self.state = 'not_started'
        self.last_error = None
        self.stats = dict(enqueued_observations=0, persisted_observations=0, dropped_queue=0, dropped_storage=0,
                          retention_truncated=0, budget_truncated=0, sessions_truncated=0, write_errors=0)
        self.disk_bytes = 0
        self.decision_sequence=0
        self.stats.update(persisted_tuning_decisions=0,dropped_tuning_decisions=0,tuning_decisions_truncated=0)
        self.stats.update(write_batches=0,largest_batch_observations=0)

    def health(self):
        return dict(persistent=self.state in ('ready', 'closed'), state=self.state, last_error=self.last_error,
                    writer_batch_window_ms=BATCH_SECONDS*1000,writer_batch_limit=BATCH_LIMIT,
                    queue_depth=self.queue.qsize(), queue_capacity=self.queue.maxsize, disk_bytes=self.disk_bytes,
                    disk_budget_bytes=self.config.metrics.storage_max_bytes,
                    tuning_history_may_be_incomplete=bool(self.stats['dropped_tuning_decisions'] or self.stats['tuning_decisions_truncated']),transport=dict(self.transport_counters), **self.stats,
                    sampling_incomplete=bool(self.stats['dropped_queue'] or self.stats['dropped_storage'] or
                                               self.stats['retention_truncated'] or self.stats['budget_truncated'] or
                                               self.stats['dropped_tuning_decisions'] or self.stats['tuning_decisions_truncated']))

    def submit(self, stream, row):
        if not self.accepting:
            self.stats['dropped_storage'] += 1
            return
        try:
            self.queue.put_nowait(('event', (stream, dict(row)), None))
            self.stats['enqueued_observations'] += 1
        except Full:
            self.stats['dropped_queue'] += 1

    def record_decision(self,decision):
        if not self.accepting:
            self.stats['dropped_tuning_decisions']+=1
            return
        self.decision_sequence+=1
        try:
            self.queue.put_nowait(('decision',(self.decision_sequence,self.clock(),dict(decision)),None))
        except Full:
            self.stats['dropped_tuning_decisions']+=1

    async def start(self):
        ready = Future()
        ready.set_running_or_notify_cancel()
        self.thread = Thread(target=self._run, args=(ready,), name='tuneio-telemetry', daemon=True)
        self.thread.start()
        try:
            await asyncio.wrap_future(ready)
        except asyncio.CancelledError:
            await self.close()
            raise

    async def query(self, kind, session_id=None):
        if not self.accepting:
            raise StorageUnavailable('Telemetry storage unavailable')
        future = Future()
        try:
            self.queue.put_nowait((kind, session_id, future))
        except Full:
            raise StorageUnavailable('Telemetry queue is busy; retry export') from None
        return await asyncio.wrap_future(future)

    async def close(self):
        if self.thread is None:
            return
        self.accepting = False
        # The bounded worker drains accepted events before recording clean shutdown.
        await asyncio.to_thread(self.queue.put, ('close', None, None))
        await asyncio.to_thread(self.thread.join)
        self.thread = None

    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=.05)
        try:
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            owned = connection.execute('PRAGMA application_id').fetchone()[0] == APPLICATION_ID
            if (version and not owned) or (version == 0 and connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone()):
                raise StorageUnavailable('Not a tuneio telemetry database')
            if version not in (0, SCHEMA_VERSION):
                raise StorageUnavailable('Unsupported telemetry schema')
            pages = self.config.metrics.storage_max_bytes // 8192 - 4
            if connection.execute('PRAGMA page_count').fetchone()[0] > pages:
                raise StorageUnavailable('Existing telemetry exceeds configured disk budget')
            connection.execute('PRAGMA page_size=4096')
            connection.execute('PRAGMA auto_vacuum=FULL')
            connection.execute('PRAGMA journal_mode=DELETE')
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('PRAGMA temp_store=MEMORY')
            # Reserve half the disk allowance for the rollback journal, plus header slack.
            connection.execute(f'PRAGMA max_page_count={pages}')
            connection.executescript('''
              CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, started REAL NOT NULL, ended REAL,
                config_digest TEXT NOT NULL, config TEXT NOT NULL, health TEXT NOT NULL, tool_version TEXT NOT NULL, source_digest TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS samples(session TEXT NOT NULL, minute INTEGER NOT NULL, stream TEXT NOT NULL,
                method TEXT NOT NULL, route TEXT NOT NULL, status INTEGER NOT NULL, cache TEXT NOT NULL,
                duration REAL NOT NULL, n INTEGER NOT NULL,
                PRIMARY KEY(session,minute,stream,method,route,status,cache,duration)) WITHOUT ROWID;
              CREATE INDEX IF NOT EXISTS samples_age ON samples(minute);
              CREATE TABLE IF NOT EXISTS decisions(session TEXT NOT NULL,sequence INTEGER NOT NULL,created REAL NOT NULL,
                data TEXT NOT NULL,PRIMARY KEY(session,sequence)) WITHOUT ROWID;
              PRAGMA user_version=1;
              PRAGMA application_id=1096045902;
            ''')
            self._maintain(connection)
            connection.commit()
            for attempt in range(MAX_SESSIONS + 1):
                try:
                    with connection:
                        connection.execute('INSERT INTO sessions VALUES(?,?,NULL,?,?,?,?,?)',
                                           (self.session_id, self.started_at, self.digest, json.dumps(self.redacted_config), json.dumps(self.health()), TOOL_VERSION, source_digest()))
                    break
                except sqlite3.Error as exc:
                    if getattr(exc, 'sqlite_errorcode', None) != sqlite3.SQLITE_FULL:
                        raise
                    oldest = connection.execute('SELECT id FROM sessions ORDER BY started,id LIMIT 1').fetchone()
                    if oldest is None:
                        raise
                    with connection:
                        n = connection.execute('SELECT COALESCE(SUM(n),0) FROM samples WHERE session=?', oldest).fetchone()[0]
                        connection.execute('DELETE FROM samples WHERE session=?', oldest)
                        connection.execute('DELETE FROM decisions WHERE session=?',oldest)
                        connection.execute('DELETE FROM sessions WHERE id=?', oldest)
                    self.stats['sessions_truncated'] += 1
                    self.stats['budget_truncated'] += n
            return connection
        except (sqlite3.Error, OSError):
            connection.close()
            raise

    def _truncate(self, connection, *, budget=False):
        if budget:
            oldest = connection.execute('SELECT MIN(minute) FROM samples').fetchone()[0]
            if oldest is None:
                return False
            where, args, counter = 'minute=?', (oldest,), 'budget_truncated'
        else:
            where, args, counter = 'minute<?', (math.floor((self.clock()-self.config.metrics.retention_days*86400)/60),), 'retention_truncated'
        count = connection.execute('SELECT COALESCE(SUM(n),0) FROM samples WHERE '+where, args).fetchone()[0]
        # Attribute lost rows to their own retained sessions, including earlier runs.
        affected = connection.execute('SELECT session,SUM(n) FROM samples WHERE '+where+' GROUP BY session', args).fetchall()
        for session, n in affected:
            if session != self.session_id:
                row = connection.execute('SELECT health FROM sessions WHERE id=?', (session,)).fetchone()
                old = json.loads(row[0])
                old[counter] = old.get(counter, 0) + n
                old['sampling_incomplete'] = True
                connection.execute('UPDATE sessions SET health=? WHERE id=?', (json.dumps(old), session))
        connection.execute('DELETE FROM samples WHERE '+where, args)
        self.stats[counter] += count
        return bool(count)

    def _maintain(self, connection):
        self._truncate(connection)
        cursor=connection.execute('DELETE FROM decisions WHERE created<?',(self.clock()-self.config.metrics.retention_days*86400,))
        self.stats['tuning_decisions_truncated']+=cursor.rowcount
        stale = connection.execute('SELECT id FROM sessions WHERE started<? AND id!=?',
                                   (self.clock()-self.config.metrics.retention_days*86400, self.session_id)).fetchall()
        overflow = connection.execute('SELECT id FROM sessions WHERE id!=? ORDER BY started DESC,id DESC LIMIT -1 OFFSET ?',
                                      (self.session_id, MAX_SESSIONS-1)).fetchall()
        for (session,) in set(stale+overflow):
            connection.execute('DELETE FROM samples WHERE session=?', (session,))
            connection.execute('DELETE FROM decisions WHERE session=?',(session,))
            connection.execute('DELETE FROM sessions WHERE id=?', (session,))
            self.stats['sessions_truncated'] += 1

    def _reclaim_budget(self,connection):
        """Audit rows also consume the bounded DB; disclose eviction to their session."""
        oldest=connection.execute('SELECT session,sequence FROM decisions ORDER BY created,sequence LIMIT 8').fetchall()
        if not oldest:return self._truncate(connection,budget=True)
        # Removing one small row need not release a SQLite page. Reclaim a bounded
        # group so the retry can obtain page-level space instead of exhausting
        # its attempts while deleting fragments within still-occupied pages.
        connection.executemany('DELETE FROM decisions WHERE session=? AND sequence=?',oldest)
        for session,count in Counter(session for session,sequence in oldest).items():
            if session==self.session_id:
                self.stats['tuning_decisions_truncated']+=count
                continue
            row=connection.execute('SELECT health FROM sessions WHERE id=?',(session,)).fetchone()
            if row is not None:
                health=json.loads(row[0]);health['tuning_decisions_truncated']=health.get('tuning_decisions_truncated',0)+count
                health.update(tuning_history_may_be_incomplete=True,sampling_incomplete=True)
                connection.execute('UPDATE sessions SET health=? WHERE id=?',(json.dumps(health),session))
        return True

    def _write_decision(self,connection,data):
        sequence,created,decision=data
        for attempt in range(9):
            try:
                with connection:
                    self._maintain(connection)
                    removed=connection.execute('DELETE FROM decisions WHERE session=? AND sequence NOT IN '
                        '(SELECT sequence FROM decisions WHERE session=? ORDER BY sequence DESC LIMIT ?)',
                        (self.session_id,self.session_id,max(0,self.config.tuning.history_capacity-1))).rowcount
                    connection.execute('INSERT INTO decisions VALUES(?,?,?,?)',(self.session_id,sequence,created,json.dumps(decision)))
                self.stats['tuning_decisions_truncated']+=removed
                self.stats['persisted_tuning_decisions']+=1
                self.state,self.last_error='ready',None
                return
            except sqlite3.Error as exc:
                if getattr(exc,'sqlite_errorcode',None)!=sqlite3.SQLITE_FULL or attempt==8:raise
                with connection:
                    if not self._reclaim_budget(connection):raise

    def _checkpoint(self, connection, ended=None):
        for _ in range(MAX_SESSIONS + 64):
            self.disk_bytes = sum(p.stat().st_size for p in (self.path, Path(str(self.path)+'-journal'), Path(str(self.path)+'.writer-lock')) if p.exists())
            try:
                with connection:
                    connection.execute('UPDATE sessions SET ended=?,health=? WHERE id=?',
                                       (ended, json.dumps(dict(self.health(), checkpoint_at_utc=utc(self.clock()))), self.session_id))
                self.disk_bytes = self.path.stat().st_size
                return
            except sqlite3.Error as exc:
                if getattr(exc, 'sqlite_errorcode', None) != sqlite3.SQLITE_FULL:
                    raise
                with connection:
                    if self._reclaim_budget(connection):
                        continue
                    oldest = connection.execute('SELECT id FROM sessions WHERE id!=? ORDER BY started,id LIMIT 1', (self.session_id,)).fetchone()
                    if oldest is None:
                        raise
                    connection.execute('DELETE FROM decisions WHERE session=?',oldest)
                    connection.execute('DELETE FROM sessions WHERE id=?', oldest)
                    self.stats['sessions_truncated'] += 1
        raise StorageUnavailable('Unable to checkpoint within storage budget')

    def _write(self, connection, batch):
        rows = [(self.session_id, int(r['timestamp']//60), stream, r['method'], r['route'], r['status'],
                 r['cache'], r['duration_ms'], 1) for stream, r in batch]
        for attempt in range(9):
            try:
                with connection:
                    self._maintain(connection)
                    connection.executemany('INSERT INTO samples VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET n=n+1', rows)
                self.stats['persisted_observations'] += len(rows)
                self.stats['write_batches']+=1
                self.stats['largest_batch_observations']=max(self.stats['largest_batch_observations'],len(rows))
                self.state, self.last_error = 'ready', None
                return
            except sqlite3.Error as exc:
                if getattr(exc, 'sqlite_errorcode', None) == sqlite3.SQLITE_FULL and attempt < 8:
                    with connection:
                        if not self._reclaim_budget(connection):raise
                    continue
                raise

    def _run(self, ready):
        connection = lease = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Separate SQLite lock file allows read-only reports while excluding a second writer.
            # OS locks are released on process exit, so crashes leave no stale PID lock.
            lease = sqlite3.connect(str(self.path)+'.writer-lock', timeout=.05)
            lease.execute('BEGIN EXCLUSIVE')
            connection = self._open()
            self.state, self.accepting = 'ready', True
            self._maintain(connection)
            self._checkpoint(connection)
        except (OSError, sqlite3.Error):
            self.state, self.last_error = 'degraded', 'storage_open_failed'
            self.stats['write_errors'] += 1
            if connection is not None:
                connection.close()
            if lease is not None:
                lease.close()
            ready.set_result(None)
            return
        ready.set_result(None)
        pending = None
        while True:
            kind, data, future = pending or self.queue.get()
            pending = None
            batch = []
            written = False
            if future is not None and not future.set_running_or_notify_cancel():
                continue
            try:
                if kind == 'close':
                    self.accepting = False
                    self._maintain(connection)
                    self.state = 'closed'
                    self._checkpoint(connection, self.clock())
                    break
                if kind == 'decision':
                    self._write_decision(connection,data)
                    written=True
                elif kind == 'event':
                    batch,pending=collect_events(data,self.queue)
                    self._write(connection, batch)
                    written = True
                else:
                    self._maintain(connection)
                self._checkpoint(connection)
                if future is not None:
                    value = history(connection, self.digest) if kind == 'sessions' else report(connection, self.digest, data) if kind == 'report' else None
                    if not future.cancelled():
                        future.set_result(value)
            except StorageUnavailable:
                if future is not None:future.set_exception(StorageUnavailable('Session not found for this configuration'))
                else:
                    self.state,self.last_error='degraded','storage_write_failed'
                    self.stats['write_errors']+=1
                    if kind=='decision' and not written:self.stats['dropped_tuning_decisions']+=1
                    if kind=='event' and not written:self.stats['dropped_storage']+=len(batch)
                    if kind=='close':break
            except (OSError, sqlite3.Error, ValueError):
                self.state, self.last_error = 'degraded', 'storage_write_or_read_failed'
                self.stats['write_errors'] += 1
                if kind == 'event' and not written:
                    self.stats['dropped_storage'] += len(batch)
                if kind == 'decision' and not written:
                    self.stats['dropped_tuning_decisions']+=1
                if future is not None and not future.cancelled():
                    future.set_exception(StorageUnavailable('Telemetry storage unavailable'))
                if kind == 'close':
                    break
        connection.close()
        lease.close()
