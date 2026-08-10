#!/usr/bin/env python3
"""A股市场宽度 — 分页拉全市场，统计真实涨跌家数。stdlib only.

用法:
    python tools/ashare_breadth.py
    python tools/ashare_breadth.py --output-dir reports/
    python tools/ashare_breadth.py --save-raw   # 同时保存原始 JSON 快照

输出字段:
    breadth_score    上涨家数 / (上涨+下跌+平) × 100，排除ST
    limit_up_count   接近或到达涨停的票数 (距涨停 ≤ 3%)
    limit_down_count 接近或到达跌停的票数
    total_amount_yi  全市场成交额（亿元）
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TIMEOUT = 20
_HEADERS_EM = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "http://quote.eastmoney.com/",
}
_HEADERS_SINA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://finance.sina.com.cn",
}
_PAGE_SIZE = 3000

_MARKETS = [
    ("主板+创业板", "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"),
    ("科创板",     "m:0+t:81+s:2048"),
]


def _get(params: dict) -> dict:
    url = "http://push2.eastmoney.com/api/qt/clist/get?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=_HEADERS_EM)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read()
    except Exception as e:
        raise ConnectionError(f"请求失败: {url}\n{e}") from e
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"响应非 JSON: {raw[:200]}") from e


_SINA_PAGE_SIZE = 80  # 新浪实际上限约 80-100，保守用 80

def _fetch_sina_all() -> list[dict]:
    """新浪 fallback：分页拉全 A 股（hs_a 含沪深北所有 A 股）。
    返回统一格式 {f3, f6, f12, f14} 对应宽度计算字段。
    # ponytail: ~70 pages x 80 stocks = ~5600 stocks, ~30s total
    """
    base = "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
    all_stocks = []
    page = 1
    while True:
        params = {"num": str(_SINA_PAGE_SIZE), "sort": "symbol", "asc": "1",
                  "node": "hs_a", "page": str(page), "_s_r_a": "page"}
        url = base + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=_HEADERS_SINA)
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                raw = resp.read()
                try:
                    batch = json.loads(raw.decode("utf-8"))
                except Exception:
                    batch = json.loads(raw.decode("gbk", errors="replace"))
        except Exception as e:
            print(f"  ! 新浪第{page}页失败: {e}", file=sys.stderr)
            break
        if not batch:   # 空页 = 拉完了
            break
        for s in batch:
            all_stocks.append({
                "f3": s.get("changepercent"),
                "f6": float(s.get("amount") or 0),
                "f12": s.get("code", ""),
                "f14": s.get("name", ""),
            })
        if page % 10 == 0:
            print(f"  新浪已拉 {len(all_stocks)} 只...")
        page += 1
    return all_stocks


def _fetch_market(fs: str) -> list[dict]:
    """分页拉取指定板块所有股票，field 集合精简到宽度计算所需字段。"""
    base_params = {
        "pn": "1", "pz": str(_PAGE_SIZE), "po": "1", "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2",
        "invt": "2",
        "fid": "f3",
        "fs": fs,
        "fields": "f3,f6,f12,f14",
    }
    all_stocks: list[dict] = []
    page = 1
    while True:
        base_params["pn"] = str(page)
        data = _get(base_params)
        inner = data.get("data") or {}
        diff = inner.get("diff") or []
        if not diff:
            break
        all_stocks.extend(diff)
        total = inner.get("total", 0)
        if len(all_stocks) >= total:
            break
        page += 1
    return all_stocks


def _limit_pct(code: str) -> float:
    """涨跌停幅度：创业板/科创板 ±20%，其余 ±10%。"""
    if str(code).startswith(("300", "301", "688", "689")):
        return 20.0
    return 10.0


def run_analysis() -> tuple[dict, list[dict]]:
    """返回 (result, raw_stocks)。raw_stocks 用于 --save-raw 的原始快照。"""
    all_stocks: list[dict] = []
    fetch_errors = []
    source = "eastmoney"

    for label, fs in _MARKETS:
        try:
            stocks = _fetch_market(fs)
            all_stocks.extend(stocks)
            print(f"  {label}: {len(stocks)} 只")
        except Exception as e:
            fetch_errors.append(f"{label}: {e}")
            print(f"  ! {label} 获取失败: {e}", file=sys.stderr)

    # 东财全部失败时切换新浪
    if not all_stocks:
        print("  东方财富不可达，切换新浪数据源...")
        try:
            all_stocks = _fetch_sina_all()
            source = "sina"
            fetch_errors = []
            print(f"  新浪全 A 股: {len(all_stocks)} 只")
        except Exception as e:
            fetch_errors.append(f"新浪: {e}")

    if not all_stocks:
        raise RuntimeError("未能获取任何数据，请检查网络。错误: " + "; ".join(fetch_errors))

    # 分离 ST（±5% 涨跌停，单独体系，不计入宽度）
    st_stocks = [s for s in all_stocks if "ST" in (s.get("f14") or "")]
    non_st = [s for s in all_stocks if "ST" not in (s.get("f14") or "")]

    up = down = flat = 0
    limit_up_count = limit_down_count = 0
    total_amount = 0.0

    for s in non_st:
        raw_pct = s.get("f3")
        code = str(s.get("f12") or "")
        raw_amount = s.get("f6") or 0

        # 停牌或无成交时 f3 可能为 None/"-"
        if raw_pct is None or raw_pct == "-":
            continue

        try:
            pct = float(raw_pct)
            amount = float(raw_amount)
        except (ValueError, TypeError):
            continue

        lim = _limit_pct(code)
        threshold = lim * 0.97  # 距涨跌停 ≤ 3%

        if pct > 0:
            up += 1
        elif pct < 0:
            down += 1
        else:
            flat += 1

        if pct >= threshold:
            limit_up_count += 1
        elif pct <= -threshold:
            limit_down_count += 1

        total_amount += amount

    total = up + down + flat
    breadth_score = round(up / total * 100, 1) if total > 0 else 0.0

    result = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "time": datetime.now().strftime("%H:%M"),
        "breadth_score": breadth_score,
        "up": up,
        "down": down,
        "flat": flat,
        "total": total,
        "limit_up_count": limit_up_count,
        "limit_down_count": limit_down_count,
        "st_excluded": len(st_stocks),
        "total_amount_yi": round(total_amount / 1e8, 1),
        "fetch_errors": fetch_errors,
        "data_source": source,
        "_raw_count": len(all_stocks),
    }
    return result, all_stocks


def _gate_signal(score: float) -> str:
    if score >= 60:
        return "[GO] 可以寻找机会"
    if score >= 40:
        return "[CAUTION] 仅维护现有仓位"
    return "[STOP] 资金保护模式"


def print_summary(r: dict) -> None:
    print("\n" + "=" * 55)
    print(f"  A股市场宽度报告  {r['date']}  {r['time']}")
    print("=" * 55)
    print(f"  宽度得分:  {r['breadth_score']:.1f}%  →  {_gate_signal(r['breadth_score'])}")
    print(f"  上涨:      {r['up']}  下跌: {r['down']}  平盘: {r['flat']}")
    print(f"  涨停附近:  {r['limit_up_count']}  跌停附近: {r['limit_down_count']}")
    print(f"  全市场成交: {r['total_amount_yi']} 亿元")
    print(f"  ST 剔除:   {r['st_excluded']} 只")
    if r["fetch_errors"]:
        print(f"  ⚠ 获取错误: {'; '.join(r['fetch_errors'])}")
    print("=" * 55)


def main() -> None:
    parser = argparse.ArgumentParser(description="A股市场宽度分析")
    parser.add_argument("--output-dir", default="reports", help="报告输出目录")
    parser.add_argument("--save-raw", action="store_true",
                        help="同时保存每支股票的原始行情快照（用于事后审计）")
    parser.add_argument("--allow-partial", action="store_true",
                        help="板块部分失败时仍输出结果（默认: 有任何板块失败即退出 1）")
    args = parser.parse_args()

    print("正在拉取全市场数据...")
    result, raw_stocks = run_analysis()
    print_summary(result)

    if result["fetch_errors"] and not args.allow_partial:
        print(f"\n[ERROR] 部分板块获取失败，宽度数据不完整，退出。"
              f"如需继续使用不完整数据，加 --allow-partial", file=sys.stderr)
        sys.exit(1)

    os.makedirs(args.output_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")

    out_path = os.path.join(args.output_dir, f"ashare_breadth_{ts}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n  报告已保存: {out_path}")

    if args.save_raw:
        raw_path = os.path.join(args.output_dir, f"ashare_breadth_raw_{ts}.json")
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(raw_stocks, f, ensure_ascii=False, indent=2)
        print(f"  原始快照:   {raw_path}  ({len(raw_stocks)} 只)")


if __name__ == "__main__":
    main()
