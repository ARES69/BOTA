"""Диагностика «почему бот не входит»: печатает вердикт и причины отказа
для каждого fast-кандидата и гистограмму причин полного скана.

Запуск:  python3 diag_entry.py            (пороги как у watch, из config.json)
         python3 diag_entry.py --aggressive  (те пороги, что собираемся поставить)
Только чтение: ордера не ставятся, состояние не меняется.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import funding_executor as fe            # noqa: E402
from funding_monitor import PRESETS      # noqa: E402


def build_cli(argv: list[str]) -> argparse.Namespace:
    """cli как в main(): config.json (только нужные фильтру ключи) + пресет."""
    import json
    ns = argparse.Namespace()
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            fe.CONFIG_FILE)
    if os.path.exists(cfg_path):
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        for key, val in cfg.items():
            if key.startswith("_") or not isinstance(val, (int, float, bool, str)):
                continue
            attr = key.replace("-", "_")
            if attr in ("preset", "mode", "capital", "min_volume", "min_oi",
                        "prefilter", "fast_apy", "fast_top", "pass_oi",
                        "min_apy30", "min_pos", "max_breakeven", "min_basis"):
                setattr(ns, attr, val)
    preset = getattr(ns, "preset", None) or "hold"
    ns.preset = preset
    # атрибуты, которых требуют fast_candidates/deep_check
    defaults = dict(
        mode="testnet", min_volume=3.0, min_oi=3.0, prefilter=60,
        fast_apy=3.0, fast_top=8, pass_oi=10.0, spot_fee=fe.DEFAULT_SPOT_FEE,
        perp_taker_fee=fe.DEFAULT_PERP_TAKER_FEE,
        perp_maker_fee=fe.DEFAULT_PERP_MAKER_FEE,
        capital=1000.0, no_learn=False, ws=False, interval=15, full_every=300,
    )
    for k, v in defaults.items():
        if not hasattr(ns, k):
            setattr(ns, k, v)
    # пресет должен перекрывать argparse-дефолты, как в main()
    for key, val in PRESETS[preset].items():
        cur = getattr(ns, key, None)
        if cur is None or (key == "fast_apy" and cur == 3.0) \
                or (key == "pass_oi" and cur == 10.0) \
                or (key in ("min_volume", "min_oi") and cur == 3.0):
            setattr(ns, key, val)
    return ns


def verdict_row(r: dict) -> str:
    w30 = r.get("w30") or {}
    return (f"{r['symbol']:<14} cur={r.get('cur_apy', 0):+6.1f}% "
            f"apy30={w30.get('avg_apy', float('nan')):+6.1f}% "
            f"pos={w30.get('pos_share', 0):3.0f}% "
            f"basis={(r['basis'] if r.get('basis') is not None else float('nan')):+.3f}% "
            f"окуп={r.get('breakeven_shown') or float('nan'):5.1f}д "
            f"OI=${r.get('oi_value', 0)/1e6:,.1f}M  {r['verdict']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--aggressive", action="store_true",
                    help="показать результат с агрессивными порогами из аргументов")
    ap.add_argument("--min-apy30", type=float, default=None)
    ap.add_argument("--min-pos", type=float, default=None)
    ap.add_argument("--max-breakeven", type=float, default=None)
    ap.add_argument("--pass-oi", type=float, default=None)
    ap.add_argument("--fast-apy", type=float, default=None)
    ap.add_argument("--min-basis", type=float, default=None)
    ap.add_argument("--no-learn", action="store_true",
                    help="игнорировать калиброванные пороги памяти "
                         "(показать чистый эффект конфига)")
    a = ap.parse_args()
    cli = build_cli([])
    if a.no_learn:
        cli.no_learn = True
    if a.aggressive:
        for k in ("min_apy30", "min_pos", "max_breakeven", "pass_oi",
                  "fast_apy", "min_basis"):
            v = getattr(a, k)
            if v is not None:
                setattr(cli, k, v)
    print(f"пороги: preset={cli.preset} min_apy30={cli.min_apy30} "
          f"min_pos={cli.min_pos} min_basis={cli.min_basis} "
          f"max_breakeven={cli.max_breakeven} pass_oi={cli.pass_oi} "
          f"fast_apy={cli.fast_apy}")

    # --- 1) fast-кандидаты (как в watch) ---
    now = int(time.time() * 1000)
    fargs = fe.build_filter_args(cli)
    cands = fe.fast_candidates(cli, feed=None)
    print(f"\nfast-кандидатов: {len(cands)}")
    spot = fe.fetch_spot_quotes()
    passed = 0
    for t in cands:
        r = fe.analyze_symbol(t, now)
        fe.enrich(r, spot, fargs)
        if r.get("verdict_ok"):
            passed += 1
        print("  " + verdict_row(r))
    print(f"прошли deep_check: {passed}")

    # --- 2) полный скан: гистограмма причин ---
    tickers = fe.fetch_linear_tickers()
    rows = [t for t in tickers
            if t["turnover24h"] >= cli.min_volume * 1e6
            and t["oi_value"] >= cli.min_oi * 1e6]
    rows.sort(key=lambda t: t["funding_rate"], reverse=True)
    rows = rows[: cli.prefilter]
    hist: Counter = Counter()
    ok = 0
    best = []
    for t in rows:
        r = fe.analyze_symbol(t, now)
        fe.enrich(r, spot, fargs)
        if r.get("verdict_ok"):
            ok += 1
            best.append(r)
        for reason in (r.get("fail_reasons") or []):
            hist[reason.split()[0] if reason.startswith("базис") else reason] += 1
        time.sleep(0.02)
    print(f"\nполный скан: {len(rows)} проверено, ГОДЕН → {ok}")
    print("причины отказа:", dict(hist.most_common()))
    best.sort(key=fe.conservative_apy, reverse=True)
    for r in best[:5]:
        print("  ГОДЕН " + verdict_row(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
