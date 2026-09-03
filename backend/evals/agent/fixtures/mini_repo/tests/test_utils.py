"""utils 的单元测试（夹具）。其中 test_discount_sign 故意写错预期值，供调试场景修复。"""

from src.utils import calc_discount, total_with_tax


def test_discount_sign():
    # 故意写错：calc_discount 当前实现是 price*(1+discount)，0.2 应让利 20%
    # 正确期望应为 80.0，这里故意写成 120.0 制造失败
    assert calc_discount(100.0, 0.2) == 120.0


def test_total_with_tax():
    assert total_with_tax(100.0, 0.0) == 106.0
