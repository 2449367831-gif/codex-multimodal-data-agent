import importlib.util
from pathlib import Path
import sqlite3

import pytest
import sqlglot

from app.agent_core.dms_manager_lookup import canonical_manager_id

SPEC = importlib.util.spec_from_file_location('manager_reference', Path(__file__).resolve().parents[2] / 'tests/acceptance/check_dms_manager_live.py')
reference = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reference)


@pytest.mark.parametrize('raw',[None,'','  ','001','+01','-001','-00','12abc','1e2','1.5','１２','\u30001','9223372036854775807','-9223372036854775808','9223372036854775808','-9223372036854775809'])
def test_independent_identity_parser(raw):
    assert canonical_manager_id(raw)==reference.reference_id(raw)


def test_independent_reference_preserves_versions_conflicts_and_misses():
    db=sqlite3.connect(':memory:')
    db.row_factory=sqlite3.Row
    db.execute("ATTACH DATABASE ':memory:' AS dms_ods")
    db.execute('CREATE TABLE dms_ods.t_employee(employee_id INTEGER, actual_name TEXT, deleted_flag INTEGER, update_time TEXT)')
    rows=[(1,'Old',0,'2026-09-01'),(1,' New ',0,'2026-09-10'),(1,' New ',0,'2026-09-10'),
          (2,'Deleted',1,'2026-09-10'),(3,'First',0,'2026-09-10'),(3,'Second',0,'2026-09-10'),
          (4,None,0,'2026-09-10'),(5,'Invalid',2,'2026-09-10')]
    db.executemany('INSERT INTO dms_ods.t_employee VALUES(?,?,?,?)',rows)
    refs=('001','2','3','4','5','6',None,'','bad')
    observed=[dict(row) for row in db.execute(sqlglot.transpile(reference.reference_sql(refs),read='doris',write='sqlite')[0])]
    expected=reference.expected_lookup(observed,refs)
    assert [row['match_state'] for row in expected]==['matched','deleted_employee','conflicting_employee','missing_name','invalid_employee_state','not_found','missing_reference','missing_reference','invalid_reference']
    assert expected[0]['manager_name']=='New' and expected[0]['source_rows']==2
    assert expected[2]['variant_count']==2
    assert all(row['manager_name'] is None for row in expected[1:])
    db.close()
