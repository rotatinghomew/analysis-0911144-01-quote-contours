from decimal import Decimal
from .ledger import validate_ledger, net_positions
from .common import finite, require, table, unique


def validate(data):
    contracts = table(data, 'contracts')
    scenarios = table(data, 'scenarios')
    unique(contracts)
    unique(scenarios)
    validate_ledger(data)
    for contract in contracts:
        require(contract['currency'] in ('USD', 'EUR', 'CNY'), '结算币种不支持')
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
    for scenario in scenarios:
        require(type(scenario['quantity']) is int and scenario['quantity'] >= 0, '销量必须为非负整数')
        require(isinstance(scenario['fx'], dict), '汇率必须按币种声明')
        require({contract['currency'] for contract in contracts} <= set(scenario['fx']), '情景缺少合同币种汇率')
        for currency, rate in scenario['fx'].items():
            require(finite(rate, currency) > 0, '汇率必须为正')
        finite(scenario['freight_multiplier'], 'freight_multiplier', 0)


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
