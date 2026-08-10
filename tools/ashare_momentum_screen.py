#!/usr/bin/env python3
"""A股动量筛选 — 东方财富排行榜 + 多重硬过滤。stdlib only.

筛选逻辑（顺序执行，任一不过即 REJECTED）:
  1. 排除 ST/*ST
  2. 排除接近涨停（距涨停 ≤ 15%，即 10cm 股 f3 ≥ 8.5%）
  3. 价格 ≥ 5 元（规避低价股噪音）
  4. 成交额 ≥ MIN_AMOUNT_YI 亿元（默认 2 亿）
  5. 量比 ≥ MIN_VOL_RATIO（默认 1.5）

输出分级:
  ACTIONABLE  — 全部过滤通过，可进入 Phase 3 验证
  WATCH       — 量比稍低（>= 1.0）但其余条件全通过
  REJECTED    — 任一硬过滤不通过

用法:
    python tools/ashare_momentum_screen.py
    python tools/ashare_momentum_screen.py --min-amount 3 --min-vol-ratio 2.0 --top 20
    python tools/ashare_momentum_screen.py --output-dir reports/
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
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "http://quote.eastmoney.com/",
}

# 主板+创业板+科创板（排除北交所：T+1 且流动性太差，混入会污染结果）
_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
_FIELDS = "f2,f3,f5,f6,f10,f12,f14,f20"
# f2=价格 f3=涨跌幅 f5=成交量(手) f6=成交额(元) f10=量比 f12=代码 f14=名称 f20=总市值(元)


def _get(params: dict) -> dict:
    url = "http://push2.eastmoney.com/api/qt/clist/get?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read()
    except Exception as e:
        raise ConnectionError(f"请求失败: {e}") from e
    return json.loads(raw.decode("utf-8"))


_HEADERS_SINA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://finance.sina.com.cn",
}


def _fetch_top_gainers_sina(top: int) -> list[dict]:
    """新浪涨幅榜 fallback，返回与东财相同的字段格式。"""
    url = (
        "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php"
        "/Market_Center.getHQNodeData"
        f"?num={top}&sort=changepercent&asc=0&node=hs_a&_s_r_a=page"
    )
    req = urllib.request.Request(url, headers=_HEADERS_SINA)
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        raw = resp.read()
        try:
            batch = json.loads(raw.decode("utf-8"))
        except Exception:
            batch = json.loads(raw.decode("gbk", errors="replace"))
    result = []
    for s in batch:
        result.append({
            "f2": s.get("trade"),           # 最新价
            "f3": s.get("changepercent"),   # 涨跌幅 %
            "f5": s.get("volume"),          # 成交量（手）
            "f6": float(s.get("amount") or 0),  # 成交额（元）
            "f10": None,                    # 量比（新浪不提供，置 None）
            "f12": s.get("code", ""),
            "f14": s.get("name", ""),
            "f20": float(s.get("mktcap") or 0) * 10000,  # 总市值（万→元）
        })
    return result


def _fetch_top_gainers(top: int) -> list[dict]:
    """拉取涨幅榜前 top 只，按 f3 降序。"""
    params = {
        "pn": "1", "pz": str(top), "po": "1", "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2", "invt": "2",
        "fid": "f3",    # 按涨跌幅排序
        "fs": _FS,
        "fields": _FIELDS,
    }
    data = _get(params)
    return (data.get("data") or {}).get("diff") or []


def _limit_pct(code: str) -> float:
    if str(code).startswith(("300", "301", "688", "689")):
        return 20.0
    return 10.0


def _classify(
    s: dict,
    min_amount_yi: float,
    min_vol_ratio: float,
) -> tuple[str, list[str]]:
    """返回 (tier, [reject_reasons])。tier: ACTIONABLE / WATCH / REJECTED。"""
    reasons: list[str] = []

    name = str(s.get("f14") or "")
    code = str(s.get("f12") or "")

    # --- 硬过滤 ---
    if "ST" in name:
        return "REJECTED", ["ST股"]

    raw_pct = s.get("f3")
    raw_price = s.get("f2")
    raw_amount = s.get("f6")
    raw_vol_ratio = s.get("f10")

    try:
        pct = float(raw_pct) if raw_pct not in (None, "-") else None
        price = float(raw_price) if raw_price not in (None, "-") else None
        amount_yi = float(raw_amount) / 1e8 if raw_amount not in (None, "-") else None
        vol_ratio = float(raw_vol_ratio) if raw_vol_ratio not in (None, "-") else None
    except (ValueError, TypeError):
        return "REJECTED", ["数据解析失败"]

    # 硬过滤字段缺失 → 直接拒绝，不放行
    if pct is None:
        return "REJECTED", ["涨跌幅数据缺失（可能停牌）"]
    if price is None:
        return "REJECTED", ["价格数据缺失"]
    if amount_yi is None:
        return "REJECTED", ["成交额数据缺失"]

    lim = _limit_pct(code)
    near_limit_threshold = lim * 0.85  # 10cm → 8.5%, 20cm → 17%
    if pct >= near_limit_threshold:
        reasons.append(f"接近/达到涨停 ({pct:.1f}% >= {near_limit_threshold:.1f}%)")

    if price < 5.0:
        reasons.append(f"价格过低 ({price:.2f} < 5.00)")

    if amount_yi < min_amount_yi:
        reasons.append(f"成交额不足 ({amount_yi:.2f}亿 < {min_amount_yi}亿)")

    if reasons:
        return "REJECTED", reasons

    # --- 量比分级 ---
    if vol_ratio is None:
        # 数据源不提供量比（如新浪），硬过滤已通过则视为 ACTIONABLE
        return "ACTIONABLE", ["量比数据源不支持，已跳过"]

    if vol_ratio >= min_vol_ratio:
        return "ACTIONABLE", []
    if vol_ratio >= 1.0:
        return "WATCH", [f"量比偏低 ({vol_ratio:.2f} < {min_vol_ratio})"]

    return "REJECTED", [f"量比不足 ({vol_ratio:.2f} < 1.0)"]


def screen(
    top: int = 100,
    min_amount_yi: float = 2.0,
    min_vol_ratio: float = 1.5,
) -> dict:
    print(f"拉取涨幅榜前 {top} 只...")
    source = "eastmoney"
    try:
        raw = _fetch_top_gainers(top)
    except Exception as e:
        print(f"  东方财富失败 ({e})，切换新浪...")
        raw = _fetch_top_gainers_sina(top)
        source = "sina"
    if not raw:
        raise RuntimeError("未返回数据，请检查网络")
    print(f"  数据源: {source}，获取 {len(raw)} 只")

    results: dict[str, list[dict]] = {"ACTIONABLE": [], "WATCH": [], "REJECTED": []}

    for s in raw:
        tier, reasons = _classify(s, min_amount_yi, min_vol_ratio)

        code = str(s.get("f12") or "")
        lim = _limit_pct(code)
        raw_amount = s.get("f6")
        amount_yi = round(float(raw_amount) / 1e8, 2) if raw_amount not in (None, "-") else None

        entry = {
            "code": code,
            "name": str(s.get("f14") or ""),
            "price": s.get("f2"),
            "pct": s.get("f3"),
            "vol_ratio": s.get("f10"),
            "amount_yi": amount_yi,
            "limit_pct": lim,
            "tier": tier,
            "reject_reasons": reasons,
        }
        results[tier].append(entry)

    return {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "time": datetime.now().strftime("%H:%M"),
        "params": {
            "top": top,
            "min_amount_yi": min_amount_yi,
            "min_vol_ratio": min_vol_ratio,
        },
        "summary": {k: len(v) for k, v in results.items()},
        "results": results,
    }


def _fmt_row(e: dict) -> str:
    return (
        f"  {e['code']} {e['name']:<8}"
        f"  {str(e['pct']) + '%':>7}"
        f"  量比:{str(e['vol_ratio'] or '-'):>5}"
        f"  {str(e['amount_yi'] or '-') + '亿':>8}"
    )


def print_summary(r: dict) -> None:
    print("\n" + "=" * 60)
    print(f"  A股动量筛选  {r['date']}  {r['time']}")
    print(f"  参数: 前{r['params']['top']}名涨幅  "
          f"最低成交额 {r['params']['min_amount_yi']}亿  "
          f"最低量比 {r['params']['min_vol_ratio']}")
    print("=" * 60)

    for tier in ("ACTIONABLE", "WATCH"):
        items = r["results"][tier]
        label = "[GO] ACTIONABLE" if tier == "ACTIONABLE" else "[WATCH]"
        print(f"\n{label} ({len(items)} 只):")
        if not items:
            print("  （无）")
        for e in items:
            print(_fmt_row(e))

    rej = r["results"]["REJECTED"]
    print(f"\n[REJECTED]: {len(rej)} 只 (首条原因)")
    for e in rej[:5]:
        print(f"  {e['code']} {e['name']:<8}  {e['reject_reasons'][0] if e['reject_reasons'] else '-'}")
    if len(rej) > 5:
        print(f"  ... 还有 {len(rej) - 5} 只")
    print("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(description="A股动量筛选")
    parser.add_argument("--top", type=int, default=100, help="拉取涨幅榜前 N 只（默认 100）")
    parser.add_argument("--min-amount", type=float, default=2.0,
                        dest="min_amount", help="最低成交额（亿元，默认 2）")
    parser.add_argument("--min-vol-ratio", type=float, default=1.5,
                        dest="min_vol_ratio", help="最低量比（默认 1.5）")
    parser.add_argument("--output-dir", default="reports", help="报告输出目录")
    args = parser.parse_args()

    try:
        result = screen(
            top=args.top,
            min_amount_yi=args.min_amount,
            min_vol_ratio=args.min_vol_ratio,
        )
    except Exception as e:
        print(f"❌ 错误: {e}", file=sys.stderr)
        sys.exit(1)

    print_summary(result)

    os.makedirs(args.output_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out_path = os.path.join(args.output_dir, f"ashare_momentum_{ts}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n  报告已保存: {out_path}")


if __name__ == "__main__":
    main()
