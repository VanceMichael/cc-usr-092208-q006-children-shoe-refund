import json
import tempfile
import unittest
from pathlib import Path

from src.domain import check_reconciliation, load_domain, recall_closed


class DomainTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.domain = load_domain(Path("fixtures/domain.json"))

    def test_fixture_matches_domain(self):
        self.assertEqual(self.domain["domain"], "children-shoe-refund")
        self.assertGreaterEqual(len(self.domain["constraints"]), 2)

    def test_identification_signals_complete(self):
        signals = set(self.domain["identification"]["signals"])
        self.assertEqual(
            signals,
            {"采购批次", "商品变体", "订单", "物流", "收货确认", "消费者主动报告"},
        )

    def test_dedup_rules_cover_double_counting_cases(self):
        cases = {rule["case"] for rule in self.domain["dedup_rules"]}
        self.assertEqual(
            cases,
            {"合并订单", "拒收重派", "匿名购买", "售后换货", "重复通知", "支付回调"},
        )

    def test_actor_visibility_is_minimal(self):
        visibility = {actor["id"]: actor["visibility"] for actor in self.domain["actors"]}
        self.assertIn("履约所需", visibility["merchant"])
        self.assertIn("核验通知真伪", visibility["family"])
        self.assertIn("未控制", visibility["regulator"])

    def test_reconciliation_example_is_consistent(self):
        example = self.domain["reconciliation"]["example"]
        self.assertEqual(check_reconciliation(example), [])
        self.assertTrue(recall_closed(example))

    def test_reconciliation_detects_imbalance(self):
        example = dict(self.domain["reconciliation"]["example"])
        example["uncontrolled"] -= 1
        self.assertTrue(check_reconciliation(example))
        self.assertFalse(recall_closed(example))

    def test_uncontrolled_without_reasons_keeps_recall_open(self):
        example = dict(self.domain["reconciliation"]["example"])
        example["uncontrolled_reasons"] = []
        self.assertEqual(check_reconciliation(example), [])
        self.assertFalse(recall_closed(example))

    def test_loader_rejects_incomplete_materials(self):
        broken = {"domain": "children-shoe-refund", "version": 2}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "domain.json"
            path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_domain(path)


if __name__ == "__main__":
    unittest.main()
