import hashlib
import json
import math
from decimal import Decimal


def require(condition, message):
    if not condition:
        raise ValueError(message)


def finite(value, name, minimum=None):
    require(not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value), f'{name} 必须是有限数值')
    require(minimum is None or value >= minimum, f'{name} 小于允许范围')
    return value


def unique(rows, field='id'):
    keys = [row[field] for row in rows]
    require(all(isinstance(key, str) and key for key in keys), '标识不能为空')
    require(len(keys) == len(set(keys)), '标识重复')


def table(data, field):
    rows = data[field]
    require(isinstance(rows, list), f'{field} 必须是数组')
    require(all(isinstance(row, dict) for row in rows), f'{field} 的行必须是对象')
    return rows


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def number(value, name):
    """Exact Decimal for a declared business number.

    Business data is declared as JSON numbers.  Converting through ``str`` keeps
    the decimal value the author wrote instead of the binary float approximation,
    which matters at discount and rebate thresholds.
    """
    require(not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value), f'{name} 必须是有限数值')
    return Decimal(str(value))


def decimal(value, name='数值'):
    """Parse an already-computed Decimal, numeric value or decimal string."""
    if isinstance(value, Decimal):
        require(value.is_finite(), f'{name} 必须是有限数值')
        return value
    if isinstance(value, str):
        parse = Decimal(value)
        require(parse.is_finite(), f'{name} 必须是有限数值')
        return parse
    return number(value, name)


def money(value):
    """Exact external representation (no rounding).

    Rounding each line would make the published composition fail to add up to
    the published total at the last digit.  Exact decimal strings keep every
    line traceable and reconcile the composition to the margin.
    """
    value = value if isinstance(value, Decimal) else Decimal(str(value))
    require(value.is_finite(), '金额必须是有限数值')
    return format(value, 'f')


def rate(value):
    return None if value is None else float(value)
