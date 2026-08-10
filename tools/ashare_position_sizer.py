#!/usr/bin/env python3
"""A股仓位计算 — 风险百分比法，自动对齐 100 股/手。stdlib only.

A股特殊规则:
  - 最小交易单位: 100 股（1 手），买入必须是 100 的整数倍
  - 卖出可以为任意数量（不强制 100 整数倍），本脚本仅处理买入
  - 科创板部分个股最低 200 股，用 --lot-size 指定
  - 仓位不足 1 手时输出 NO_TRADE（不做），不强行买零散股

算法:
  dollar_risk    = account_size × risk_pct / 100
  risk_per_share = entry - stop
  lots_by_risk   = floor(dollar_risk / risk_per_share / lot_size)
  lots_by_cash   = floor(account_size / (entry × lot_size))   ← 硬上限：不超账户现金
  lots           = min(lots_by_risk, lots_by_cash)
  actual_shares  = lots × lot_size
  如 lots == 0 → NO_TRADE

用法:
    python tools/ashare_position_sizer.py --entry 52.30 --stop 49.80 \\
        --account-size 500000 --risk-pct 1.0

    # 科创板 / 最低 200 股的标的
    python tools/ashare_position_sizer.py --entry 85.00 --stop 81.00 \\
        --account-size 500000 --risk-pct 1.0 --lot-size 200
"""

import argparse
import json
import math
import os
import sys
from datetime import datetime

# Windows GBK 控制台下 emoji 会触发 UnicodeEncodeError，统一强制 utf-8 输出
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def size_position(
    account_size: float,
    entry: float,
    stop: float,
    risk_pct: float,
    lot_size: int = 100,
) -> dict:
    if entry <= stop:
        raise ValueError(f"entry ({entry}) 必须大于 stop ({stop})")
    if risk_pct <= 0:
        raise ValueError("risk_pct 必须为正数")
    if account_size <= 0:
        raise ValueError("account_size 必须为正数")
    if lot_size <= 0 or lot_size % 100 != 0:
        raise ValueError("lot_size 必须是 100 的正整数倍")

    dollar_risk = account_size * risk_pct / 100
    risk_per_share = entry - stop

    lots_by_risk = math.floor(dollar_risk / risk_per_share / lot_size)
    # 硬上限：仓位市值不超账户现金（拒绝隐含融资）
    lots_by_cash = math.floor(account_size / (entry * lot_size))
    lots = min(lots_by_risk, lots_by_cash)
    cash_capped = lots < lots_by_risk

    actual_shares = lots * lot_size
    no_trade = actual_shares == 0

    return {
        "entry": entry,
        "stop": stop,
        "account_size": account_size,
        "risk_pct": risk_pct,
        "lot_size": lot_size,
        "dollar_risk_budget": round(dollar_risk, 2),
        "risk_per_share": round(risk_per_share, 4),
        "lots_by_risk": lots_by_risk,
        "lots_by_cash": lots_by_cash,
        "lots": lots,
        "cash_capped": cash_capped,
        "actual_shares": actual_shares,
        "actual_position_value": round(actual_shares * entry, 2),
        "actual_risk": round(actual_shares * risk_per_share, 2),
        "actual_risk_pct": round(actual_shares * risk_per_share / account_size * 100, 3)
            if actual_shares > 0 else 0.0,
        "no_trade": no_trade,
        "note": (
            "持仓不足 1 手，本次不入场" if no_trade
            else "风险预算超过账户现金，已按现金上限裁剪" if cash_capped
            else ""
        ),
    }


def print_result(r: dict) -> None:
    print("\n" + "=" * 50)
    if r["no_trade"]:
        print("  [NO TRADE]")
        print(f"  风险预算 {r['dollar_risk_budget']:.0f} 元")
        print(f"  每股风险 {r['risk_per_share']:.4f} 元")
        print(f"  按风险算 {r['lots_by_risk']} 手 / 按现金算 {r['lots_by_cash']} 手 → 均不足 1 手")
        print("  建议: 调大账户或风险比例，或选择更低价格的标的")
    else:
        print(f"  买入: {r['actual_shares']} 股  ({r['lots']} 手 x {r['lot_size']} 股/手)")
        print(f"  入场: {r['entry']}  止损: {r['stop']}  风险/股: {r['risk_per_share']:.4f}")
        print(f"  仓位市值: {r['actual_position_value']:,.0f} 元")
        print(f"  实际风险: {r['actual_risk']:,.0f} 元  ({r['actual_risk_pct']:.3f}%)")
        if r["cash_capped"]:
            print(f"  [WARNING] 风险预算超账户资金，已裁剪至 {r['lots_by_cash']} 手（现金上限）")
        elif r["actual_risk_pct"] < r["risk_pct"] * 0.85:
            delta = r["risk_pct"] - r["actual_risk_pct"]
            print(f"  取整导致实际风险比目标低 {delta:.3f}%（正常，勿调整）")
    print("=" * 50)


def main() -> None:
    parser = argparse.ArgumentParser(description="A股仓位计算（100股/手）")
    parser.add_argument("--entry", type=float, required=True, help="计划入场价（元）")
    parser.add_argument("--stop", type=float, required=True, help="止损价（元）")
    parser.add_argument("--account-size", type=float, required=True,
                        dest="account_size", help="账户总资金（元）")
    parser.add_argument("--risk-pct", type=float, required=True,
                        dest="risk_pct", help="单笔风险比例，如 1.0 表示 1%%")
    parser.add_argument("--lot-size", type=int, default=100,
                        dest="lot_size", help="最小交易单位（默认 100）")
    parser.add_argument("--output-dir", default="reports", help="JSON 报告保存目录")
    args = parser.parse_args()

    try:
        result = size_position(
            account_size=args.account_size,
            entry=args.entry,
            stop=args.stop,
            risk_pct=args.risk_pct,
            lot_size=args.lot_size,
        )
    except ValueError as e:
        print(f"❌ 参数错误: {e}", file=sys.stderr)
        sys.exit(1)

    print_result(result)

    os.makedirs(args.output_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out_path = os.path.join(args.output_dir, f"ashare_position_{ts}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n  报告已保存: {out_path}")

    if result["no_trade"]:
        sys.exit(2)  # 让调用方可以检测 NO_TRADE 状态


if __name__ == "__main__":
    main()
