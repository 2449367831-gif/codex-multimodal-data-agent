"""Durable export plans and hashes only. Result bytes remain in bounded memory."""

from dataclasses import asdict
from contextlib import contextmanager
from datetime import date
from hashlib import sha256
import csv
import io
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .agent_core.sales_detail_batches import SalesDetailCursor
from .agent_core.sales_detail_partitions import SalesDetailPartitionPlan
from .agent_core.sales_export import EXPORT_ROWS, summary_digest
from .query_scope import QueryScope
from .sales_detail_contexts import DetailContextError


EXPORT_ID = re.compile(r'ex_[a-f0-9]{32}\Z')
MAX_EXPORT_BYTES = 32 * 1024 * 1024
MAX_EXPORT_ROWS = 100_000_000
CSV_FIELDS = ('cargo_owner', 'warehouse_code', 'outbound_code', 'sku_code', 'ship_at', 'quantity')
CSV_LABELS = ('货主', '仓库编码', '出库单号', 'SKU编码', '出库时间', '实发数量（EA）')


def export_error(message, code='export_unavailable', status=409):
    error = DetailContextError(message)
    error.code, error.http_status = code, status
    return error


def csv_bytes(rows, *, first):
    output = io.BytesIO()
    line = io.StringIO(newline='')
    if first:
        output.write(b'\xef\xbb\xbf')
    writer = csv.writer(line, lineterminator='\r\n')

    def emit(values):
        line.seek(0)
        line.truncate(0)
        writer.writerow(values)
        encoded = line.getvalue().encode('utf-8')
        if output.tell() + len(encoded) > MAX_EXPORT_BYTES:
            raise export_error('本批文件超过容量，未继续写入', status=413)
        output.write(encoded)

    if first:
        emit(CSV_LABELS)
    for row in rows:
        values = []
        for key in CSV_FIELDS:
            value = str(row[key])
            # Preserve identifiers and neutralize spreadsheet formula evaluation.
            if value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r', '\n')) or value[:1] in '\t\r\n':
                value = "'" + value
            values.append(value)
        emit(values)
    return output.getvalue()


class SalesExportStore:
    def __init__(self, path, *, clock=time.time, chunk_rows=EXPORT_ROWS):
        if type(chunk_rows) is not int or not 1 <= chunk_rows <= EXPORT_ROWS:
            raise ValueError('invalid export chunk size')
        self.path, self.clock, self.chunk_rows = Path(path), clock, chunk_rows
        with self.connect() as db:
            db.executescript('''
CREATE TABLE IF NOT EXISTS sales_exports_v1 (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, conversation TEXT NOT NULL, source TEXT NOT NULL,
 plan TEXT NOT NULL, definition TEXT NOT NULL, baseline TEXT NOT NULL, total INTEGER NOT NULL,
 chunk_rows INTEGER NOT NULL, expires INTEGER NOT NULL, UNIQUE(owner, conversation, source));
CREATE TABLE IF NOT EXISTS sales_export_hashes_v1 (
 export_id TEXT NOT NULL, position INTEGER NOT NULL, digest TEXT NOT NULL, bytes INTEGER NOT NULL,
 prefix_digest TEXT NOT NULL,
 PRIMARY KEY(export_id, position));
''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, owner, conversation, source, cursor):
        if type(cursor) is not SalesDetailCursor or cursor.loaded or cursor.complete:
            raise ValueError('initial checked export required')
        if not 1 <= cursor.baseline.detail_count <= MAX_EXPORT_ROWS:
            raise export_error('原范围超过单文件导出容量', status=413)
        plan = json.dumps({'start': cursor.plan.start.isoformat(), 'end': cursor.plan.end.isoformat(),
                           'scope': asdict(cursor.plan.scope)}, ensure_ascii=False, sort_keys=True)
        if len(plan.encode()) > 32000:
            raise ValueError('export plan too large')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM sales_export_hashes_v1 WHERE export_id IN (SELECT id FROM sales_exports_v1 WHERE expires <= ?)', (self.clock(),))
            db.execute('DELETE FROM sales_exports_v1 WHERE expires <= ?', (self.clock(),))
            prior = db.execute('SELECT * FROM sales_exports_v1 WHERE owner=? AND conversation=? AND source=?', (owner, conversation, source)).fetchone()
            if prior:
                return prior['id']
            if (db.execute('SELECT COUNT(*) FROM sales_exports_v1').fetchone()[0] >= 1000
                or db.execute('SELECT COUNT(*) FROM sales_exports_v1 WHERE owner=?', (owner,)).fetchone()[0] >= 32):
                raise export_error('导出任务容量不足，请稍后重试', status=503)
            identifier = 'ex_' + uuid.uuid4().hex
            db.execute('INSERT INTO sales_exports_v1 VALUES (?,?,?,?,?,?,?,?,?,?)',
                (identifier, owner, conversation, source, plan, cursor.definition, summary_digest(cursor.baseline),
                 cursor.baseline.detail_count, self.chunk_rows, int(self.clock() + 7*86400)))
        return identifier

    def get(self, owner, conversation, identifier):
        if not isinstance(identifier, str) or not EXPORT_ID.fullmatch(identifier):
            raise export_error('导出任务不存在', status=404)
        with self.connect() as db:
            row = db.execute('SELECT * FROM sales_exports_v1 WHERE id=? AND owner=? AND conversation=?',
                             (identifier, owner, conversation)).fetchone()
        if row is None:
            raise export_error('导出任务不存在', status=404)
        if row['expires'] <= self.clock():
            raise export_error('导出已过期，请重新查询', 'export_expired', 410)
        return dict(row)

    @staticmethod
    def plan(item):
        raw = json.loads(item['plan'])
        scope = raw['scope']
        for key in ('warehouse_codes', 'warehouse_labels', 'sku_codes', 'product_terms', 'finished_goods_exceptions'):
            scope[key] = tuple(scope[key])
        return SalesDetailPartitionPlan(date.fromisoformat(raw['start']), date.fromisoformat(raw['end']), QueryScope(**scope))

    @staticmethod
    def public(item):
        return {'export_id': item['id'], 'total_rows': item['total'], 'chunk_rows': item['chunk_rows'],
                'chunks': (item['total'] + item['chunk_rows'] - 1) // item['chunk_rows'], 'expires_at': item['expires']}

    def position(self, item, index):
        if type(index) is not int or not 0 <= index < self.public(item)['chunks']:
            raise export_error('导出位置无效', status=422)
        with self.connect() as db:
            count = db.execute('SELECT COUNT(*) FROM sales_export_hashes_v1 WHERE export_id=?', (item['id'],)).fetchone()[0]
        if index > count:
            raise export_error('导出顺序不连续，未跳过数据')
        return index * item['chunk_rows']

    def previous_digest(self, item, index):
        if not index:
            return None
        with self.connect() as db:
            row = db.execute('SELECT prefix_digest FROM sales_export_hashes_v1 WHERE export_id=? AND position=?',
                             (item['id'], index - 1)).fetchone()
        if row is None:
            raise export_error('前一批尚未完成，未跳过数据')
        return row[0]

    def record(self, item, index, encoded, prefix_digest):
        digest = sha256(encoded).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT digest,bytes,prefix_digest FROM sales_export_hashes_v1 WHERE export_id=? AND position=?', (item['id'], index)).fetchone()
            if prior and (prior['digest'] != digest or prior['bytes'] != len(encoded) or prior['prefix_digest'] != prefix_digest):
                raise export_error('已保存的数据块发生变化，请重新查询后导出', 'detail_source_changed')
            db.execute('INSERT OR IGNORE INTO sales_export_hashes_v1 VALUES (?,?,?,?,?)', (item['id'], index, digest, len(encoded), prefix_digest))
        return digest
