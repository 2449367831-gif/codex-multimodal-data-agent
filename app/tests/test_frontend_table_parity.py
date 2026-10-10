"""Every backend public table must survive browser validation and restoration."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from app.chat_api import PUBLIC_TABLE_SCHEMAS, PUBLIC_TABLE_SUMMARY_LABELS, _public_model_verified_table
from app.model_query_contract import MEASURE_UNITS


ROOT = Path(__file__).resolve().parents[2]
CHECK = r"""
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const contract = JSON.parse(readFileSync(0, 'utf8'));
const source = readFileSync('frontend/app.js', 'utf8');
const boundary = '  const proposalHost = {';
assert.equal(source.split(boundary).length, 2);
const sandbox = {TextEncoder, URL, window: {location: {origin: 'http://127.0.0.1:8021'}},
  document: {body: {}, getElementById: () => ({})}};
vm.createContext(sandbox);
vm.runInContext(source.replace(boundary, `
  globalThis.api = {normalizeBusinessTable, normalizePersistedBusinessTable,
    normalizeResponse, normalizeStoredEvent, serializeUiEvent, renderExecution,
    labels: BUSINESS_TABLE_LABELS, summaries: BUSINESS_TABLE_SUMMARY_LABELS};
  return;
${boundary}`), sandbox);
const api = sandbox.api;
for (const label of contract.summaries) assert.ok(api.summaries.has(label), 'summary label mismatch');
for (const [id, schema] of Object.entries(contract.schemas)) {
  const raw = {format: 'table', table_id: id, title: schema.title,
    caption: '', empty_label: schema.empty_label, columns: schema.columns,
    rows: [Object.fromEntries(schema.columns.map(c => [c.key,
      c.key === 'unit' ? 'EA' : c.kind === 'number' ? '1.25' : 'synthetic']))],
    summary: id === 'so07_order_details' ? [{label: '订单数', value: '1', unit: '单'}] : []};
  raw.rows.push(Object.fromEntries(schema.columns.map(c => [c.key,
    c.nullable ? null : c.key === 'unit' ? 'EA' : c.kind === 'number' ? (c.signed ? '-2.50' : '0') : 'synthetic'])));
  let table = api.normalizeBusinessTable(raw, 'completed');
  assert.ok(table, `public table rejected: ${id}`);
  let event = {kind: 'execution', question: '验证结果展示', index: 0,
    ...api.normalizeResponse({outcome: {state: 'completed'}, query_status: 'succeeded',
      result_state: 'live_success', business_table: raw,
      process: {steps: [{id: 'query', state: 'completed'}]}})};
  for (let refresh = 0; refresh < 3; refresh++) {
    assert.equal(event.outcome, 'completed', `response rejected: ${id}`);
    assert.equal(event.queryStatus, 'succeeded', `status lost: ${id}`);
    assert.ok(event.businessTable, `response table lost: ${id}`);
    const html = api.renderExecution(event);
    assert.ok(html.includes('查询完成，结果已通过校验'), `success label missing: ${id}`);
    assert.ok(!html.includes('execution-status-message'), `spurious failure: ${id}`);
    assert.ok(!html.includes('正在校验结果'), `terminal stage still running: ${id}`);
    assert.ok(!html.includes('data-retry-index'), `successful result retryable: ${id}`);
    event = api.normalizeStoredEvent(JSON.parse(JSON.stringify(api.serializeUiEvent(event))));
  }
  for (let index = 0; index < 2; index++) {
    table = api.normalizePersistedBusinessTable(JSON.parse(JSON.stringify(table)));
    assert.ok(table, `restored table rejected: ${id}`);
    assert.equal(JSON.stringify(table.rows), JSON.stringify(raw.rows), `values changed: ${id}`);
    for (let c = 0; c < schema.columns.length; c++) {
      assert.equal(table.columns[c].label, schema.columns[c].label, `label changed: ${id}`);
      assert.equal(table.columns[c].kind, schema.columns[c].kind, `kind changed: ${id}`);
      assert.equal(table.columns[c].unit || '', schema.columns[c].unit || '', `unit changed: ${id}`);
    }
  }
  const corrupt = JSON.parse(JSON.stringify(raw));
  corrupt.title = 'SELECT secret FROM internal_table';
  assert.equal(api.normalizeBusinessTable(corrupt, 'completed'), null, `unsafe table accepted: ${id}`);
  assert.equal(api.normalizeBusinessTable(raw, 'failed'), null, `failed result accepted: ${id}`);
}
for (const raw of contract.generic || []) {
  let table = api.normalizeBusinessTable(raw, 'completed');
  assert.ok(table, 'generic public table rejected');
  for (let index = 0; index < 3; index++) {
    assert.equal(JSON.stringify(table.rows), JSON.stringify(raw.rows), 'generic values changed');
    table.columns.forEach((c, i) => {
      assert.equal(c.label, raw.columns[i].label, 'generic label changed');
      assert.equal(c.kind, raw.columns[i].kind, 'generic kind changed');
      assert.equal(c.unit, raw.columns[i].unit, 'generic unit lost');
    });
    table = api.normalizePersistedBusinessTable(JSON.parse(JSON.stringify(table)));
    assert.ok(table, 'generic history rejected');
  }
  for (const unit of ['', null, true, {}, 'SELECT secret', '<script>', 'unknown-unit']) {
    const corrupt = JSON.parse(JSON.stringify(raw));
    corrupt.columns[1].unit = unit;
    assert.equal(api.normalizeBusinessTable(corrupt, 'completed'), null, 'invalid unit accepted');
  }
  const identifier = JSON.parse(JSON.stringify(raw));
  identifier.columns[0].unit = 'EA';
  assert.equal(api.normalizeBusinessTable(identifier, 'completed'), null, 'text identifier given a unit');
}
"""


def run_browser_contract(contract):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for browser-contract verification")
    completed = subprocess.run(
        [node, "-e", CHECK], cwd=ROOT, input=json.dumps(contract, ensure_ascii=False),
        text=True, encoding="utf-8", capture_output=True,
        timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert completed.returncode == 0, completed.stderr


def test_backend_public_tables_survive_frontend_contract():
    run_browser_contract({
        "schemas": PUBLIC_TABLE_SCHEMAS,
        "summaries": sorted(PUBLIC_TABLE_SUMMARY_LABELS),
    })


def test_generic_query_units_values_and_identifiers_survive_browser_and_history():
    tables = []
    for unit in sorted(MEASURE_UNITS):
        raw = {
            "columns": ["product_id", "quantity"],
            "rows": [
                {"product_id": "001234567890123456789", "quantity": "-12345678901234567890.00100"},
                {"product_id": "000002", "quantity": None},
                {"product_id": "000003", "quantity": "0.00000"},
            ],
            "result_columns": [
                {"key": "product_id", "label": "商品编码", "kind": "text"},
                {"key": "quantity", "label": "数值", "kind": "number", "unit": unit},
            ],
        }
        table = _public_model_verified_table(raw)
        assert table is not None
        tables.append(table)
    run_browser_contract({"schemas": {}, "summaries": [], "generic": tables})


def test_current_backend_generic_projection_without_legacy_marker_survives_browser():
    raw = {
        "format": "table", "title": "HJXH昨日SKU实发TOP20", "caption": "",
        "empty_label": "当前范围没有可展示的数据",
        "columns": [
            {"key": "col_1", "label": "SKU编码", "kind": "text", "align": "left"},
            {"key": "col_2", "label": "实发数量", "kind": "number", "align": "right", "signed": True, "unit": "EA"},
            {"key": "col_3", "label": "出库单数", "kind": "number", "align": "right", "signed": True, "unit": "单"},
        ],
        "rows": [{"col_1": "SKU-001", "col_2": "12", "col_3": "2"}],
        "summary": [],
    }
    run_browser_contract({"schemas": {}, "summaries": [], "generic": [raw]})
