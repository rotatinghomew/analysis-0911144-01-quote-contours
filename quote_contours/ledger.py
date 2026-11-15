from .common import require, table, unique


def validate_ledger(data):
    positions = table(data, 'positions')
    unique(positions)
    contracts = {row['id'] for row in data['contracts']}
    orders = {}
    returned = {}
    # Events are ordered by (month, record order).  The declared list order is
    # the tie-breaker for same-month events and is preserved by the repository.
    for row in positions:
        require(row['contract'] in contracts, '履约合同不存在')
        require(type(row['month']) is int and 1 <= row['month'] <= 12, '履约月份错误')
        require(type(row['quantity']) is int and row['quantity'] > 0, '履约数量必须为正整数')
        require(row['kind'] in ('ship', 'return'), '履约事件类型错误')
        if row['kind'] == 'ship':
            orders[row['id']] = row
        else:
            original = orders.get(row['original'])
            require(original is not None, '退单需要先前交付记录')
            require(original['contract'] == row['contract'] and original['month'] <= row['month'], '退单与原交付不一致')
            returned[row['original']] = returned.get(row['original'], 0) + row['quantity']
            require(returned[row['original']] <= original['quantity'], '累计退单超过原交付')


def net_positions(data):
    totals = {row['id']: 0 for row in data['contracts']}
    for row in data['positions']:
        totals[row['contract']] += row['quantity'] * (1 if row['kind'] == 'ship' else -1)
    return totals
