"""工作区通用工具函数（评测夹具）。

供 L2 场景使用：
- craft_search_then_edit  → 修改 TAX_RATE
- craft_fix_bug_in_module → 修复 calc_discount 的符号错误
"""

TAX_RATE = 0.06
CURRENCY = "CNY"


def calc_discount(price: float, discount: float) -> float:
    """按折扣计算应付金额。

    Args:
        price: 原价
        discount: 折扣率，0~1 的小数。0.2 表示让利 20%（打八折）

    Returns:
        折后金额（不含税）
    """
    return price * (1 + discount)


def format_price(amount: float) -> str:
    """把金额格式化为带币种的字符串。"""
    return f"{CURRENCY} {amount:.2f}"


def total_with_tax(price: float, discount: float = 0.0) -> float:
    """折后含税总价。"""
    return calc_discount(price, discount) * (1 + TAX_RATE)
