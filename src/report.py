"""命令行入口：输出监管侧召回闭环报告。

用法::

    python -m src.report            # 查看当前处置状态
    python -m src.report --accept   # 监管人员书面接受残余未控制后重新判定
"""

from __future__ import annotations

import argparse
import json

from .sample_data import build_case


def main() -> None:
    parser = argparse.ArgumentParser(description="童鞋召回闭环报告")
    parser.add_argument("--accept", action="store_true",
                        help="监管人员接受已注明原因的未控制残余")
    args = parser.parse_args()

    case = build_case()
    view = case.regulator_view()
    view["closure"] = case.closure_check(regulator_accepts_residual=args.accept)
    print(json.dumps(view, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
