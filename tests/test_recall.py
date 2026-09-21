import unittest

from src.recall import (
    Disposition,
    Order,
    OrderLine,
    RecallCase,
    Shipment,
    UncontrolledReason,
)
from src.sample_data import AFFECTED_BATCH, build_case


class SampleTotalsTest(unittest.TestCase):
    def setUp(self):
        self.case = build_case()

    def test_sold_is_exactly_280_pairs(self):
        self.assertEqual(self.case.regulator_view()["sold_units"], 280)

    def test_identity_sold_controlled_refund_uncontrolled(self):
        """售出 = 实物控制(退回+换货收回) + 退款不退货 + 未控制。"""
        t = self.case.regulator_view()
        self.assertEqual(
            t["sold_units"],
            t["controlled_units"] + t["refund_only_units"] + t["uncontrolled_units"],
        )
        self.assertEqual(
            (t["returned_units"], t["exchange_recovered_units"],
             t["refund_only_units"], t["uncontrolled_units"]),
            (265, 2, 2, 11),
        )
        self.assertTrue(self.case.identities_hold())

    def test_uncontrolled_reasons_are_classified(self):
        view = self.case.regulator_view()
        self.assertEqual(
            view["uncontrolled_breakdown"],
            {"无法触达": 5, "已触达无回应": 4, "转赠后无人报告": 2},
        )
        for item in view["closure"]["residual_uncontrolled"]:
            self.assertIn("：", item["reason"])


class NoticeDedupTest(unittest.TestCase):
    def setUp(self):
        self.case = build_case()

    def test_merged_orders_share_single_notice(self):
        """合并订单 O-1001/O-1002 只产生一条通知、一张退货单。"""
        targets = self.case.notice_targets()
        merged = targets["F-01"]
        self.assertEqual(len(merged), 5)
        notices = [n for n in self.case._notices.values() if n.recipient_ref == "F-01"]
        self.assertEqual(len(notices), 1)

    def test_repeated_notice_is_idempotent(self):
        """拒收重派家庭的重复通知只增加尝试次数，不产生新通知。"""
        notices = [n for n in self.case._notices.values() if n.recipient_ref == "F-02"]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].attempts, 2)
        view = self.case.regulator_view()
        self.assertEqual(view["notices_issued"], 12)
        self.assertEqual(view["notice_attempts"], 13)

    def test_exchanged_units_are_not_notified(self):
        """召回前换货收回的 2 双不进入通知目标（278 = 280 - 2）。"""
        self.assertEqual(self.case.regulator_view()["notice_target_units"], 278)


class PaymentDedupTest(unittest.TestCase):
    def test_duplicate_callback_does_not_double_refund(self):
        case = build_case()
        before = case.regulator_view()["refunded_units"]
        accepted = case.apply_refund_callback(
            "CB-0002", "O-1002", ("B2026A-V22-0004",), 200
        )
        self.assertFalse(accepted)
        self.assertEqual(case.regulator_view()["refunded_units"], before)

    def test_each_refunded_unit_has_return_or_refund_only_disposition(self):
        case = build_case()
        for uid in case.affected_units():
            res = case._units[uid]
            if res.refunded:
                self.assertIn(
                    res.disposition,
                    (Disposition.RETURNED, Disposition.REFUND_ONLY),
                    uid,
                )


class TransferAndAnonymousTest(unittest.TestCase):
    def test_gifted_units_notice_donee_via_report(self):
        """转赠后受赠人主动报告：通知发给受赠人 D-09，购买人不收到该 3 双的通知。"""
        case = build_case()
        self.assertIn("D-09", case.notice_targets())
        f05_notice = [n for n in case._notices.values() if n.recipient_ref == "F-05"]
        self.assertEqual(f05_notice, [])
        donee = [n for n in case._notices.values() if n.recipient_ref == "D-09"]
        self.assertEqual(len(donee[0].unit_ids), 3)

    def test_anonymous_buyer_gets_inapp_channel_only(self):
        case = build_case()
        notice = [n for n in case._notices.values() if n.recipient_ref == "F-03"][0]
        self.assertEqual(notice.channel, "平台站内信")
        self.assertTrue(case.orders["O-1004"].anonymous)

    def test_refund_only_units_carry_reason(self):
        case = build_case()
        refund_only = [
            u for u in case.affected_units()
            if case._units[u].disposition is Disposition.REFUND_ONLY
        ]
        self.assertEqual(len(refund_only), 2)
        for uid in refund_only:
            self.assertIn("无实物可退", case._units[uid].disposition_detail)


class ConsumerVerificationTest(unittest.TestCase):
    def setUp(self):
        self.case = build_case()
        self.notice = [
            n for n in self.case._notices.values() if n.recipient_ref == "F-01"
        ][0]

    def test_consumer_can_verify_notice_and_see_status(self):
        result = self.case.verify_notice(self.notice.verification_code, "O-1001")
        self.assertTrue(result["valid"])
        self.assertEqual(result["notice_id"], self.notice.notice_id)
        statuses = result["status"]
        self.assertEqual({s["disposition"] for s in statuses}, {"已退回"})
        self.assertTrue(all(s["refunded"] for s in statuses))

    def test_wrong_code_rejected(self):
        self.assertFalse(self.case.verify_notice("0" * 16, "O-1001")["valid"])

    def test_unknown_order_rejected(self):
        self.assertFalse(
            self.case.verify_notice(self.notice.verification_code, "O-XXXX")["valid"]
        )

    def test_code_does_not_work_for_other_familys_order(self):
        """通知码只能核验本人订单，不能枚举他人状态。"""
        self.assertFalse(
            self.case.verify_notice(self.notice.verification_code, "O-1101")["valid"]
        )


class MerchantViewTest(unittest.TestCase):
    def test_merchant_sees_only_fulfilment_data(self):
        case = build_case()
        view = case.merchant_view()
        flat = repr(view)
        for leak in ("F-01", "D-09", "B-03", "电话", "停机"):
            self.assertNotIn(leak, flat)
        tokens = [r["logistics_token"] for r in view["returns_to_receive"]]
        # 合并订单只有一张退货单（RT-0001 覆盖 5 双）
        self.assertEqual(len(tokens), len(set(tokens)))
        merged = next(r for r in view["returns_to_receive"] if r["return_id"] == "RT-0001")
        self.assertEqual(merged["quantity"], 5)

    def test_disposition_conflict_is_rejected(self):
        case = build_case(execute=False)
        case.settle_pre_recall_exchanges()
        uid = next(iter(case.pre_recall_controlled_units()))
        with self.assertRaises(ValueError):
            case.record_return("RT-BAD", "O-1005", [uid])


class ClosureTest(unittest.TestCase):
    def test_recall_cannot_close_with_unaccepted_residual(self):
        case = build_case()
        closure = case.closure_check()
        self.assertFalse(closure["can_close"])
        self.assertEqual(closure["pending_units"], 0)
        self.assertEqual(len(closure["residual_uncontrolled"]), 11)

    def test_recall_closes_after_regulator_accepts_residual(self):
        case = build_case()
        closure = case.closure_check(regulator_accepts_residual=True)
        self.assertTrue(closure["can_close"])

    def test_recall_with_zero_residual_closes_directly(self):
        """构造一个全部追回的小案件：无需监管接受残余即可结束。"""
        units = [f"{AFFECTED_BATCH}-V21-{i:04d}" for i in range(1, 4)]
        order = Order("O-1", "B-1", False, (OrderLine("V21", tuple(units)),))
        shipment = Shipment("S-1", ("O-1",), "F-1", (("签收", ""),), True)
        case = RecallCase("RC-X", [AFFECTED_BATCH], [order], [shipment])
        case.settle_pre_recall_exchanges()
        notice = case.issue_notice("F-1", frozenset(units), "短信")
        case.mark_notice_delivered(notice.notice_id, True)
        case.record_return("RT-1", "O-1", units)
        case.apply_refund_callback("CB-1", "O-1", units, 300)
        self.assertTrue(case.identities_hold())
        self.assertTrue(case.closure_check()["can_close"])
        self.assertEqual(case.regulator_view()["uncontrolled_units"], 0)


if __name__ == "__main__":
    unittest.main()
