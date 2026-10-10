"""Bounded employee lookup that never drops or multiplies an order reference."""

import re

from sqlglot import exp

from .semantic_types import SemanticRuntimeError, _parse_decimal, _parse_source_datetime


def canonical_manager_id(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError('manager reference must remain a string')
    text = value.strip(' ')
    if not re.fullmatch(r'[+-]?[0-9]+', text):
        return None
    digits = text.lstrip('+-').lstrip('0') or '0'
    if len(digits) > 19:
        return None
    number = int(('-' if text.startswith('-') else '') + digits)
    return str(number) if -(2**63) <= number < 2**63 else None


def _references(values):
    if not isinstance(values, tuple) or len(values) > 200:
        raise ValueError('bounded reference tuple required')
    if any(value is not None and (not isinstance(value,str) or len(value)>240
           or any(ord(char)<32 for char in value)) for value in values):
        raise ValueError('invalid manager reference')
    if len(set(values)) != len(values):
        raise ValueError('duplicate manager reference')


def compile_manager_lookup(references):
    _references(references)
    source = ' UNION ALL '.join(
        f"SELECT {index} AS reference_ordinal, {exp.Literal.string(value).sql(dialect='doris') if value is not None else 'CAST(NULL AS STRING)'} AS manager_reference"
        for index,value in enumerate(references,start=1)
    ) or 'SELECT 0 AS reference_ordinal, CAST(NULL AS STRING) AS manager_reference WHERE FALSE'
    # Canonicalize as text, then enforce the signed BIGINT range. A permissive
    # numeric cast must never turn malformed input into employee zero.
    return f"""WITH requested AS ({source}), reference_text AS (
 SELECT *,TRIM(manager_reference) AS raw_ref,
   COALESCE(NULLIF(REGEXP_REPLACE(REGEXP_REPLACE(TRIM(manager_reference),'^[-+]',''),'^0+',''),''),'0') AS digits
 FROM requested
), reference_keys AS (
 SELECT *,CASE WHEN raw_ref REGEXP '^[-+]?[0-9]+$'
   AND (LENGTH(digits)<19 OR (LENGTH(digits)=19 AND digits <=
       CASE WHEN SUBSTRING(raw_ref,1,1)='-' THEN '9223372036854775808' ELSE '9223372036854775807' END))
   THEN CASE WHEN SUBSTRING(raw_ref,1,1)='-' AND digits<>'0' THEN CONCAT('-',digits) ELSE digits END
   ELSE NULL END AS employee_key
 FROM reference_text
), employee_rows AS (
 SELECT CAST(e.employee_id AS STRING) AS employee_key,NULLIF(TRIM(e.actual_name),'') AS actual_name,
   e.deleted_flag,e.update_time
 FROM dms_ods.t_employee e
 WHERE EXISTS (SELECT 1 FROM reference_keys r WHERE r.employee_key=CAST(e.employee_id AS STRING))
), ranked AS (
 SELECT *,DENSE_RANK() OVER(PARTITION BY employee_key ORDER BY update_time DESC NULLS LAST) AS version_rank
 FROM employee_rows
), current_variants AS (
 SELECT DISTINCT employee_key,actual_name,deleted_flag,update_time FROM ranked WHERE version_rank=1
), employee_dimension AS (
 SELECT employee_key,COUNT(*) AS variant_count,MIN(actual_name) AS actual_name,
   MIN(deleted_flag) AS deleted_flag,MAX(update_time) AS source_updated
 FROM current_variants GROUP BY employee_key
), source_counts AS (
 SELECT employee_key,COUNT(*) AS source_rows FROM ranked WHERE version_rank=1 GROUP BY employee_key
), resolved AS (
 SELECT r.reference_ordinal,r.manager_reference,r.employee_key AS employee_id,
   CASE WHEN r.raw_ref IS NULL OR r.raw_ref='' THEN 'missing_reference'
        WHEN r.employee_key IS NULL THEN 'invalid_reference'
        WHEN e.employee_key IS NULL THEN 'not_found'
        WHEN e.variant_count>1 THEN 'conflicting_employee'
        WHEN e.deleted_flag IS NULL OR e.deleted_flag NOT IN (0,1) THEN 'invalid_employee_state'
        WHEN e.deleted_flag=1 THEN 'deleted_employee'
        WHEN e.actual_name IS NULL THEN 'missing_name'
        ELSE 'matched' END AS match_state,
   CASE WHEN e.variant_count=1 AND e.deleted_flag=0 THEN e.actual_name ELSE NULL END AS manager_name,
   COALESCE(e.variant_count,0) AS variant_count,COALESCE(c.source_rows,0) AS source_rows,e.source_updated
 FROM reference_keys r LEFT JOIN employee_dimension e ON e.employee_key=r.employee_key
 LEFT JOIN source_counts c ON c.employee_key=r.employee_key
)
SELECT *,{len(references)} AS group_count FROM resolved ORDER BY reference_ordinal LIMIT 501"""


def _integer(value):
    number=_parse_decimal(value)
    if number<0 or number!=number.to_integral_value():
        raise ValueError('invalid manager lookup count')
    return int(number)


def reconcile_manager_lookup(rows,references):
    _references(references)
    if not isinstance(rows,list) or len(rows)!=len(references):
        raise ValueError('incomplete manager lookup')
    result=[]
    for ordinal,(raw,row) in enumerate(zip(references,rows),start=1):
        if row['manager_reference']!=raw or _integer(row['reference_ordinal'])!=ordinal or _integer(row['group_count'])!=len(references):
            raise ValueError('manager references changed')
        expected=canonical_manager_id(raw)
        if row['employee_id']!=expected:
            raise ValueError('employee identity changed')
        state=row['match_state']
        variants,sources=_integer(row['variant_count']),_integer(row['source_rows'])
        name=row['manager_name']
        stamp=row['source_updated']
        updated=_parse_source_datetime(stamp,timezone_name='Asia/Shanghai').strftime('%Y-%m-%d %H:%M:%S') if stamp is not None else None
        if raw is None or not raw.strip(' '):
            allowed={'missing_reference'}
        elif expected is None:
            allowed={'invalid_reference'}
        else:
            allowed={'matched','not_found','conflicting_employee','invalid_employee_state','deleted_employee','missing_name'}
        if state not in allowed or sources<variants:
            raise ValueError('invalid manager match state')
        if sources and stamp is None:
            raise ValueError('missing employee version timestamp')
        if state in {'missing_reference','invalid_reference','not_found'}:
            if sources or variants or name is not None or stamp is not None:
                raise ValueError('unmatched employee has source attributes')
        elif state=='conflicting_employee':
            if variants<2 or name is not None:
                raise ValueError('conflict hidden by arbitrary name')
        elif variants!=1 or sources<1:
            raise ValueError('invalid unique employee controls')
        if state=='matched':
            if not isinstance(name,str) or not name or name!=name.strip(' '):
                raise ValueError('missing matched employee name')
        elif name is not None:
            raise ValueError('unresolved employee name exposed')
        result.append({'manager_reference':raw,'employee_id':expected,'manager_name':name,
            'match_state':state,'variant_count':variants,'source_rows':sources,'source_updated':updated})
    return result


def retrieve_manager_lookup(references,query):
    from .sales_pages import retrieve_sales_groups
    _references(references)
    if not references:
        return []
    try:
        rows=retrieve_sales_groups(compile_manager_lookup(references),query,max_rows=200)
        return reconcile_manager_lookup(rows,references)
    except SemanticRuntimeError as exc:
        if exc.reason != 'sales_page_invalid':
            raise
        raise SemanticRuntimeError('城市经理匹配结果存在缺页或变化，暂未展示',reason='dms_manager_reconciliation_failed') from exc
    except (ValueError,TypeError,KeyError,ArithmeticError) as exc:
        raise SemanticRuntimeError('城市经理匹配结果未通过完整性校验',reason='dms_manager_reconciliation_failed') from exc
