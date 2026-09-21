"""读取并检查共享的领域资料。"""

import json
from pathlib import Path

REQUIRED_FIELDS = {
    "domain",
    "version",
    "sample_id",
    "actors",
    "facts",
    "identification",
    "phases",
    "dedup_rules",
    "privacy",
    "reconciliation",
    "constraints",
}

IDENTIFICATION_SIGNALS = {"采购批次", "商品变体", "订单", "物流", "收货确认", "消费者主动报告"}

DEDUP_CASES = {"合并订单", "拒收重派", "匿名购买", "售后换货", "重复通知", "支付回调"}

RECONCILIATION_METRICS = ["售出", "触达", "退回", "退款", "未控制"]

ACCOUNT_KEYS = ("sold", "reached", "returned", "refunded", "controlled", "uncontrolled")


def load_domain(path: Path) -> dict:
    """返回字段完整且带版本的业务资料。"""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not REQUIRED_FIELDS.issubset(value):
        raise ValueError("共享资料缺少必要字段")
    if value["version"] < 2 or len(value["actors"]) < 2 or len(value["facts"]) < 2 or len(value["constraints"]) < 2:
        raise ValueError("共享资料内容不完整")
    if not IDENTIFICATION_SIGNALS.issubset(value["identification"]["signals"]):
        raise ValueError("受影响交易识别信号不完整")
    cases = {rule["case"] for rule in value["dedup_rules"]}
    if not DEDUP_CASES.issubset(cases):
        raise ValueError("去重规则未覆盖全部易重复计算情形")
    if value["reconciliation"]["metrics"] != RECONCILIATION_METRICS:
        raise ValueError("召回闭环核对指标不符合约定")
    problems = check_reconciliation(value["reconciliation"]["example"])
    if problems:
        raise ValueError("示例账目不满足可核对关系：" + "；".join(problems))
    return value


def check_reconciliation(record: dict) -> list[str]:
    """核对售出、触达、退回、退款与未控制之间的数量关系。

    已控制是退回与退款按受影响交易去重后的并集。返回发现的问题列表，
    空列表表示账目可核对。
    """
    missing = [key for key in ACCOUNT_KEYS if key not in record]
    if missing:
        return ["账目缺少字段：" + "、".join(missing)]
    problems = []
    sold = record["sold"]
    reached = record["reached"]
    returned = record["returned"]
    refunded = record["refunded"]
    controlled = record["controlled"]
    uncontrolled = record["uncontrolled"]
    reasons = record.get("uncontrolled_reasons", [])
    if any(record[key] < 0 for key in ACCOUNT_KEYS):
        problems.append("账目数量不能为负")
    if reached > sold:
        problems.append("触达数量超过售出数量")
    if controlled > reached:
        problems.append("已控制数量超过触达数量")
    if returned > controlled:
        problems.append("退回数量超过已控制数量")
    if refunded > controlled:
        problems.append("退款数量超过已控制数量")
    if controlled > returned + refunded:
        problems.append("已控制数量超过退回与退款之和")
    if controlled + uncontrolled != sold:
        problems.append("已控制与未控制之和不等于售出数量")
    if sum(item["quantity"] for item in reasons) > uncontrolled:
        problems.append("无法追回原因的数量超过未控制数量")
    return problems


def recall_closed(record: dict) -> bool:
    """判断召回是否结束：账目可核对，且未控制为零或逐项注明无法追回原因。"""
    if check_reconciliation(record):
        return False
    if record["uncontrolled"] == 0:
        return True
    reasons = record.get("uncontrolled_reasons", [])
    return bool(reasons) and sum(item["quantity"] for item in reasons) == record["uncontrolled"]
