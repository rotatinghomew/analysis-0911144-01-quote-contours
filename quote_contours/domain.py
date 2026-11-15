from decimal import Decimal

from .common import digest, finite, require, table, unique
from .ledger import net_positions, validate_ledger

CURRENCIES = ('USD', 'EUR', 'CNY')


def _validate_fx(fx, currencies, name):
    require(isinstance(fx, dict), f'{name} 汇率必须按币种声明')
    require(set(currencies) <= set(fx), f'{name} 缺少合同币种汇率')
    for currency, rate in fx.items():
        require(isinstance(currency, str) and currency, f'{name} 币种标识不能为空')
        require(finite(rate, f'{name} {currency}') > 0, f'{name} 汇率必须为正')


def validate_contract(contract):
    require(isinstance(contract, dict), '合同必须是对象')
    require(contract['currency'] in CURRENCIES, '结算币种不支持')
    require(contract['base_currency'] == 'CNY', '当前核算本币为 CNY')
    for field in ('price', 'unit_cost', 'freight_floor', 'freight_unit'):
        finite(contract[field], field, 0)
    require(bool(contract['annual_rebates']), '年度返利档位不能为空')
    thresholds = [row['net_units'] for row in contract['annual_rebates']]
    require(thresholds[0] == 0 and thresholds == sorted(set(thresholds)), '年度返利档位错误')
    for row in contract['annual_rebates']:
        require(type(row['net_units']) is int and row['net_units'] >= 0, '年度数量门槛错误')
        require(0 <= finite(row['rate'], 'rebate_rate') < 1, '年度返利比例错误')
    tiers = contract['tiers']
    require(bool(tiers) and tiers[0]['minimum'] == 0, '首档必须从零开始')
    require([tier['minimum'] for tier in tiers] == sorted(set(tier['minimum'] for tier in tiers)), '档位下界必须严格递增')
    for tier in tiers:
        require(type(tier['minimum']) is int and tier['minimum'] >= 0, '档位数量必须为非负整数')
        require(0 <= finite(tier['discount'], 'discount') < 1, '折扣范围错误')


def _validate_month_path(months, currencies, name):
    require(isinstance(months, list), f'{name} 必须是数组')
    seen = set()
    for row in months:
        require(isinstance(row, dict), f'{name} 的行必须是对象')
        require(type(row.get('month')) is int and 1 <= row['month'] <= 12, f'{name} 月份错误')
        require(row['month'] not in seen, f'{name} 月份重复')
        seen.add(row['month'])
        require(type(row.get('quantity')) is int and row['quantity'] >= 0, f'{name} 数量必须为非负整数')
        _validate_fx(row.get('fx'), currencies, f'{name}')
        finite(row.get('freight_multiplier'), f'{name} freight_multiplier', 0)
        for field in ('price', 'unit_cost', 'freight_floor', 'freight_unit'):
            if field in row:
                finite(row[field], f'{name} {field}', 0)
        if 'currency' in row:
            require(row['currency'] in CURRENCIES, f'{name} 结算币种不支持')


def validate_scenario(scenario, currencies):
    require(isinstance(scenario, dict), '情景必须是对象')
    require(type(scenario['quantity']) is int and scenario['quantity'] >= 0, '销量必须为非负整数')
    _validate_fx(scenario['fx'], currencies, '情景')
    finite(scenario['freight_multiplier'], 'freight_multiplier', 0)
    if 'months' in scenario:
        _validate_month_path(scenario['months'], currencies, f"情景 {scenario['id']} months")
    if 'return_rate' in scenario:
        require(0 <= finite(scenario['return_rate'], 'return_rate') < 1, '退单率必须在 [0,1) 内')
    if 'return_month' in scenario:
        require(type(scenario['return_month']) is int and 1 <= scenario['return_month'] <= 12, '退单月份错误')


def _validate_position_terms(row):
    for field in ('price', 'unit_cost', 'freight_floor', 'freight_unit', 'freight_multiplier'):
        if field in row:
            finite(row[field], f'履约记录 {field}', 0)
    if 'currency' in row:
        require(row['currency'] in CURRENCIES, '履约记录结算币种不支持')
    if 'fx_rate' in row:
        require(finite(row['fx_rate'], '履约记录 fx_rate') > 0, '履约记录汇率必须为正')
    if 'fx' in row:
        value = row['fx']
        if isinstance(value, dict):
            require(bool(value), '履约记录汇率映射不能为空')
            for rate in value.values():
                require(finite(rate, '履约记录汇率') > 0, '履约记录汇率必须为正')
        else:
            require(finite(value, '履约记录汇率') > 0, '履约记录汇率必须为正')


def contract_digest(contract):
    return digest(contract)


def scenario_digest(scenario):
    return digest(scenario)


def validate(data):
    contracts = table(data, 'contracts')
    scenarios = table(data, 'scenarios')
    unique(contracts)
    unique(scenarios)
    validate_ledger(data)
    currencies = {contract['currency'] for contract in contracts}
    for contract in contracts:
        validate_contract(contract)
    for scenario in scenarios:
        validate_scenario(scenario, currencies)
    for row in data['positions']:
        _validate_position_terms(row)


def quote(contract, scenario):
    convert = lambda value: Decimal(str(value))
    quantity = scenario['quantity']
    tier = max((tier for tier in contract['tiers'] if tier['minimum'] <= quantity), key=lambda tier: tier['minimum'])
    revenue = convert(contract['price']) * quantity * (1 - convert(tier['discount'])) * convert(scenario['fx'][contract['currency']])
    freight = max(convert(contract['freight_floor']), quantity * convert(contract['freight_unit'])) * convert(scenario['freight_multiplier'])
    cost = convert(contract['unit_cost']) * quantity
    margin = revenue - cost - freight
    return {'contract': contract['id'], 'scenario': scenario['id'], 'tier': tier['minimum'], 'revenue': str(revenue), 'cost': str(cost), 'freight': str(freight), 'margin': str(margin), 'margin_rate': None if revenue == 0 else float(margin / revenue)}


def records(data):
    return data['contracts']


def summarize(data):
    return {'net_shipped_units': net_positions(data), 'basis': '单单报价预览不含年度追补', 'contracts': len(data['contracts']), 'scenarios': len(data['scenarios']), 'quotes': [quote(contract, scenario) for contract in data['contracts'] for scenario in data['scenarios']]}
