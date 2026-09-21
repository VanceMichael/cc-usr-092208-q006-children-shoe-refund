"""合成数据集：恰好 280 双不合格童鞋，覆盖全部边界场景。

场景分配（合计 280 双，均属受影响批次 B2026A）：

| 场景 | 订单 | 数量 | 处置 |
| --- | --- | --- | --- |
| 合并订单（两个订单号一张物流单） | O-1001/O-1002 | 5 | 已退回，重复支付回调去重 |
| 拒收后重派签收 | O-1003 | 4 | 已退回，重复通知只计一次 |
| 匿名购买 | O-1004 | 6 | 4 退回 + 2 退款无法追回（已穿弃） |
| 召回前售后换货 | O-1005 | 2 | 换货已收回，不通知、不退款 |
| 转赠后受赠人主动报告 | O-1006 | 3 | 已退回，通知受赠人而非购买人 |
| 无法触达 | O-1007 | 5 | 未控制：无法触达 |
| 已触达无回应 | O-1008 | 4 | 未控制：已触达无回应 |
| 转赠且无人报告 | O-1009 | 2 | 未控制：转赠后无人报告 |
| 普通订单 | O-1101..O-1105 | 249 | 已退回 |

数据全部为假名标识，不含真实个人信息。
"""

from __future__ import annotations

from .recall import (
    ConsumerReport,
    Exchange,
    Order,
    OrderLine,
    RecallCase,
    Shipment,
    UncontrolledReason,
)

AFFECTED_BATCH = "B2026A"
RECALL_ID = "RC-2026-0280"

_expected = {
    "sold": 280,
    "returned": 265,
    "exchange_recovered": 2,
    "controlled": 267,
    "refund_only": 2,
    "uncontrolled": 11,
}


class _Minter:
    def __init__(self, batch: str) -> None:
        self._batch = batch
        self._serial: dict[str, int] = {}

    def mint(self, variant_id: str, n: int) -> list[str]:
        start = self._serial.get(variant_id, 0)
        ids = [
            f"{self._batch}-{variant_id}-{i:04d}"
            for i in range(start + 1, start + n + 1)
        ]
        self._serial[variant_id] = start + n
        return ids


def build_case(*, execute: bool = True) -> RecallCase:
    """构造召回案件；execute=True 时直接把各场景推进到处置完成。"""
    m = _Minter(AFFECTED_BATCH)

    # 1. 合并订单：同一购买人两个订单，共用一张物流单 -------------------
    merged_3 = m.mint("V21", 3)
    merged_2 = m.mint("V22", 2)
    o_1001 = Order("O-1001", "B-01", False, (OrderLine("V21", tuple(merged_3)),))
    o_1002 = Order("O-1002", "B-01", False, (OrderLine("V22", tuple(merged_2)),))

    # 2. 拒收后重派 -----------------------------------------------------
    rej_4 = m.mint("V21", 4)
    o_1003 = Order("O-1003", "B-02", False, (OrderLine("V21", tuple(rej_4)),))

    # 3. 匿名购买 -------------------------------------------------------
    anon_6 = m.mint("V22", 6)
    o_1004 = Order("O-1004", "B-03", True, (OrderLine("V22", tuple(anon_6)),))

    # 4. 召回前售后换货 -------------------------------------------------
    exch_2 = m.mint("V21", 2)
    o_1005 = Order("O-1005", "B-04", False, (OrderLine("V21", tuple(exch_2)),))

    # 5. 转赠后受赠人主动报告 -------------------------------------------
    gift_3 = m.mint("V22", 3)
    o_1006 = Order("O-1006", "B-05", False, (OrderLine("V22", tuple(gift_3)),))

    # 6. 无法触达 -------------------------------------------------------
    unreach_5 = m.mint("V21", 5)
    o_1007 = Order("O-1007", "B-06", False, (OrderLine("V21", tuple(unreach_5)),))

    # 7. 已触达无回应 ---------------------------------------------------
    silent_4 = m.mint("V22", 4)
    o_1008 = Order("O-1008", "B-07", False, (OrderLine("V22", tuple(silent_4)),))

    # 8. 转赠且无人报告 -------------------------------------------------
    lost_2 = m.mint("V21", 2)
    o_1009 = Order("O-1009", "B-08", False, (OrderLine("V21", tuple(lost_2)),))

    # 9. 普通订单 249 双 ------------------------------------------------
    bulk_specs = [
        ("O-1101", "V21", 60),
        ("O-1102", "V22", 60),
        ("O-1103", "V21", 60),
        ("O-1104", "V22", 60),
        ("O-1105", "V21", 9),
    ]
    bulk_orders: list[Order] = []
    bulk_units: dict[str, list[str]] = {}
    for idx, (oid, variant, n) in enumerate(bulk_specs):
        units = m.mint(variant, n)
        bulk_units[oid] = units
        bulk_orders.append(
            Order(oid, f"B-{10 + idx}", False, (OrderLine(variant, tuple(units)),))
        )

    shipments = [
        Shipment("S-7001", ("O-1001", "O-1002"), "F-01",
                 (("揽收", ""), ("签收", "")), True),
        Shipment("S-7002", ("O-1003",), "F-02",
                 (("拒收", "收件人外出"), ("重派", ""), ("签收", "")), True),
        Shipment("S-7003", ("O-1004",), "F-03", (("签收", ""),), True),
        Shipment("S-7004", ("O-1005",), "F-04", (("签收", ""),), True),
        Shipment("S-7005", ("O-1006",), "F-05", (("签收", ""),), True),
        Shipment("S-7006", ("O-1007",), "F-06",
                 (("签收", "驿站代收"),), True),
        Shipment("S-7007", ("O-1008",), "F-07", (("签收", ""),), True),
        Shipment("S-7008", ("O-1009",), "F-08", (("签收", ""),), True),
        *[
            Shipment(f"S-7{10 + idx:02d}", (oid,), f"F-{10 + idx}",
                     (("签收", ""),), True)
            for idx, (oid, _v, _n) in enumerate(bulk_specs)
        ],
    ]

    exchanges = [
        # 调给消费者的替换品来自合格批次 B2026C，不在 280 双之内
        Exchange(
            "X-0001", "O-1005",
            tuple(exch_2),
            ("B2026C-V30-0001", "B2026C-V30-0002"),
        )
    ]
    reports = [
        ConsumerReport(
            "R-0001", "O-1006", "D-09", tuple(gift_3), transferred=True
        )
    ]

    case = RecallCase(
        RECALL_ID,
        [AFFECTED_BATCH],
        [o_1001, o_1002, o_1003, o_1004, o_1005, o_1006, o_1007, o_1008, o_1009,
         *bulk_orders],
        shipments,
        exchanges,
        reports,
    )

    if execute:
        notice_id = _execute(case, merged_3, merged_2, rej_4, anon_6, gift_3,
                             unreach_5, silent_4, lost_2, bulk_units)
        case.demo_notice_id = notice_id  # 供消费者自助核验演示
    return case


def _execute(
    case: RecallCase,
    merged_3: list[str],
    merged_2: list[str],
    rej_4: list[str],
    anon_6: list[str],
    gift_3: list[str],
    unreach_5: list[str],
    silent_4: list[str],
    lost_2: list[str],
    bulk_units: dict[str, list[str]],
) -> None:
    case.settle_pre_recall_exchanges()
    targets = case.notice_targets()

    def notify(recipient: str, channel: str, *, delivered: bool, repeat: bool = False):
        notice = case.issue_notice(recipient, targets[recipient], channel)
        case.mark_notice_delivered(notice.notice_id, delivered)
        if repeat:
            # 重复通知：返回同一条通知，attempts 增加但计数不变
            again = case.issue_notice(recipient, targets[recipient], channel)
            assert again is notice and again.attempts == 2
        return notice

    # 合并订单 → 合并通知；退款回调重复到达
    n01 = notify("F-01", "短信", delivered=True)
    case.record_return("RT-0001", "O-1001", frozenset(merged_3) | frozenset(merged_2))
    assert case.apply_refund_callback("CB-0001", "O-1001", merged_3, 300)
    assert case.apply_refund_callback("CB-0002", "O-1002", merged_2, 200)
    assert not case.apply_refund_callback("CB-0002", "O-1002", merged_2, 200)

    # 拒收重派：重复发送通知验证幂等
    notify("F-02", "短信", delivered=True, repeat=True)
    case.record_return("RT-0002", "O-1003", rej_4)
    assert case.apply_refund_callback("CB-0003", "O-1003", rej_4, 400)

    # 匿名购买：只能站内信触达；部分实物无法追回，先退款并注明原因
    notify("F-03", "平台站内信", delivered=True)
    case.record_return("RT-0003", "O-1004", anon_6[:4])
    case.mark_refund_only("O-1004", anon_6[4:], "消费者反馈两双已穿旧丢弃，无实物可退")
    assert case.apply_refund_callback("CB-0004", "O-1004", anon_6, 600)

    # 转赠：通知发给受赠人
    notify("D-09", "短信", delivered=True)
    case.record_return("RT-0004", "O-1006", gift_3)
    assert case.apply_refund_callback("CB-0005", "O-1006", gift_3, 300)

    # 无法触达：通知未送达
    notify("F-06", "短信", delivered=False)
    case.mark_uncontrolled(unreach_5, UncontrolledReason.UNREACHABLE,
                           "电话停机、驿站代收信息失效，两轮触达失败")

    # 已触达无回应
    notify("F-07", "短信", delivered=True)
    case.mark_uncontrolled(silent_4, UncontrolledReason.NO_RESPONSE,
                           "通知送达后 30 日宽限期满无回应")

    # 转赠无人报告：通知购买家庭，回复鞋已转赠
    notify("F-08", "短信", delivered=True)
    case.mark_uncontrolled(lost_2, UncontrolledReason.TRANSFERRED_NO_REPORT,
                           "购买人称已转赠，受赠人未主动报告，去向不明")

    # 普通订单
    for idx, oid in enumerate(["O-1101", "O-1102", "O-1103", "O-1104", "O-1105"]):
        units = bulk_units[oid]
        notify(f"F-{10 + idx}", "短信", delivered=True)
        case.record_return(f"RT-{1001 + idx}", oid, units)
        assert case.apply_refund_callback(
            f"CB-{1001 + idx}", oid, units, len(units) * 100
        )

    # 通知校验码留给测试与消费者自助核验使用
    return n01.notice_id
