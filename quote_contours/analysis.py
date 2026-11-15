"""Negotiation-oriented margin scenario analysis on a pinned data revision.

The analysis keeps three levels of claim apart:

* ``exact`` — one declared scenario for one contract, fully reconciled to the
  amount composition returned by :func:`quote_contours.annual.account`;
* ``feasible_regions`` — an exact integer enumeration of the planned annual
  volume along *that scenario's* declared monthly shape, with the company
  margin floor applied.  Every point keeps the month's FX and freight
  conditions together;
* ``risk`` — a description of what this finite set of declared scenarios
  shows.  It is an observed set, not a probability, not a confidence interval,
  and never a worst case assembled from conditions that do not co-occur.

Changing volume, FX / settlement currency or freight conditions is expressed
through scenario ``overrides``.  An override is applied consistently to the
whole declared path (all months) and is echoed in the result; the analysis
never combines an overridden FX from one scenario with freight from another.
"""

import copy
from decimal import Decimal

from .annual import account, snapshot
from .common import decimal, digest, finite, number, require
from .domain import contract_digest, scenario_digest


EPSILON = Decimal('1e-9')


def _floor(options):
    floor = options.get('margin_floor')
    require(floor is not None, '缺少公司毛利率底线 margin_floor')
    require(0 <= finite(floor, 'margin_floor') < 1, 'margin_floor 必须在 [0,1) 内')
    return number(floor, 'margin_floor')


def _declared_total(scenario):
    if 'path_total' in scenario:
        return scenario['path_total']
    months = scenario.get('months')
    return scenario['quantity'] if months is None else sum(row['quantity'] for row in months)


def _search(options, scenario):
    search = options.get('search') or {}
    if not isinstance(search, dict):
        raise ValueError('search 必须是对象')
    declared = _declared_total(scenario)
    low = search.get('min_path_total', 0)
    high = search.get('max_path_total')
    if high is None:
        high = max(declared * 3, declared + 500)
    require(type(low) is int and low >= 0, 'search.min_path_total 必须为非负整数')
    require(type(high) is int and high >= low, 'search.max_path_total 不合法')
    require(high - low <= 20000, '枚举范围过大，请缩小 search 或分批分析')
    return low, high, declared


def _rate_of(result):
    return None if result['margin_rate'] is None else number(result['margin_rate'], 'margin_rate')


def _validate_month_row(row, scenario, fx, multiplier):
    require(type(row.get('quantity')) is int and row['quantity'] >= 0, 'months 覆盖数量必须为非负整数')
    require(isinstance(fx, dict), 'months 覆盖汇率必须按币种声明')
    for currency in scenario['fx']:
        require(currency in fx, 'months 覆盖缺少情景币种汇率')
        require(finite(fx[currency], 'months 覆盖汇率') > 0, 'months 覆盖汇率必须为正')
    require(finite(multiplier, 'months 覆盖 freight_multiplier', 0) >= 0, 'months 覆盖 freight_multiplier 不能为负')


def _apply_override(contract, scenario, override):
    """Apply one declared what-if consistently to a whole scenario path.

    Returns copies; the pinned input data is never mutated.  The override is
    intentionally scenario-wide: a month's FX and freight multiplier stay
    linked, and no cross-scenario mixture is created.
    """
    if not override:
        return contract, scenario, {}
    require(isinstance(override, dict), 'overrides 的每个条目必须是对象')
    contract = copy.deepcopy(contract)
    scenario = copy.deepcopy(scenario)
    applied = {}
    if 'currency' in override:
        currency = override['currency']
        require(isinstance(currency, str), 'currency 覆盖必须是字符串')
        if currency == contract['base_currency'] and currency not in scenario['fx']:
            # Settlement in the accounting base currency is the identity
            # conversion; no market FX is needed and the implied 1.0 is echoed.
            scenario['fx'] = dict(scenario['fx'], **{currency: 1.0})
            for row in scenario.get('months', []):
                if currency not in row['fx']:
                    row['fx'] = dict(row['fx'], **{currency: 1.0})
            applied['implied_fx'] = {currency: 1.0}
        require(currency in scenario['fx'], f'currency 覆盖的币种未在情景中声明汇率: {currency}')
        contract['currency'] = currency
        applied['currency'] = currency
    fx = override.get('fx')
    if fx is not None:
        require(isinstance(fx, dict) and fx, 'fx 覆盖必须是非空映射')
        for currency, rate_value in fx.items():
            require(currency in scenario['fx'], f'fx 覆盖的币种未在情景中声明: {currency}')
            require(finite(rate_value, 'fx 覆盖') > 0, 'fx 覆盖必须为正')
        scenario['fx'] = dict(scenario['fx'], **fx)
        for row in scenario.get('months', []):
            relevant = {currency: rate_value for currency, rate_value in fx.items() if currency in row['fx']}
            row['fx'] = dict(row['fx'], **relevant)
        applied['fx'] = fx
    multiplier = override.get('freight_multiplier')
    if multiplier is not None:
        require(finite(multiplier, 'freight_multiplier 覆盖', 0) >= 0, 'freight_multiplier 覆盖不能为负')
        scenario['freight_multiplier'] = multiplier
        for row in scenario.get('months', []):
            row['freight_multiplier'] = multiplier
        applied['freight_multiplier'] = multiplier
    for field in ('freight_floor', 'freight_unit'):
        if field in override:
            value = override[field]
            require(finite(value, f'{field} 覆盖', 0) >= 0, f'{field} 覆盖不能为负')
            contract[field] = value
            applied[field] = value
    if 'return_rate' in override:
        require(0 <= finite(override['return_rate'], 'return_rate 覆盖') < 1, 'return_rate 覆盖必须在 [0,1) 内')
        scenario['return_rate'] = override['return_rate']
        applied['return_rate'] = override['return_rate']
    if 'return_month' in override:
        require(type(override['return_month']) is int and 1 <= override['return_month'] <= 12, 'return_month 覆盖错误')
        scenario['return_month'] = override['return_month']
        applied['return_month'] = override['return_month']
    if 'quantity' in override and 'months' in override:
        raise ValueError('quantity 与 months 覆盖不能同时使用，请只声明一种数量调整方式')
    if 'quantity' in override:
        require(type(override['quantity']) is int and override['quantity'] >= 0, 'quantity 覆盖必须为非负整数')
        scenario['quantity'] = override['quantity']
        if scenario.get('months') is not None:
            # A total-volume what-if keeps the declared monthly shape and
            # rescales it; the monthly FX / freight conditions stay attached.
            scenario['path_total'] = override['quantity']
        applied['quantity'] = override['quantity']
    if 'months' in override:
        months = override['months']
        require(isinstance(months, list) and months, 'months 覆盖必须是非空数组')
        base = {row['month']: row for row in scenario.get('months', [])}
        declared = list(base) if base else [row.get('month') for row in months]
        updated = []
        for row in months:
            require(isinstance(row, dict), 'months 覆盖的行必须是对象')
            require(type(row.get('month')) is int and 1 <= row['month'] <= 12, 'months 覆盖月份错误')
            template = base.get(row['month'], {})
            fx = row.get('fx', template.get('fx', scenario['fx']))
            multiplier = row.get('freight_multiplier', template.get('freight_multiplier', scenario['freight_multiplier']))
            _validate_month_row(row, scenario, fx, multiplier)
            effective = {'month': row['month'], 'quantity': row['quantity'], 'fx': fx, 'freight_multiplier': multiplier}
            updated.append(effective)
            if row['month'] not in base:
                declared.append(row['month'])
        require(len({row['month'] for row in updated}) == len(updated), 'months 覆盖月份重复')
        scenario['months'] = sorted(updated, key=lambda item: item['month'])
        require(not base or set(row['month'] for row in updated) == set(base), 'months 覆盖必须覆盖原有的全部月份')
        applied['months'] = [{'month': row['month'], 'quantity': row['quantity']} for row in scenario['months']]
    unknown = set(override) - {'currency', 'fx', 'freight_multiplier', 'freight_floor', 'freight_unit',
                               'return_rate', 'return_month', 'quantity', 'months'}
    require(not unknown, f'未知的覆盖字段: {sorted(unknown)}')
    return contract, scenario, applied


def _effective(contract, scenario, options, contract_id, scenario_id):
    """Resolve the scenario-wide override and an optional per-contract refinement.

    ``overrides`` is keyed by scenario id.  Its value is either a flat override
    applied to every contract in that scenario, or an object with a
    ``contracts`` map whose entries refine it for one contract.  Unknown keys
    are rejected instead of silently ignored.
    """
    overrides = options.get('overrides') or {}
    require(isinstance(overrides, dict), 'overrides 必须是对象')
    entry = overrides.get(scenario_id, {})
    require(isinstance(entry, dict), 'overrides 的情景条目必须是对象')
    shared = {key: value for key, value in entry.items() if key != 'contracts'}
    per_contract = entry.get('contracts', {})
    require(isinstance(per_contract, dict), 'overrides.contracts 必须是对象')
    specific = per_contract.get(contract_id, {})
    require(isinstance(specific, dict), 'overrides.contracts 的条目必须是对象')
    merged = dict(shared)
    merged.update(specific)
    return _apply_override(contract, scenario, merged)


def _exact(contract, scenario, data, options):
    return account(contract, scenario, data, options)


def _probe(contract, scenario, data, options, total):
    return snapshot(contract, scenario, data, dict(options, path_total=total))


def _point(result, feasible):
    """Compact observation used by the enumeration.

    Only the fields needed for interval, boundary and cliff analysis are kept;
    the full exact account remains available in ``exact``.  This keeps a
    20,000-point enumeration bounded in memory.
    """
    orders = result['orders']
    return {
        'net_units': result['units']['net'],
        'margin': result['margin'],
        'margin_rate': result['margin_rate'],
        'feasible': feasible,
        'signature': {'rebate_rate': result['rebate_rate'],
                      'discount_basis': result['discount_basis'],
                      'annual_discount_rate': result['discount_rate'],
                      'discount_rates': {key: value['discount_final_rate'] for key, value in orders.items()},
                      'freight_floor_orders': {key: value['freight_floor_binds'] for key, value in orders.items()}},
        'orders': orders,
    }


def _explain(previous, current):
    """Name the clause that changed between two adjacent exact points.

    Only orders present at both points are compared for a tier change; a newly
    created monthly delivery is reported as such, so a freight floor on a tiny
    extra shipment is not mislabelled as a discount-tier jump.
    """
    before_sig = previous['signature']
    after_sig = current['signature']
    triggers = []
    if before_sig['rebate_rate'] != after_sig['rebate_rate']:
        triggers.append({'clause': 'annual_rebate', 'threshold_units': current['net_units'],
                         'rate_before': before_sig['rebate_rate'], 'rate_after': after_sig['rebate_rate'],
                         'note': '全年净数量跨过返利档位；返利率作用于全年折后净销售额，不是本月独立返利。'})
    common = sorted(set(before_sig['discount_rates']) & set(after_sig['discount_rates']))
    if after_sig['discount_basis'] == 'annual':
        if before_sig['annual_discount_rate'] != after_sig['annual_discount_rate']:
            triggers.append({'clause': 'quantity_discount', 'basis': 'annual',
                             'threshold_units': current['net_units'],
                             'rate_before': before_sig['annual_discount_rate'],
                             'rate_after': after_sig['annual_discount_rate'],
                             'note': '全年净数量跨过折扣档位；年末按全年整单统一适用，不能把各月当独立订单。'})
    else:
        for order_id in common:
            if before_sig['discount_rates'][order_id] != after_sig['discount_rates'][order_id]:
                triggers.append({'clause': 'quantity_discount', 'order': order_id,
                                 'rate_before': before_sig['discount_rates'][order_id],
                                 'rate_after': after_sig['discount_rates'][order_id],
                                 'note': '该次交付的整单数量跨过折扣档位；折扣作用于整单销售额，不能把分段合同平滑成一条直线。'})
    new_orders = sorted(set(after_sig['discount_rates']) - set(before_sig['discount_rates']))
    for order_id in new_orders:
        order = current['orders'][order_id]
        triggers.append({'clause': 'new_delivery_freight_floor' if order['freight_floor_binds'] else 'new_delivery',
                         'order': order_id, 'shipped_units': order['shipped_units'],
                         'discount_rate': order['discount_final_rate'], 'freight': order['freight'],
                         'note': '新增一次交付单独适用最低收费；小批量交付可能因 freight_floor 而使总毛利下降。'
                                 if order['freight_floor_binds'] else '新增一次交付，按该单整单数量选择折扣档位。'})
    common_floor = [order_id for order_id in common
                    if before_sig['freight_floor_orders'][order_id] != after_sig['freight_floor_orders'][order_id]]
    if common_floor:
        triggers.append({'clause': 'freight_minimum', 'orders': common_floor,
                         'note': '交付数量跨过最低收费与按量运费的交点，运费由 floor 转为按量。'})
    if not triggers:
        triggers.append({'clause': 'continuous_ratio',
                         'note': '相邻整单量没有档位跳变；临界点由价格、成本、汇率与运费比例连续移动产生。'})
    return triggers


def _region(contract, scenario, data, options, floor, applied):
    """Enumerate total forecast-year path units for one joint scenario.

    The declared monthly quantities define the shape; each probe rescales that
    shape to the enumerated total, so a month's FX / freight conditions remain
    attached to that month.  Recorded positions are always included, so the
    committed annual units are recorded net units plus the enumerated total.
    """
    low, high, declared = _search(options, scenario)
    baseline_result = _exact(contract, scenario, data, dict(options, path_total=declared))
    points = {}
    feasible = []
    cliffs = []
    rate_dips = []
    previous = None
    for total in range(low, high + 1):
        result = _probe(contract, scenario, data, options, total)
        value = _rate_of(result)
        is_feasible = value is not None and value >= floor
        if is_feasible:
            feasible.append(total)
        point = _point(result, is_feasible)
        if previous is not None and decimal(point['margin'], 'margin') < decimal(previous['margin'], 'margin') - EPSILON:
            triggers = _explain(previous, point)
            discrete = any(trigger['clause'] != 'continuous_ratio' for trigger in triggers)
            entry = {
                'path_total': total,
                'committed_net_units': point['net_units'],
                'margin_before': previous['margin'],
                'margin_after': point['margin'],
                'margin_rate_before': previous['margin_rate'],
                'margin_rate_after': point['margin_rate'],
                'margin_change': str(decimal(point['margin'], 'margin') - decimal(previous['margin'], 'margin')),
                'triggers': triggers,
                'note': '多卖反而少赚：档位或最低收费跳变使毛利绝对额下降，不是线性边际收益。',
            }
            (cliffs if discrete else rate_dips).append(entry)
        points[total] = point
        previous = point

    intervals = []
    if feasible:
        start = last = feasible[0]
        for total in feasible[1:]:
            if total != last + 1:
                intervals.append([start, last])
                start = total
            last = total
        intervals.append([start, last])

    transitions = []
    for total in range(low + 1, high + 1):
        before, after = points[total - 1], points[total]
        if before['feasible'] == after['feasible']:
            continue
        transitions.append({
            'path_total': total,
            'committed_net_units': after['net_units'],
            'direction': 'enter_feasible' if after['feasible'] else 'leave_feasible',
            'margin_rate_before': before['margin_rate'],
            'margin_rate_after': after['margin_rate'],
            'margin_before': before['margin'],
            'margin_after': after['margin'],
            'triggers': _explain(before, after),
        })
    return {
        'contract': contract['id'],
        'contract_digest': contract_digest(contract),
        'scenario': scenario['id'],
        'scenario_digest': scenario_digest(scenario),
        'applied_overrides': applied,
        'axis': 'total_forecast_year_units (情景计划年度总量，按声明月份比例分配；已记录履约另行叠加)',
        'baseline': {'path_total': declared, 'net_units': baseline_result['units']['net'],
                     'margin': baseline_result['margin'], 'margin_rate': baseline_result['margin_rate'],
                     'feasible': _rate_of(baseline_result) is not None and _rate_of(baseline_result) >= floor},
        'feasible': bool(feasible),
        'intervals': intervals,
        'min_path_total': feasible[0] if feasible else None,
        'max_path_total': feasible[-1] if feasible else None,
        'min_committed_net_units': points[feasible[0]]['net_units'] if feasible else None,
        'max_committed_net_units': points[feasible[-1]]['net_units'] if feasible else None,
        'cliffs': cliffs,
        'rate_dips': rate_dips,
        'transitions': transitions,
        'method': {
            'type': 'exact_integer_enumeration',
            'range': [low, high],
            'declared_path_total': declared,
            'step': 1,
            'floor': float(floor),
            'joint_conditions': '每个枚举点保持该情景声明的每月汇率、运费与退单条件联动',
            'shared_path': True,
            'claim': '精确核算的区域结论；只覆盖枚举范围与声明情景。',
        },
        '_feasible_totals': feasible,
    }


def _intersections(regions):
    by_contract = {}
    for region in regions:
        if region.get('_feasible_totals') is None:
            continue
        totals = set(region['_feasible_totals'])
        by_contract.setdefault(region['contract'], []).append((region['scenario'], totals))
    result = []
    for contract_id, entries in by_contract.items():
        common = None
        for _, totals in entries:
            common = totals if common is None else common & totals
        ordered = sorted(common or [])
        intervals = []
        if ordered:
            start = previous = ordered[0]
            for total in ordered[1:]:
                if total != previous + 1:
                    intervals.append([start, previous])
                    start = total
                previous = total
            intervals.append([start, previous])
        result.append({
            'contract': contract_id,
            'scenarios': [scenario_id for scenario_id, _ in entries],
            'feasible': bool(ordered),
            'intervals': intervals,
            'min_path_total': ordered[0] if ordered else None,
            'max_path_total': ordered[-1] if ordered else None,
            'scope': '只在这些已声明情景的交集内；不外推到未声明情景。',
        })
    return result


def _compare(contracts, scenarios, data, options, floor):
    cells = []
    for scenario in scenarios:
        effective_contracts = []
        for contract in contracts:
            effective_contract, effective_scenario, applied = _effective(
                contract, scenario, options, contract['id'], scenario['id'])
            effective_contracts.append((effective_contract, effective_scenario, applied))
        group = []
        for effective_contract, effective_scenario, applied in effective_contracts:
            result = _exact(effective_contract, effective_scenario, data, options)
            value = _rate_of(result)
            group.append({
                'contract': effective_contract['id'],
                'scenario': effective_scenario['id'],
                'applied_overrides': applied,
                'margin': result['margin'],
                'margin_rate': result['margin_rate'],
                'feasible': value is not None and value >= floor,
                'discount_basis': result['discount_basis'],
                'rebate_rate': result['rebate']['rate'],
                'freight': result['freight'],
                'net_revenue': result['sales']['net_revenue'],
                'composition': result['composition'],
                'contract_digest': contract_digest(effective_contract),
                'scenario_digest': scenario_digest(effective_scenario),
            })
        best = max(decimal(row['margin'], 'margin') for row in group)
        for row in group:
            row['margin_shortfall_vs_best'] = str(best - decimal(row['margin'], 'margin'))
            row['best_in_scenario'] = decimal(row['margin'], 'margin') == best
        cells.extend(group)
    per_scenario = {}
    wins = {contract['id']: 0 for contract in contracts}
    for scenario in scenarios:
        group = [row for row in cells if row['scenario'] == scenario['id']]
        best = max(decimal(row['margin'], 'margin') for row in group)
        winners = [row['contract'] for row in group if decimal(row['margin'], 'margin') == best]
        per_scenario[scenario['id']] = winners
        for identifier in winners:
            wins[identifier] += 1
    return {
        'cells': cells,
        'per_scenario_best_margin': per_scenario,
        'scenario_wins': wins,
        'note': '只比较同一组已声明情景下的取舍；margin_shortfall_vs_best 是同一情景内的机会差，'
                '不是概率，也不把不同情景拼成最坏情况。',
    }


def _risk(exact, floor):
    observations = []
    by_contract = {}
    for row in exact:
        value = _rate_of(row['account'])
        feasible = value is not None and value >= floor
        observations.append({'contract': row['contract'], 'scenario': row['scenario'],
                             'margin': row['account']['margin'], 'margin_rate': row['account']['margin_rate'],
                             'feasible': feasible})
        by_contract.setdefault(row['contract'], []).append((value, row['scenario']))
    ranges = []
    for contract_id, values in by_contract.items():
        known = [(value, scenario_id) for value, scenario_id in values if value is not None]
        if not known:
            ranges.append({'contract': contract_id, 'min_margin_rate': None, 'max_margin_rate': None,
                           'worst_observed_scenario': None, 'best_observed_scenario': None})
            continue
        worst = min(known)
        best = max(known)
        ranges.append({'contract': contract_id,
                       'min_margin_rate': float(worst[0]), 'worst_observed_scenario': worst[1],
                       'max_margin_rate': float(best[0]), 'best_observed_scenario': best[1]})
    return {
        'scope': 'finite_declared_scenarios',
        'floor': float(floor),
        'observations': observations,
        'observed_ranges': ranges,
        'not_supported': ['概率', '置信区间', '未声明情景的插值', '跨情景拼接的最坏情况'],
        'note': '只描述所选有限情景中的观察结果。未观测的汇率、成本或运费条件不插值、不拼接；'
                '不能据此对总体概率或情景外的承诺下结论。',
    }


def analyse(data, options):
    """Build the negotiation analysis for a pinned snapshot.

    ``data`` is expected to come from ``Repository.get(revision)``.  The
    returned ``data_digest`` equals the repository digest, and every exact /
    comparison row carries the contract and scenario digests, so a result can
    still be traced after the contract is revised.
    """
    require(isinstance(data, dict), '资料必须是对象')
    require(isinstance(options, dict), 'options 必须是对象')
    unknown_options = set(options) - {'margin_floor', 'contracts', 'scenarios', 'search', 'overrides'}
    require(not unknown_options, f'不支持的 options 字段: {sorted(unknown_options)}')
    floor = _floor(options)
    require(isinstance(data.get('contracts'), list) and data['contracts'], '资料缺少合同')
    require(isinstance(data.get('scenarios'), list) and data['scenarios'], '资料缺少情景')
    contract_ids = options['contracts'] if 'contracts' in options else [contract['id'] for contract in data['contracts']]
    scenario_ids = options['scenarios'] if 'scenarios' in options else [scenario['id'] for scenario in data['scenarios']]
    require(isinstance(contract_ids, list) and contract_ids, 'contracts 必须是非空合同标识数组')
    require(isinstance(scenario_ids, list) and scenario_ids, 'scenarios 必须是非空情景标识数组')
    require(all(isinstance(key, str) for key in contract_ids), 'contracts 必须是合同标识数组')
    require(all(isinstance(key, str) for key in scenario_ids), 'scenarios 必须是情景标识数组')
    contracts = {contract['id']: contract for contract in data['contracts']}
    scenarios = {scenario['id']: scenario for scenario in data['scenarios']}
    unknown = [key for key in contract_ids if key not in contracts] + [key for key in scenario_ids if key not in scenarios]
    require(not unknown, f'合同或情景不存在: {unknown}')
    chosen_contracts = [contracts[key] for key in contract_ids]
    chosen_scenarios = [scenarios[key] for key in scenario_ids]

    exact = []
    for scenario in chosen_scenarios:
        for contract in chosen_contracts:
            effective_contract, effective_scenario, applied = _effective(
                contract, scenario, options, contract['id'], scenario['id'])
            result = _exact(effective_contract, effective_scenario, data, options)
            exact.append({
                'contract': contract['id'], 'scenario': scenario['id'],
                'contract_digest': contract_digest(effective_contract),
                'scenario_digest': scenario_digest(effective_scenario),
                'applied_overrides': applied,
                'account': result,
                'feasible_at_floor': _rate_of(result) is not None and _rate_of(result) >= floor,
            })

    regions = []
    for scenario in chosen_scenarios:
        for contract in chosen_contracts:
            effective_contract, effective_scenario, applied = _effective(
                contract, scenario, options, contract['id'], scenario['id'])
            region = _region(effective_contract, effective_scenario, data, options, floor, applied)
            region['contract'] = contract['id']
            region['scenario'] = scenario['id']
            regions.append(region)
    intersections = _intersections(regions)
    for region in regions:
        region.pop('_feasible_totals', None)

    comparison = _compare(chosen_contracts, chosen_scenarios, data, options, floor)
    return {
        'data_digest': digest(data),
        'result_digest': digest({'options': options, 'data_digest': digest(data)}),
        'contracts': [{'id': contract['id'], 'digest': contract_digest(contract)} for contract in chosen_contracts],
        'scenarios': [{'id': scenario['id'], 'digest': scenario_digest(scenario)} for scenario in chosen_scenarios],
        'margin_floor': float(floor),
        'options_echo': {'contracts': list(contract_ids), 'scenarios': list(scenario_ids),
                         'search': dict(options.get('search') or {}),
                         'overrides': options.get('overrides') or {}},
        'exact': exact,
        'feasible_regions': regions,
        'region_intersections': intersections,
        'comparison': comparison,
        'risk': _risk(exact, floor),
        'assumptions': [
            '单个情景的金额来自年度精确核算，composition 可逐项对账到 margin。',
            '可行区域在声明的联合情景内对情景年度计划总量做精确整数枚举，保持每月汇率与运费联动。',
            '风险只描述已声明有限情景内的观察，不构成概率、置信区间或跨情景最坏情况。',
            '情景中的月度路径是共同履约声明，不是可任意组合的独立样本。',
            '已记录履约与情景月度路径合并为同一年度路径；年度返利与折扣不可按各月拆开重算。',
            '覆盖项 (overrides) 一致应用于整个情景路径，并已回显在 applied_overrides。',
        ],
    }
