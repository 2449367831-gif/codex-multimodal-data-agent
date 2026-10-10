"""Complete, stable sales aggregate pages without relaxing backend row limits."""

from hashlib import sha256
import json
import math
import time

import sqlglot
from sqlglot import exp

from .semantic_types import SemanticRuntimeError


class AggregatePageError(SemanticRuntimeError):
    """Static internal diagnostics; no rows, keys or database error text."""

    def __init__(self, failure, pass_number, *, reason="sales_page_invalid"):
        super().__init__("分批结果存在缺页或变化，暂未展示", reason=reason)
        allowed = {'batch_shape', 'page_shape', 'page_length', 'ordinal_changed', 'count_changed', 'content_changed'}
        self.page_failure = failure if failure in allowed else 'invalid_response'
        self.pass_number = pass_number


class _InvalidPage(ValueError):
    pass


def _result_order(base, order):
    projections=list(base.selects)
    has_star=any(projection.is_star for projection in projections)
    named=[projection for projection in projections if not projection.is_star and projection.alias_or_name]
    if any(projection.alias_or_name.casefold()=='result_ordinal' for projection in named):
        raise ValueError('reserved aggregate ordinal column')
    def output(projection):
        if projection.is_star or not projection.alias_or_name:
            raise ValueError('ordering requires a named result column')
        return exp.column(projection.alias_or_name,table='aggregate_source',quoted=True)
    def comparable(expression):
        normalized=expression.copy()
        for column in normalized.find_all(exp.Column):
            column.set('this',exp.to_identifier(column.name.casefold()))
        return normalized
    for term in order.expressions:
        target=term.this
        if isinstance(target,exp.Literal) and target.is_int:
            position=int(target.this)
            if has_star or not 1<=position<=len(projections):
                raise ValueError('unresolved aggregate order position')
            term.set('this',output(projections[position-1]))
            continue
        matches=[]
        if isinstance(target,exp.Column) and not target.table:
            matches=[projection for projection in named if projection.alias_or_name.casefold()==target.name.casefold()]
        if not matches:
            for projection in named:
                expression=projection.this if isinstance(projection,exp.Alias) else projection
                if comparable(expression)==comparable(target) or (
                    isinstance(expression,exp.Column) and isinstance(target,exp.Column)
                    and expression.name.casefold()==target.name.casefold() and (not expression.table or not target.table)
                ):
                    matches.append(projection)
        if len(matches)==1:
            term.set('this',output(matches[0]))
        elif not matches and has_star and isinstance(target,exp.Column):
            term.set('this',exp.column(target.name,table='aggregate_source',quoted=True))
        else:
            raise ValueError('aggregate ordering is not an unambiguous result column')
    return order


def aggregate_page_sql(sql, offset, page_size=200, *, page_cap=500):
    if (type(offset) is not int or offset < 0 or type(page_cap) is not int or not 1 <= page_cap <= 10000
        or type(page_size) is not int or not 1 <= page_size <= page_cap):
        raise ValueError("invalid aggregate page bounds")
    base = sqlglot.parse_one(sql, read="doris")
    order = base.args.pop("order", None)
    if not order:
        raise ValueError("stable group ordering required")
    base.set("limit", None)
    # Number the completed relation, after DISTINCT, grouping and existing windows.
    # Inserting ROW_NUMBER into that projection can change its rows or alias meaning.
    ordered=_result_order(base,order.copy())
    numbered=exp.select('aggregate_source.*',exp.alias_(exp.Window(this=exp.RowNumber(),order=ordered),'result_ordinal')).from_(base.subquery('aggregate_source'))
    return exp.select("*").from_(numbered.subquery("paged_groups")).where(
        exp.and_(exp.GT(this=exp.column("result_ordinal"), expression=exp.Literal.number(offset)),
                 exp.LTE(this=exp.column("result_ordinal"), expression=exp.Literal.number(offset+page_size)))
    ).order_by("result_ordinal").limit(page_size).sql(dialect="doris")


def retrieve_sales_groups(sql, query, *, query_many=None, page_size=500, page_cap=500,
                          max_rows=100000, max_bytes=64*1024*1024,
                          timeout_seconds=180, consistency_retries=0):
    from .handlers.sales_aggregate import _integer
    if (type(page_cap) is not int or not 1 <= page_cap <= 10000
        or type(page_size) is not int or not 1 <= page_size <= page_cap
        or type(max_rows) is not int or not 1 <= max_rows <= 100000
        or type(max_bytes) is not int or not 1 <= max_bytes <= 64*1024*1024
        or isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int,float))
        or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 180
        or type(consistency_retries) is not int or not 0 <= consistency_retries <= 2):
        raise ValueError("invalid aggregate budget")
    deadline = time.monotonic() + timeout_seconds
    pass_number = 0
    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise SemanticRuntimeError("分批查询超时，未返回完整结果", reason="query_timeout")
        return value
    def read_pass(retain):
        nonlocal pass_number
        pass_number += 1
        offset, expected, byte_count = 0, None, 0
        retained, digest = [], sha256()
        while expected is None or offset < expected:
            remaining()
            # Read controls first; never fan out before the complete row budget
            # is known. Only one bounded wave is retained at a time.
            offsets = [offset] if expected is None or query_many is None else list(range(offset, min(expected, offset+4*page_size), page_size))
            statements = [aggregate_page_sql(sql, position, page_size, page_cap=page_cap) for position in offsets]
            if query_many is None:
                batches = [query(statements[0], purpose="sales aggregate bounded page")]
            else:
                batches = query_many(statements, purpose="sales aggregate bounded page", timeout_seconds=remaining())
            remaining()
            if not isinstance(batches, (list,tuple)) or len(batches) != len(offsets):
                raise _InvalidPage("batch_shape")
            for batch in batches:
                if not isinstance(batch, (list, tuple)):
                    raise _InvalidPage("page_shape")
                if expected is None:
                    expected = _integer(batch[0]["group_count"]) if batch else 0
                    if expected > max_rows:
                        raise SemanticRuntimeError("完整分组超过处理容量，未展示部分结果", reason="result_truncated")
                if len(batch) != min(page_size,expected-offset):
                    raise _InvalidPage("page_length")
                for index,row in enumerate(batch,start=offset+1):
                    if _integer(row["result_ordinal"]) != index:
                        raise _InvalidPage("ordinal_changed")
                    if _integer(row["group_count"]) != expected:
                        raise _InvalidPage("count_changed")
                    encoded = json.dumps(row,ensure_ascii=False,sort_keys=True,separators=(",",":"),default=str).encode()
                    byte_count += len(encoded)
                    if byte_count > max_bytes:
                        raise SemanticRuntimeError("完整分组超过处理容量，未展示部分结果", reason="result_truncated")
                    digest.update(len(encoded).to_bytes(8,"big"))
                    digest.update(encoded)
                    if retain:
                        retained.append(row)
                offset += len(batch)
                remaining()
        return retained,digest.hexdigest(),offset
    try:
        # A live fact table can advance while the two complete reads are in
        # progress. Retry only that race; structural page failures remain
        # immediate failures and no partial read is ever returned.
        for attempt in range(consistency_retries + 1):
            rows,first,count = read_pass(True)
            _,second,second_count = read_pass(False)
            if first == second and count == second_count:
                return rows
            if attempt == consistency_retries:
                raise _InvalidPage("content_changed")
    except (ValueError,TypeError,KeyError,ArithmeticError) as exc:
        failure = str(exc) if isinstance(exc, _InvalidPage) else "invalid_response"
        raise AggregatePageError(failure, pass_number) from exc
