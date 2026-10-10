"""Compose independently checked order and return facts without joining lines."""

from dataclasses import dataclass
from fractions import Fraction

from .order_components import OrderComponentResult, retrieve_order_components
from .return_components import ReturnComponentPlan, ReturnComponentResult, retrieve_return_components
from .semantic_types import SemanticRuntimeError


@dataclass(frozen=True)
class OrderReturnResult:
    groups: list[dict]
    orders: list[dict]
    currency_totals: dict


def compose_order_returns(sales, returns, *, amount_mode='pending'):
    if amount_mode not in ('pending', 'proportional'):
        raise ValueError('explicit supported amount basis required')
    if not isinstance(sales, OrderComponentResult) or not isinstance(returns, ReturnComponentResult):
        raise ValueError('independently reconciled components required')
    try:
        return _compose(sales, returns, amount_mode)
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        raise SemanticRuntimeError('销售与退货范围或分量不一致，暂未展示', reason='order_return_composition_failed') from exc


def _compose(sales, returns, amount_mode):
    def index(rows, keys):
        result = {}
        for row in rows:
            key = tuple(row[name] for name in keys)
            if key in result or any(not isinstance(value, str) or not value for value in key):
                raise ValueError('duplicate or invalid component key')
            result[key] = row
        return result

    orders = index(sales.orders, ('cargo_owner', 'sales_order_code'))
    sold = index(sales.groups, ('cargo_owner', 'sales_order_code', 'sku_code'))
    returned = index(returns.groups, ('cargo_owner', 'sales_order_code', 'sku_code'))
    if any(key[:2] not in orders for key in set(sold) | set(returned)):
        raise ValueError('component outside exact sales order cohort')
    if sum((Fraction(row['returned_qty']) for row in returned.values()), Fraction(0)) != returns.total_quantity:
        raise ValueError('return component sum changed')
    groups, currency_totals = [], {}
    for key in sorted(set(sold) | set(returned)):
        sale, ret, order = sold.get(key), returned.get(key), orders[key[:2]]
        issues = []
        shipped = sale['order_ship_qty'] if sale else None
        quantity = ret['returned_qty'] if ret else 0
        if (shipped is not None and Fraction(shipped).denominator != 1) or Fraction(quantity).denominator != 1:
            raise ValueError('noninteger source quantity')
        net = int(shipped) - int(quantity) if shipped is not None else None
        if sale and (sale['currency_code'], sale['order_created']) != (order['currency_code'], order['order_created']):
            raise ValueError('sales header attributes changed')
        if not sale: issues.append('missing_sales_sku')
        if sale and sale['negative_ship_rows']: issues.append('negative_sales_source')
        if ret and ret['negative_detail_count']: issues.append('negative_return_source')
        if shipped is not None and quantity > shipped: issues.append('returns_exceed_shipment')
        original = sale['detail_payment'] if sale else None
        currency = order['currency_code']
        paid, deducted = None, None
        if amount_mode == 'pending':
            issues.append('amount_choice_pending')
        elif sale:
            if original is None: issues.append('missing_detail_payment')
            if currency is None: issues.append('missing_currency')
            if shipped < 0: issues.append('negative_shipment_payment_unresolved')
            if original is not None and currency is not None and shipped >= 0:
                if shipped == 0:
                    paid = Fraction(original)
                    issues.append('zero_shipment_kept_payment')
                else:
                    paid = Fraction(original) * net / Fraction(shipped)
                deducted = Fraction(original) - paid
        groups.append(dict(cargo_owner=key[0], sales_order_code=key[1], sku_code=key[2],
            currency_code=currency, order_ship_qty=shipped, returned_qty=quantity, net_qty=net,
            original_detail_payment=original, proportional_payment=paid, proportional_deduction=deducted,
            return_document_count=ret['return_document_count'] if ret else 0,
            return_detail_count=ret['return_detail_count'] if ret else 0,
            sales_detail_count=sale['source_rows'] if sale else 0, issues=tuple(issues)))
        total = currency_totals.setdefault(currency, dict(known_proportional_payment=Fraction(0),
            known_proportional_deduction=Fraction(0), unavailable_payment_groups=0))
        if paid is None:
            total['unavailable_payment_groups'] += 1
        else:
            total['known_proportional_payment'] += paid
            total['known_proportional_deduction'] += deducted
    return OrderReturnResult(groups, [dict(orders[key]) for key in sorted(orders)], currency_totals)


def retrieve_order_returns(plan, query, *, amount_mode='pending', query_many=None, **budgets):
    if amount_mode not in ('pending', 'proportional'):
        raise ValueError('explicit supported amount basis required')
    sales = retrieve_order_components(plan, query, query_many=query_many, **budgets)
    return_plan = ReturnComponentPlan(owner_codes=plan.owner_codes, order_codes=plan.order_codes,
                                     sku_codes=plan.sku_codes, order_keys=sales.order_keys)
    if not sales.orders:
        # An empty selected cohort is NONE, never an unscoped retry.
        return compose_order_returns(sales, ReturnComponentResult([], 0, 0, 0, None), amount_mode=amount_mode)
    returns = retrieve_return_components(return_plan, query, query_many=query_many, **budgets)
    repeated_sales = retrieve_order_components(plan, query, query_many=query_many, **budgets)
    repeated_returns = retrieve_return_components(return_plan, query, query_many=query_many, **budgets)
    if repeated_sales != sales or repeated_returns != returns:
        raise SemanticRuntimeError('查询期间销售或退货来源发生变化，暂未展示', reason='order_return_sources_changed')
    return compose_order_returns(sales, returns, amount_mode=amount_mode)
