"""Exact annual accounting for one contract along a declared fulfilment path.

The model keeps the contract mechanisms separate and auditable:

* quantity discount tiers choose one rate from full-year net units by default:
  the annual commitment is the "整单", so months are never treated as
  independent orders.  A contract that prices per delivery may declare
  ``discount_basis: "shipment"``; the basis actually used is reported.
* every delivery pays ``max(freight_floor, quantity * freight_unit) *
  freight_multiplier``; the floor is per delivery and is not refunded when
  units come back.
* the annual rebate picks one tier from full-year net units and applies its
  rate to full-year discounted base-currency revenue.  It is never computed
  independently month by month.
* a return is reversed at the original delivery's transaction price (after the
  discount that delivery was booked at) and the original exchange rate.  If the
  return pushes the order (or the year, for annual basis) into a lower discount
  tier, the over-granted discount is trued up once; the return month's FX is
  never used.

Revenue identity, per order::

    gross shipped - provisional discount - return reversal + discount true-up
        = net revenue

The margin composition published by :func:`account` is::

    gross shipped - provisional discount - return reversal + discount true-up
      - cost - freight - provisional rebate + rebate true-up = margin

where ``provisional rebate - rebate true-up`` equals the final annual rebate.
Every line is published as an exact decimal string, so the composition adds up
to ``margin`` without display rounding.
"""

from decimal import Decimal, ROUND_HALF_UP

from .common import money, number, rate as as_rate, require


ZERO = Decimal(0)


def _tier(contract, units):
    return max((row for row in contract['tiers'] if row['minimum'] <= units), key=lambda row: row['minimum'])


def _rebate(contract, units):
    return max((row for row in contract['annual_rebates'] if row['net_units'] <= units), key=lambda row: row['net_units'])


def allocate(total, weights):
    """Allocate an integer total across month weights (largest remainder).

    Deterministic, and preserves the declared monthly shape: it does not turn a
    front-loaded or back-loaded path into a constant rate.
    """
    require(type(total) is int and total >= 0, '总数量必须为非负整数')
    require(bool(weights), '月度路径不能为空')
    weight_total = sum(weights)
    if weight_total == 0:
        result = [0] * len(weights)
        result[0] = total
        return result
    raw = [Decimal(total) * Decimal(weight) / Decimal(weight_total) for weight in weights]
    result = [int(value) for value in raw]
    remainder = total - sum(result)
    order = sorted(range(len(weights)), key=lambda index: (-(raw[index] - result[index]), index))
    for index in order[:remainder]:
        result[index] += 1
    return result


def _return_quantity(rate_value, units):
    value = number(rate_value, 'return_rate')
    rounded = int((value * Decimal(units)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    return min(max(rounded, 0), units)


def _resolved(contract, row, scenario, month_map, assumptions):
    """Resolve the terms a recorded event was actually booked at.

    A record may carry its own price / currency / FX / cost.  When it does not,
    the contract's cost version and the scenario's jointly declared monthly FX
    are used; every fallback is recorded in ``assumptions`` and in the order's
    ``fx_source`` / ``price_source`` so a reader can see exactly what was used.
    """
    currency = row.get('currency', contract['currency'])
    price = row.get('price', contract['price'])
    unit_cost = row.get('unit_cost', contract['unit_cost'])
    freight_floor = row.get('freight_floor', contract['freight_floor'])
    freight_unit = row.get('freight_unit', contract['freight_unit'])
    month_entry = month_map.get(row['month'])
    if 'fx_rate' in row:
        fx_rate = row['fx_rate']
        fx_source = 'recorded fx_rate'
    elif isinstance(row.get('fx'), dict):
        require(currency in row['fx'], f"履约记录 {row['id']} 缺少 {currency} 汇率")
        fx_rate = row['fx'][currency]
        fx_source = 'recorded fx mapping'
    elif month_entry is not None:
        require(currency in month_entry['fx'], f"履约记录 {row['id']} 的月份缺少 {currency} 汇率")
        fx_rate = month_entry['fx'][currency]
        fx_source = f"scenario {scenario['id']} month {row['month']} fx"
    else:
        require(currency in scenario['fx'], f"履约记录 {row['id']} 缺少 {currency} 汇率")
        fx_rate = scenario['fx'][currency]
        fx_source = f"scenario {scenario['id']} fx"
    if 'fx' in row and not isinstance(row['fx'], dict):
        require(row['fx'] == fx_rate, f"履约记录 {row['id']} 的汇率字段不一致")
    if 'freight_multiplier' in row:
        multiplier = row['freight_multiplier']
        freight_source = 'recorded freight_multiplier'
    elif month_entry is not None:
        multiplier = month_entry['freight_multiplier']
        freight_source = f"scenario {scenario['id']} month {row['month']} freight_multiplier"
    else:
        multiplier = scenario['freight_multiplier']
        freight_source = f"scenario {scenario['id']} freight_multiplier"
    if 'price' not in row:
        assumptions.add('部分已记录交付未声明成交单价，按当前合同价格与成本版本折算。')
    if 'fx_rate' not in row and not isinstance(row.get('fx'), dict):
        assumptions.add(f"已记录交付 {row['id']} 未声明成交汇率，按情景 {scenario['id']} 的联合汇率折算。")
    return {
        'price': price, 'currency': currency, 'fx_rate': fx_rate, 'unit_cost': unit_cost,
        'freight_floor': freight_floor, 'freight_unit': freight_unit,
        'freight_multiplier': multiplier,
        'fx_source': fx_source, 'freight_source': freight_source,
        'price_source': 'recorded price' if 'price' in row else 'contract price',
        'cost_source': 'recorded unit_cost' if 'unit_cost' in row else 'contract unit_cost',
    }


def _order(contract, identifier, month, units, terms, source):
    return {
        'id': identifier, 'month': month, 'source': source, 'shipped_units': units,
        'returns': [], 'price': terms['price'], 'currency': terms['currency'], 'fx_rate': terms['fx_rate'],
        'unit_cost': terms['unit_cost'], 'freight_floor': terms['freight_floor'],
        'freight_unit': terms['freight_unit'], 'freight_multiplier': terms['freight_multiplier'],
        'fx_source': terms['fx_source'], 'freight_source': terms['freight_source'],
        'price_source': terms['price_source'], 'cost_source': terms['cost_source'],
    }


def build_orders(contract, scenario, data, options, assumptions):
    include_recorded = options.get('include_recorded', True)
    month_map = {row['month']: row for row in scenario.get('months', [])}
    orders = {}
    recorded_ship_months = set()
    for row in data['positions']:
        if row['contract'] != contract['id']:
            continue
        if row['kind'] == 'ship':
            recorded_ship_months.add(row['month'])
            if not include_recorded:
                continue
            terms = _resolved(contract, row, scenario, month_map, assumptions)
            orders[row['id']] = _order(contract, row['id'], row['month'], row['quantity'], terms, 'recorded')
    if include_recorded:
        for row in data['positions']:
            if row['contract'] != contract['id'] or row['kind'] != 'return':
                continue
            original = orders.get(row['original'])
            require(original is not None, f"退单 {row['id']} 的原交付未纳入本次路径")
            original['returns'].append({'id': row['id'], 'month': row['month'], 'quantity': row['quantity']})

    months = scenario.get('months')
    synthesized = months is None
    if synthesized:
        months = [{'month': 12, 'quantity': scenario['quantity'], 'fx': scenario['fx'],
                   'freight_multiplier': scenario['freight_multiplier']}]
        assumptions.add('情景未声明 months，按第 12 月一次交付的情景数量核算。')
    path_total = options.get('path_total', scenario.get('path_total'))
    if path_total is None:
        quantities = [row['quantity'] for row in months]
    else:
        require(type(path_total) is int and path_total >= 0, '路径总数量必须为非负整数')
        quantities = allocate(path_total, [row['quantity'] for row in months])
        assumptions.add('本次分析按各月声明数量的比例重分配路径总量，保持每月汇率与运费条件联动，不做跨情景拼接。')
    overlap = sorted(recorded_ship_months & {row['month'] for row in months})
    require(not (include_recorded and overlap), f"预测月份与已记录履约月份重叠: {overlap}")
    return_rate = options.get('return_rate', scenario.get('return_rate', 0))
    return_month = options.get('return_month', scenario.get('return_month', 12))
    require(type(return_month) is int and 1 <= return_month <= 12, '退单月份错误')
    forecast = []
    for row, quantity in zip(months, quantities):
        if quantity == 0:
            continue
        identifier = f"forecast-{scenario['id']}-{row['month']}"
        currency = row.get('currency', contract['currency'])
        require(currency in row['fx'], f"情景 {scenario['id']} 第 {row['month']} 月缺少 {currency} 汇率")
        terms = {
            'price': row.get('price', contract['price']), 'currency': currency,
            'fx_rate': row['fx'][currency], 'unit_cost': row.get('unit_cost', contract['unit_cost']),
            'freight_floor': row.get('freight_floor', contract['freight_floor']),
            'freight_unit': row.get('freight_unit', contract['freight_unit']),
            'freight_multiplier': row['freight_multiplier'],
            'fx_source': f"scenario {scenario['id']} month {row['month']} fx",
            'freight_source': f"scenario {scenario['id']} month {row['month']} freight_multiplier",
            'price_source': 'month price' if 'price' in row else 'contract price',
            'cost_source': 'month unit_cost' if 'unit_cost' in row else 'contract unit_cost',
        }
        order = _order(contract, identifier, row['month'], quantity, terms, 'forecast')
        returned = _return_quantity(return_rate, quantity)
        if returned:
            order['returns'].append({'id': f"forecast-return-{scenario['id']}-{row['month']}",
                                     'month': max(row['month'], return_month), 'quantity': returned})
        forecast.append(order)
        orders[identifier] = order
    if return_rate:
        assumptions.add(f"预测交付按 return_rate={return_rate} 生成退单；退单月份为 max(交付月, {return_month})，按原成交价与原汇率冲回。")
    return {'orders': list(orders.values()), 'synthesized_months': synthesized,
            'path_total': sum(quantities), 'return_rate': return_rate, 'return_month': return_month}


def _prepare(contract, scenario, data, options):
    assumptions = set()
    built = build_orders(contract, scenario, data, options, assumptions)
    orders = built['orders']
    for order in orders:
        order['returned_units'] = sum(row['quantity'] for row in order['returns'])
        order['net_units'] = order['shipped_units'] - order['returned_units']
        require(order['returned_units'] <= order['shipped_units'], f"退单超过交付: {order['id']}")
        order['gross_shipped'] = number(order['price'], 'price') * order['shipped_units'] * number(order['fx_rate'], 'fx_rate')
        order['cost'] = number(order['unit_cost'], 'unit_cost') * order['net_units']
        order['freight'] = max(number(order['freight_floor'], 'freight_floor'),
                               order['shipped_units'] * number(order['freight_unit'], 'freight_unit')) \
            * number(order['freight_multiplier'], 'freight_multiplier')
        order['freight_floor_binds'] = number(order['freight_floor'], 'freight_floor') >= \
            order['shipped_units'] * number(order['freight_unit'], 'freight_unit')
        # >= marks the floor as the binding term; at an exact tie both terms are
        # equal, so the flag is only a descriptive label, never an arithmetic
        # switch.

    # This analysis is for annual commitment quotes: the whole year's net units
    # are the "整单", so tier selection must not treat each month as a separate
    # order.  A contract that prices per delivery can declare
    # ``discount_basis: "shipment"``; whichever basis is used is reported.
    basis = contract.get('discount_basis', 'annual')
    require(basis in ('annual', 'shipment'), 'discount_basis 只能是 annual 或 shipment')
    shipped_units = sum(order['shipped_units'] for order in orders)
    returned_units = sum(order['returned_units'] for order in orders)
    net_units = shipped_units - returned_units
    if basis == 'shipment':
        for order in orders:
            order['discount_original_rate'] = _tier(contract, order['shipped_units'])['discount']
            order['discount_final_rate'] = _tier(contract, order['net_units'])['discount']
    else:
        original_rate = _tier(contract, shipped_units)['discount']
        final_rate = _tier(contract, net_units)['discount']
        for order in orders:
            order['discount_original_rate'] = original_rate
            order['discount_final_rate'] = final_rate

    for order in orders:
        gross = number(order['price'], 'price') * order['shipped_units'] * number(order['fx_rate'], 'fx_rate')
        net_gross = number(order['price'], 'price') * order['net_units'] * number(order['fx_rate'], 'fx_rate')
        original_rate = number(order['discount_original_rate'], 'discount')
        final_rate = number(order['discount_final_rate'], 'discount')
        order['provisional_discount'] = gross * original_rate
        order['return_reversal'] = number(order['price'], 'price') * order['returned_units'] * number(order['fx_rate'], 'fx_rate') * (Decimal(1) - original_rate)
        order['discount_true_up'] = net_gross * (original_rate - final_rate)
        order['net_revenue'] = gross - order['provisional_discount'] - order['return_reversal'] + order['discount_true_up']
        require(order['net_revenue'] == net_gross * (Decimal(1) - final_rate), '单笔收入分解不平')

    gross_shipped = sum((order['gross_shipped'] for order in orders), ZERO)
    provisional_discount = sum((order['provisional_discount'] for order in orders), ZERO)
    return_reversal = sum((order['return_reversal'] for order in orders), ZERO)
    discount_true_up = sum((order['discount_true_up'] for order in orders), ZERO)
    net_revenue = sum((order['net_revenue'] for order in orders), ZERO)
    cost = sum((order['cost'] for order in orders), ZERO)
    freight = sum((order['freight'] for order in orders), ZERO)
    rebate_row = _rebate(contract, net_units)
    rebate = net_revenue * number(rebate_row['rate'], 'rebate_rate')
    provisional_rebate_row = _rebate(contract, shipped_units)
    provisional_rebate_base = gross_shipped - provisional_discount
    provisional_rebate = provisional_rebate_base * number(provisional_rebate_row['rate'], 'rebate_rate')
    margin = net_revenue - cost - freight - rebate
    return {
        'orders': orders, 'basis': basis, 'shipped_units': shipped_units, 'returned_units': returned_units,
        'net_units': net_units, 'gross_shipped': gross_shipped, 'provisional_discount': provisional_discount,
        'return_reversal': return_reversal, 'discount_true_up': discount_true_up, 'net_revenue': net_revenue,
        'cost': cost, 'freight': freight, 'rebate_row': rebate_row, 'rebate': rebate,
        'provisional_rebate_row': provisional_rebate_row, 'provisional_rebate': provisional_rebate,
        'margin': margin, 'built': built, 'assumptions': assumptions,
    }


def _allocate_share(order, key, total_net_revenue, total):
    if total_net_revenue == 0:
        return ZERO
    return order['net_revenue'] / total_net_revenue * total


def snapshot(contract, scenario, data, options=None):
    """Return only the enumeration-critical figures for a path.

    Used by the feasible-region search; it computes the same exact model as
    :func:`account` but skips the presentation layers (order details, path
    rows, monthly flows) so a large integer scan stays bounded.
    """
    options = dict(options or {})
    model = _prepare(contract, scenario, data, options)
    net_revenue = model['net_revenue']
    return {
        'units': {'shipped': model['shipped_units'], 'returned': model['returned_units'],
                  'net': model['net_units']},
        'margin': money(model['margin']),
        'margin_rate': None if net_revenue == 0 else as_rate(model['margin'] / net_revenue),
        'freight': money(model['freight']),
        'discount_basis': model['basis'],
        'discount_rate': as_rate(_tier(contract, model['net_units'])['discount']) if model['basis'] == 'annual' else None,
        'rebate_rate': as_rate(model['rebate_row']['rate']),
        'orders': {order['id']: {'shipped_units': order['shipped_units'],
                                 'discount_final_rate': order['discount_final_rate'],
                                 'freight_floor_binds': order['freight_floor_binds'],
                                 'freight': money(order['freight'])}
                   for order in model['orders']},
    }


def account(contract, scenario, data, options=None, assumptions=None):
    """Return an exact full-year account for one contract and one scenario path."""
    options = dict(options or {})
    model = _prepare(contract, scenario, data, options)
    orders = model['orders']
    net_revenue = model['net_revenue']
    rebate = model['rebate']
    margin = model['margin']
    basis = model['basis']
    assumptions = set(model['assumptions']) if assumptions is None else assumptions | model['assumptions']

    rebate_true_up = model['provisional_rebate'] - rebate
    expected = model['gross_shipped'] - model['provisional_discount'] - model['return_reversal'] \
        + model['discount_true_up'] - model['cost'] - model['freight'] - model['provisional_rebate'] + rebate_true_up
    composition = [
        {'key': 'gross_shipped_revenue', 'label': '已交付销售额（原价、原汇率）', 'amount': money(model['gross_shipped'])},
        {'key': 'provisional_quantity_discount', 'label': '交付时数量折扣', 'amount': money(-model['provisional_discount'])},
        {'key': 'returns_reversed', 'label': '退单按原成交价、原汇率冲回', 'amount': money(-model['return_reversal'])},
        {'key': 'discount_true_up', 'label': '退单后折扣档位追补（收入影响）', 'amount': money(model['discount_true_up'])},
        {'key': 'net_revenue', 'label': '折后净销售额', 'amount': money(net_revenue)},
        {'key': 'cost', 'label': '销货成本（原成本版本）', 'amount': money(-model['cost'])},
        {'key': 'freight', 'label': '运费（每次交付最低收费）', 'amount': money(-model['freight'])},
        {'key': 'provisional_rebate', 'label': '未计退单时的年度返利', 'amount': money(-model['provisional_rebate'])},
        {'key': 'rebate_true_up', 'label': '退单后返利追补（正=收回先前多计，负=补付）', 'amount': money(rebate_true_up)},
        {'key': 'margin', 'label': '毛利', 'amount': money(margin)},
    ]
    checks = {
        'net_units_reconcile': model['net_units'] == model['shipped_units'] - model['returned_units'],
        'returns_use_original_terms': all(order['fx_source'] and order['price_source'] for order in orders),
        'composition_reconciles': expected == margin,
        'revenue_reconciles': model['gross_shipped'] - model['provisional_discount'] - model['return_reversal'] + model['discount_true_up'] == net_revenue,
        'order_revenue_reconciles': all(order['net_revenue'] == (number(order['price'], 'price') * order['net_units'] * number(order['fx_rate'], 'fx_rate')) * (Decimal(1) - number(order['discount_final_rate'], 'discount')) for order in orders),
        'order_returns_within_delivery': all(order['returned_units'] <= order['shipped_units'] for order in orders),
        'rebate_true_up_reconciles': rebate == model['provisional_rebate'] - rebate_true_up,
        'rebate_uses_net_units': model['rebate_row']['net_units'] <= model['net_units'] < _next_rebate_threshold(contract, model['net_units']),
    }
    require(all(checks.values()), '年度核算校验失败')

    order_details = []
    for order in sorted(orders, key=lambda item: (item['month'], item['id'])):
        order_details.append({
            'id': order['id'], 'month': order['month'], 'source': order['source'],
            'shipped_units': order['shipped_units'], 'returned_units': order['returned_units'],
            'net_units': order['net_units'], 'price': money(order['price']), 'currency': order['currency'],
            'fx_rate': money(order['fx_rate']), 'unit_cost': money(order['unit_cost']),
            'freight_multiplier': money(order['freight_multiplier']),
            'discount_original_rate': as_rate(order['discount_original_rate']),
            'discount_final_rate': as_rate(order['discount_final_rate']),
            'gross_shipped': money(order['gross_shipped']), 'provisional_discount': money(order['provisional_discount']),
            'return_reversal': money(order['return_reversal']), 'discount_true_up': money(order['discount_true_up']),
            'net_revenue': money(order['net_revenue']), 'cost': money(order['cost']), 'freight': money(order['freight']),
            'imputed_rebate': money(_allocate_share(order, 'rebate', net_revenue, rebate)),
            'margin': money(order['net_revenue'] - order['cost'] - order['freight'] - _allocate_share(order, 'rebate', net_revenue, rebate)),
            'freight_floor_binds': order['freight_floor_binds'],
            'fx_source': order['fx_source'], 'price_source': order['price_source'], 'cost_source': order['cost_source'],
            'freight_source': order['freight_source'],
            'returns': [{'id': row['id'], 'month': row['month'], 'quantity': row['quantity']} for row in sorted(order['returns'], key=lambda item: (item['month'], item['id']))],
        })

    currency_flows = {}
    for order in orders:
        flow = currency_flows.setdefault(order['currency'], {'local_revenue': ZERO, 'base_revenue': ZERO, 'net_units': 0})
        flow['local_revenue'] += number(order['price'], 'price') * order['net_units'] * (Decimal(1) - number(order['discount_final_rate'], 'discount'))
        flow['base_revenue'] += order['net_revenue']
        flow['net_units'] += order['net_units']
    for flow in currency_flows.values():
        local = flow['local_revenue']
        base = flow['base_revenue']
        flow['local_revenue'] = money(local)
        flow['base_revenue'] = money(base)
        flow['weighted_fx'] = None if local == 0 else money(base / local)

    result = {
        'contract': contract['id'],
        'scenario': scenario['id'],
        'discount_basis': basis,
        'units': {'shipped': model['shipped_units'], 'returned': model['returned_units'], 'net': model['net_units']},
        'sales': {
            'gross_shipped': money(model['gross_shipped']), 'provisional_discount': money(model['provisional_discount']),
            'returns_reversed': money(-model['return_reversal']), 'discount_true_up': money(model['discount_true_up']),
            'net_revenue': money(net_revenue),
        },
        'cost': money(model['cost']),
        'freight': money(model['freight']),
        'discount': {
            'basis': basis, 'provisional_amount': money(model['provisional_discount']),
            'true_up': money(model['discount_true_up']),
            'final_rate': as_rate(_tier(contract, model['net_units'])['discount']) if basis == 'annual' else None,
            'orders_with_tier_change': [order['id'] for order in orders if order['discount_original_rate'] != order['discount_final_rate']],
        },
        'rebate': {
            'net_units': model['net_units'], 'rate': as_rate(model['rebate_row']['rate']),
            'base': money(net_revenue), 'amount': money(rebate),
            'provisional_units': model['shipped_units'], 'provisional_rate': as_rate(model['provisional_rebate_row']['rate']),
            'provisional_base': money(model['gross_shipped'] - model['provisional_discount']), 'provisional_amount': money(model['provisional_rebate']),
            'true_up': money(rebate_true_up),
            'true_up_note': '正值为收回先前按未退数量多计的返利；负值为应补付。',
        },
        'margin': money(margin),
        'margin_rate': None if net_revenue == 0 else as_rate(margin / net_revenue),
        'composition': composition,
        'orders': order_details,
        'currency_flows': currency_flows,
        'path': _path(contract, orders, model, basis),
        'assumptions': sorted(assumptions),
        'checks': checks,
        'path_total': model['built']['path_total'],
    }
    return result


def _next_rebate_threshold(contract, units):
    higher = [row['net_units'] for row in contract['annual_rebates'] if row['net_units'] > units]
    return min(higher) if higher else float('inf')


def _path(contract, orders, model, basis):
    """Monthly delivery flows plus cumulative threshold crossings.

    ``provisional_*`` answers "if the year ended after this month", computed
    from the orders delivered and returned by that month.  It is never an
    independent monthly annual result: the final annual rebate (and, for the
    annual basis, the annual discount) is decided once at year end and appears
    in ``year_end``.
    """
    months = []
    for month in range(1, 13):
        delivered = [order for order in orders if order['month'] == month]
        returned = [(order, row) for order in orders for row in order['returns'] if row['month'] == month]
        if not delivered and not returned:
            continue
        months.append({
            'month': month,
            'delivered_units': sum(order['shipped_units'] for order in delivered),
            'returned_units': sum(row['quantity'] for order, row in returned),
            'gross_shipped': sum((order['gross_shipped'] for order in delivered), ZERO),
            'returns_reversed': sum((number(order['price'], 'price') * row['quantity'] * number(order['fx_rate'], 'fx_rate')
                                     for order, row in returned), ZERO),
            'cost': sum((number(order['unit_cost'], 'unit_cost') * order['shipped_units'] for order in delivered), ZERO)
                    - sum((number(order['unit_cost'], 'unit_cost') * row['quantity'] for order, row in returned), ZERO),
            'freight': sum((order['freight'] for order in delivered), ZERO),
        })

    rows = []
    crossings = []
    cumulative_units = 0
    cumulative_net_gross = ZERO
    cumulative_cost = ZERO
    cumulative_freight = ZERO
    seen_rebate = set()
    seen_discount = set()
    for row in months:
        previous_units = cumulative_units
        cumulative_units += row['delivered_units'] - row['returned_units']
        cumulative_net_gross += row['gross_shipped'] - row['returns_reversed']
        cumulative_cost += row['cost']
        cumulative_freight += row['freight']

        if basis == 'annual':
            for tier in contract['tiers']:
                if previous_units < tier['minimum'] <= cumulative_units and tier['minimum'] not in seen_discount:
                    seen_discount.add(tier['minimum'])
                    crossings.append({'month': row['month'], 'clause': 'quantity_discount', 'basis': basis,
                                      'threshold': tier['minimum'],
                                      'rate_before': _tier(contract, previous_units)['discount'],
                                      'rate_after': tier['discount'],
                                      'note': '全年净数量跨档，年末一次性调整，不是本月独立折扣。'})
        else:
            for order in orders:
                if order['month'] > row['month']:
                    continue
                current_net = order['shipped_units'] - sum(item['quantity'] for item in order['returns'] if item['month'] <= row['month'])
                previous_net = order['shipped_units'] - sum(item['quantity'] for item in order['returns'] if item['month'] < row['month'])
                if _tier(contract, current_net)['minimum'] != _tier(contract, previous_net)['minimum'] \
                        and _tier(contract, current_net)['minimum'] not in seen_discount:
                    threshold = _tier(contract, current_net)['minimum']
                    seen_discount.add((order['id'], threshold))
                    crossings.append({'month': row['month'], 'clause': 'quantity_discount', 'basis': 'shipment',
                                      'order': order['id'], 'threshold': threshold,
                                      'rate_before': _tier(contract, previous_net)['discount'],
                                      'rate_after': _tier(contract, current_net)['discount'],
                                      'note': '退单使原交付的整单数量降档，按原成交条件追补折扣。'})
        for rebate in contract['annual_rebates']:
            if previous_units < rebate['net_units'] <= cumulative_units and rebate['net_units'] not in seen_rebate:
                seen_rebate.add(rebate['net_units'])
                crossings.append({'month': row['month'], 'clause': 'annual_rebate', 'basis': 'annual',
                                  'threshold': rebate['net_units'],
                                  'rate_before': _rebate(contract, previous_units)['rate'],
                                  'rate_after': rebate['rate'],
                                  'note': '返利档位在年末按全年净数量定档，跨档时追补或收回。'})

        if basis == 'annual':
            provisional_rate = _tier(contract, cumulative_units)['discount']
            provisional_discount = cumulative_net_gross * number(provisional_rate, 'discount')
        else:
            provisional_rate = None
            provisional_discount = sum(
                (number(order['price'], 'price') * number(order['fx_rate'], 'fx_rate')
                 * (order['shipped_units'] - sum(item['quantity'] for item in order['returns'] if item['month'] <= row['month']))
                 * number(_tier(contract, order['shipped_units'] - sum(item['quantity'] for item in order['returns'] if item['month'] <= row['month']))['discount'], 'discount')
                 for order in orders if order['month'] <= row['month']), ZERO)
        provisional_net_revenue = cumulative_net_gross - provisional_discount
        provisional_rebate_row = _rebate(contract, cumulative_units)
        provisional_rebate = provisional_net_revenue * number(provisional_rebate_row['rate'], 'rebate_rate')
        rows.append({
            'month': row['month'], 'delivered_units': row['delivered_units'], 'returned_units': row['returned_units'],
            'cumulative_net_units': cumulative_units, 'gross_shipped': money(row['gross_shipped']),
            'returns_reversed': money(row['returns_reversed']), 'cost': money(row['cost']), 'freight': money(row['freight']),
            'provisional_discount_rate': None if provisional_rate is None else as_rate(provisional_rate),
            'provisional_discount': money(provisional_discount),
            'provisional_net_revenue': money(provisional_net_revenue),
            'provisional_rebate_rate': as_rate(provisional_rebate_row['rate']),
            'provisional_rebate': money(provisional_rebate),
            'provisional_margin_if_year_ended': money(provisional_net_revenue - cumulative_cost - cumulative_freight - provisional_rebate),
            'note': 'provisional：年度返利与年度折扣只在年末按全年净数量定档，不能当作各月独立结果。',
        })
    return {
        'rows': rows,
        'threshold_crossings': crossings,
        'year_end': {
            'discount_rate': as_rate(_tier(contract, model['net_units'])['discount']) if basis == 'annual' else None,
            'rebate_rate': as_rate(model['rebate_row']['rate']),
            'net_revenue': money(model['net_revenue']),
            'note': '年末按全年净数量一次性定档；此前月度数值只是路径观察。',
        },
    }
