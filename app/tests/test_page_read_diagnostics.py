from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def audit_type():
    path=Path(__file__).resolve().parents[2]/'tests/acceptance/page_read_diagnostics.py'
    spec=importlib.util.spec_from_file_location('page_read_diagnostics_test',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.PageReadDiagnostics


def records():
    return [dict(result_ordinal=i+1,group_count=3,key=f'PRIVATE-{i}',quantity=str(i),updated='2026-09-10 00:00:00') for i in range(3)]


def complete(audit,rows):
    audit.observe(0,rows[:2])
    audit.observe(2,rows[2:])


def test_identical_reads_keep_only_fingerprints(audit_type):
    audit=audit_type(fields=('key','quantity','updated'),keys=('key',))
    rows=records()
    complete(audit,rows)
    complete(audit,deepcopy(rows))
    result=audit.summary()
    assert result['two_complete_reads'] and result['same_row_multiset'] and result['same_key_multiset']
    assert result['changed_fields']==[] and not result['untracked_content_changed']
    assert 'PRIVATE-' not in repr(vars(audit)) and '2026-09-10' not in repr(vars(audit))
    assert 'PRIVATE-' not in json.dumps(result)


def test_reordering_is_not_misdiagnosed_as_source_value_change(audit_type):
    audit=audit_type(fields=('key','quantity','updated'),keys=('key',))
    complete(audit,records())
    swapped=list(reversed(records()))
    for i,row in enumerate(swapped):row['result_ordinal']=i+1
    complete(audit,swapped)
    result=audit.summary()
    assert result['same_row_multiset'] and result['same_key_multiset']
    assert result['changed_fields']==['key','quantity']


@pytest.mark.parametrize('field',['quantity','updated','key'])
def test_changed_fields_and_identity_are_distinct(audit_type,field):
    audit=audit_type(fields=('key','quantity','updated'),keys=('key',))
    complete(audit,records())
    changed=records()
    changed[1][field]='CHANGED-PRIVATE'
    complete(audit,changed)
    result=audit.summary()
    assert result['two_complete_reads'] and not result['same_row_multiset']
    assert result['same_key_multiset']==(field!='key')
    assert result['changed_fields']==[field]
    assert 'CHANGED-PRIVATE' not in repr(result)


def test_missing_page_is_not_a_complete_comparison(audit_type):
    audit=audit_type(fields=('key',),keys=('key',))
    complete(audit,records())
    audit.observe(0,records()[:2])
    result=audit.summary()
    assert not result['two_complete_reads']
    assert result['same_row_multiset'] is None


def test_unknown_fields_are_never_named_in_output(audit_type):
    audit=audit_type(fields=('key','quantity'),keys=('key',))
    complete(audit,records())
    changed=records()
    changed[0]['PRIVATE-FIELD-NAME']='PRIVATE-VALUE'
    complete(audit,changed)
    result=audit.summary()
    assert result['untracked_content_changed'] and not result['same_row_multiset']
    assert 'PRIVATE' not in repr(result)


@pytest.mark.parametrize('failure',['offset','ordinal','count','over_budget'])
def test_invalid_page_evidence_cannot_claim_completeness(audit_type,failure):
    audit=audit_type(fields=('key',),keys=('key',),max_rows=2 if failure=='over_budget' else 100)
    rows=records()
    if failure=='ordinal':rows[0]['result_ordinal']=2
    if failure=='count':rows[-1]['group_count']=4
    audit.observe(1 if failure=='offset' else 0,rows)
    assert not audit.summary()['two_complete_reads']
    assert audit.summary()['invalid_page_evidence']


def test_empty_reads_can_be_compared_without_inventing_rows(audit_type):
    audit=audit_type(fields=('key',),keys=('key',))
    audit.observe(0,[])
    audit.observe(0,[])
    assert audit.summary()['two_complete_reads'] and audit.summary()['same_row_multiset']
