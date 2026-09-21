"""童鞋网售召回的领域模型。

识别链路为：采购批次 → 商品变体 → 订单行 → 物流投递 → 收货确认 / 消费者主动报告。
所有计数以"双"（实物单元 unit）为最小单位，并做三类幂等：

1. 通知幂等：同一接收方与同一组实物单元只产生一条通知，重复触达不重复计数；
2. 退款幂等：支付回调以回调编号去重，重复回调不产生第二次退款；
3. 单元唯一：每双鞋在处置闭环中只能落入一个处置桶。
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable


# ---------------------------------------------------------------------------
# 基础资料
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Unit:
    """一双可追溯的鞋：批次 + 变体 + 实物编号。"""

    unit_id: str
    batch_id: str
    variant_id: str


@dataclass(frozen=True)
class OrderLine:
    variant_id: str
    unit_ids: tuple[str, ...]


@dataclass
class Order:
    order_id: str
    buyer_ref: str  # 平台侧假名标识，不等于真实身份
    anonymous: bool  # 匿名购买：商家不可见身份，平台仍可站内触达
    lines: tuple[OrderLine, ...]
    amount_per_unit: int = 100  # 单价（分），合成数据用整数避免浮点误差


@dataclass
class Shipment:
    """物流单。合并订单的多个订单号指向同一物流单。"""

    shipment_id: str
    order_ids: tuple[str, ...]
    recipient_ref: str  # 收货方标识（家庭/个人的假名）
    events: tuple[tuple[str, str], ...]  # (状态, 说明)，如拒收、重派、签收
    delivered: bool


@dataclass
class Exchange:
    """召回开始前已完成的售后换货：不合格实物已在商家手中。"""

    exchange_id: str
    order_id: str
    returned_unit_ids: tuple[str, ...]  # 换回的不合格鞋
    replacement_unit_ids: tuple[str, ...]  # 调换给消费者的合格品，不属于受影响批次


@dataclass
class ConsumerReport:
    """消费者主动报告，可来自购买人或受赠人。"""

    report_id: str
    order_id: str
    reporter_ref: str
    unit_ids: tuple[str, ...]
    transferred: bool  # 是否转赠后由受赠人报告


# ---------------------------------------------------------------------------
# 召回执行记录
# ---------------------------------------------------------------------------


class Disposition(str, Enum):
    EXCHANGE_RECOVERED = "换货已收回"
    RETURNED = "已退回"
    REFUND_ONLY = "退款无法追回实物"
    UNCONTROLLED = "未控制"


class UncontrolledReason(str, Enum):
    UNREACHABLE = "无法触达"
    NO_RESPONSE = "已触达无回应"
    TRANSFERRED_NO_REPORT = "转赠后无人报告"


@dataclass
class Notice:
    notice_id: str
    recipient_ref: str
    unit_ids: frozenset[str]
    channel: str
    attempts: int = 1
    delivered: bool = False
    verification_code: str = ""


@dataclass
class ReturnRecord:
    return_id: str
    order_id: str
    unit_ids: frozenset[str]
    received: bool = False


@dataclass
class RefundRecord:
    callback_id: str
    order_id: str
    unit_ids: frozenset[str]
    amount: int
    applied: bool = True


@dataclass
class _UnitResolution:
    unit_id: str
    order_id: str
    batch_id: str
    variant_id: str
    disposition: Disposition | None = None
    disposition_detail: str = ""
    refunded: bool = False


# ---------------------------------------------------------------------------
# 召回引擎
# ---------------------------------------------------------------------------
# 通知、退货、退款等执行记录与监管/商家/消费者视图均以本类状态为准。


class RecallCase:
    def __init__(
        self,
        recall_id: str,
        affected_batch_ids: Iterable[str],
        orders: Iterable[Order],
        shipments: Iterable[Shipment],
        exchanges: Iterable[Exchange] = (),
        reports: Iterable[ConsumerReport] = (),
        *,
        secret: str = "demo-secret",
    ) -> None:
        self.recall_id = recall_id
        self.affected_batch_ids = set(affected_batch_ids)
        self.orders = {o.order_id: o for o in orders}
        self.shipments = {s.shipment_id: s for s in shipments}
        self.exchanges = {e.exchange_id: e for e in exchanges}
        self.reports = {r.report_id: r for r in reports}
        self._secret = secret.encode("utf-8")

        self._units: dict[str, _UnitResolution] = {}
        self._notices: dict[tuple[str, frozenset[str]], Notice] = {}
        self._returns: dict[str, ReturnRecord] = {}
        self._refund_callbacks: dict[str, RefundRecord] = {}

        self._index_units()

    # -- 识别链路 ----------------------------------------------------------

    def _index_units(self) -> None:
        """批次 → 变体 → 订单行：把每双鞋落到唯一订单上。

        实物编号约定为 ``批次-变体-序号``，例如 ``B2026A-V21-0007``。
        """
        for order in self.orders.values():
            for line in order.lines:
                for unit_id in line.unit_ids:
                    if unit_id in self._units:
                        raise ValueError(f"实物单元重复挂单: {unit_id}")
                    batch_id, variant_id, _serial = unit_id.rsplit("-", 2)
                    if variant_id != line.variant_id:
                        raise ValueError(f"实物 {unit_id} 与订单行变体 {line.variant_id} 不符")
                    self._units[unit_id] = _UnitResolution(
                        unit_id=unit_id,
                        order_id=order.order_id,
                        batch_id=batch_id,
                        variant_id=variant_id,
                    )

    def affected_units(self) -> set[str]:
        """受影响批次下、已经售出的全部实物单元。"""
        return {
            uid
            for uid, u in self._units.items()
            if u.batch_id in self.affected_batch_ids
        }

    def pre_recall_controlled_units(self) -> set[str]:
        """召回开始前已通过售后换货收回的实物，不再纳入消费者通知。"""
        return {
            uid
            for ex in self.exchanges.values()
            for uid in ex.returned_unit_ids
        }

    def notice_targets(self) -> dict[str, frozenset[str]]:
        """通知目标：接收方 → 应通知的实物单元。

        转赠且有受赠人报告的，通知受赠人；其余通知购买人对应的收货家庭。
        合并订单的多个订单号共用同一收货方，合并为一条通知目标。
        """
        targets: dict[str, set[str]] = {}
        buyer_to_recipient = {
            buyer: shipment.recipient_ref
            for shipment in self.shipments.values()
            for buyer in (self.orders[oid].buyer_ref for oid in shipment.order_ids)
        }
        # 受赠人主动报告覆盖购买人作为联系人
        report_units: dict[str, set[str]] = {}
        for report in self.reports.values():
            if report.transferred:
                report_units.setdefault(report.reporter_ref, set()).update(report.unit_ids)

        exempt = self.pre_recall_controlled_units()
        for uid in self.affected_units() - exempt:
            order_id = self._units[uid].order_id
            buyer = self.orders[order_id].buyer_ref
            recipient = buyer_to_recipient.get(buyer, buyer)
            # 若该双鞋由受赠人主动报告，则触达受赠人
            gifted_to = next(
                (ref for ref, uids in report_units.items() if uid in uids), None
            )
            targets.setdefault(gifted_to or recipient, set()).add(uid)
        return {ref: frozenset(uids) for ref, uids in targets.items()}

    # -- 通知（幂等 + 真伪核验）--------------------------------------------

    def issue_notice(self, recipient_ref: str, unit_ids: Iterable[str], channel: str) -> Notice:
        key = (recipient_ref, frozenset(unit_ids))
        existing = self._notices.get(key)
        if existing is not None:
            existing.attempts += 1  # 重复通知只增加尝试次数
            return existing
        notice_id = f"N-{len(self._notices) + 1:04d}"
        notice = Notice(
            notice_id=notice_id,
            recipient_ref=recipient_ref,
            unit_ids=key[1],
            channel=channel,
            verification_code=self._sign(notice_id, recipient_ref),
        )
        self._notices[key] = notice
        return notice

    def mark_notice_delivered(self, notice_id: str, delivered: bool) -> None:
        notice = self._find_notice(notice_id)
        notice.delivered = delivered

    def _sign(self, notice_id: str, recipient_ref: str) -> str:
        msg = f"{self.recall_id}|{notice_id}|{recipient_ref}".encode("utf-8")
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()[:16]

    def verify_notice(self, code: str, order_id: str) -> dict:
        """消费者凭通知码 + 订单号核验真伪并查看本人处理状态，无需登录。"""
        if order_id not in self.orders:
            return {"valid": False}
        for notice in self._notices.values():
            if any(uid in notice.unit_ids for uid in self._order_units(order_id)):
                if hmac.compare_digest(notice.verification_code, code):
                    return {
                        "valid": True,
                        "recall_id": self.recall_id,
                        "notice_id": notice.notice_id,
                        "status": [
                            self._unit_status(uid)
                            for uid in sorted(notice.unit_ids & self._order_units(order_id))
                        ],
                    }
        return {"valid": False}

    # -- 退货 / 退款 / 无法追回 ---------------------------------------------

    def record_return(self, return_id: str, order_id: str, unit_ids: Iterable[str]) -> ReturnRecord:
        if return_id in self._returns:
            raise ValueError(f"退货单重复登记: {return_id}")
        record = ReturnRecord(return_id, order_id, frozenset(unit_ids), received=True)
        self._returns[return_id] = record
        for uid in record.unit_ids:
            self._set_disposition(uid, Disposition.RETURNED)
        return record

    def apply_refund_callback(
        self, callback_id: str, order_id: str, unit_ids: Iterable[str], amount: int
    ) -> bool:
        """支付回调。同一 callback_id 重复到达时只入账一次，返回 False 表示去重丢弃。"""
        if callback_id in self._refund_callbacks:
            return False
        units = frozenset(unit_ids)
        self._refund_callbacks[callback_id] = RefundRecord(
            callback_id, order_id, units, amount, applied=True
        )
        for uid in units:
            self._units[uid].refunded = True
        return True

    def mark_refund_only(self, order_id: str, unit_ids: Iterable[str], detail: str) -> None:
        """退款但实物无法追回（如已穿着丢弃），必须注明原因。"""
        for uid in unit_ids:
            self._set_disposition(uid, Disposition.REFUND_ONLY, detail)

    def mark_uncontrolled(
        self, unit_ids: Iterable[str], reason: UncontrolledReason, detail: str
    ) -> None:
        """未控制实物必须归类原因，供监管判断残余风险。"""
        for uid in unit_ids:
            self._set_disposition(uid, Disposition.UNCONTROLLED, f"{reason.value}：{detail}")

    def settle_pre_recall_exchanges(self) -> None:
        """召回前换货收回的实物直接标记为已控制，无消费者退款。"""
        for uid in self.pre_recall_controlled_units():
            self._set_disposition(uid, Disposition.EXCHANGE_RECOVERED, "召回前售后换货收回")

    def _set_disposition(self, unit_id: str, disposition: Disposition, detail: str = "") -> None:
        current = self._units[unit_id]
        if current.disposition is not None and current.disposition != disposition:
            raise ValueError(
                f"实物 {unit_id} 处置冲突: {current.disposition.value} → {disposition.value}"
            )
        current.disposition = disposition
        current.disposition_detail = detail

    # -- 视图 ---------------------------------------------------------------

    def _order_units(self, order_id: str) -> frozenset[str]:
        return frozenset(
            uid
            for line in self.orders[order_id].lines
            for uid in line.unit_ids
        ) & self.affected_units()

    def _find_notice(self, notice_id: str) -> Notice:
        for notice in self._notices.values():
            if notice.notice_id == notice_id:
                return notice
        raise KeyError(notice_id)

    def _unit_status(self, uid: str) -> dict:
        u = self._units[uid]
        return {
            "unit_id": uid,
            "variant_id": u.variant_id,
            "disposition": u.disposition.value if u.disposition else "待处理",
            "refunded": u.refunded,
        }

    def merchant_view(self) -> dict:
        """商家视图：只见履约所需信息，不含购买家庭身份。

        退货以平台物流令牌对接；合并订单按退货单去重；匿名订单无身份字段。
        """
        return {
            "recall_id": self.recall_id,
            "returns_to_receive": [
                {
                    "return_id": r.return_id,
                    "logistics_token": f"PK-{self.recall_id}-{r.return_id}",
                    "variant_ids": sorted({self._units[u].variant_id for u in r.unit_ids}),
                    "quantity": len(r.unit_ids),
                }
                for r in self._returns.values()
            ],
            "totals": self._totals(),
        }

    def regulator_view(self) -> dict:
        """监管视图：售出、触达、退回、退款、未控制的可核对关系。"""
        totals = self._totals()
        delivered_notices = [n for n in self._notices.values() if n.delivered]
        notified_units = frozenset().union(*(n.unit_ids for n in delivered_notices)) if delivered_notices else frozenset()
        target_units = frozenset().union(*self.notice_targets().values()) if self.notice_targets() else frozenset()
        return {
            "recall_id": self.recall_id,
            "sold_units": totals["sold"],
            "identified_units": len(self.affected_units()),
            "notice_target_units": len(target_units),
            "notices_issued": len(self._notices),
            "notice_attempts": sum(n.attempts for n in self._notices.values()),
            "notified_units": len(notified_units & self.affected_units()),
            "returned_units": totals["returned"],
            "exchange_recovered_units": totals["exchange_recovered"],
            "controlled_units": totals["controlled"],
            "refunded_units": totals["refunded"],
            "refund_only_units": totals["refund_only"],
            "uncontrolled_units": totals["uncontrolled"],
            "uncontrolled_breakdown": totals["uncontrolled_breakdown"],
            "identities_hold": self.identities_hold(),
            "closure": self.closure_check(),
        }

    def _totals(self) -> dict:
        buckets = {d: [] for d in Disposition}
        for uid in self.affected_units():
            d = self._units[uid].disposition
            buckets[d].append(uid)
        uncontrolled_breakdown: dict[str, int] = {}
        for uid in buckets[Disposition.UNCONTROLLED]:
            reason = self._units[uid].disposition_detail.split("：", 1)[0]
            uncontrolled_breakdown[reason] = uncontrolled_breakdown.get(reason, 0) + 1
        return {
            "sold": len(self.affected_units()),
            "returned": len(buckets[Disposition.RETURNED]),
            "exchange_recovered": len(buckets[Disposition.EXCHANGE_RECOVERED]),
            "controlled": len(buckets[Disposition.RETURNED])
            + len(buckets[Disposition.EXCHANGE_RECOVERED]),
            "refunded": sum(1 for uid in self.affected_units() if self._units[uid].refunded),
            "refund_only": len(buckets[Disposition.REFUND_ONLY]),
            "uncontrolled": len(buckets[Disposition.UNCONTROLLED]),
            "uncontrolled_breakdown": uncontrolled_breakdown,
            "pending": sum(1 for uid in self.affected_units() if self._units[uid].disposition is None),
        }

    # -- 闭环核对 ----------------------------------------------------------

    def identities_hold(self) -> bool:
        """三组恒等关系必须同时成立：

        售出 = 实物控制 + 退款不退货 + 未控制
        退款单元 ⊆ 已退回 ∪ 退款不退货
        通知送达单元 ⊆ 通知目标单元
        """
        t = self._totals()
        sold_ok = t["sold"] == t["controlled"] + t["refund_only"] + t["uncontrolled"]
        refund_ok = all(
            self._units[uid].disposition
            in (Disposition.RETURNED, Disposition.REFUND_ONLY)
            for uid in self.affected_units()
            if self._units[uid].refunded
        )
        delivered = frozenset().union(
            *(n.unit_ids for n in self._notices.values() if n.delivered)
        ) if any(n.delivered for n in self._notices.values()) else frozenset()
        targets = frozenset().union(*self.notice_targets().values()) if self.notice_targets() else frozenset()
        notice_ok = delivered <= targets
        return sold_ok and refund_ok and notice_ok

    def closure_check(self, regulator_accepts_residual: bool = False) -> dict:
        """判断召回能否结束：恒等关系成立、无待处理单元、残余未控制已说明。"""
        t = self._totals()
        residual = [
            {"unit_id": uid, "reason": self._units[uid].disposition_detail}
            for uid in sorted(self.affected_units())
            if self._units[uid].disposition is Disposition.UNCONTROLLED
        ]
        can_close = (
            self.identities_hold()
            and t["pending"] == 0
            and (t["uncontrolled"] == 0 or regulator_accepts_residual)
        )
        return {
            "can_close": can_close,
            "pending_units": t["pending"],
            "residual_uncontrolled": residual,
            "note": "残余未控制为零，或监管人员书面接受已注明原因的残余" if can_close
            else "存在未处置单元或未被接受的未控制残余，召回不能结束",
        }
