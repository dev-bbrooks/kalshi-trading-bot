"""
replay.py — Historical replay / regression testing framework.

Replays recorded market observations through the bot's decision pipeline
to verify behavior and compare against Observatory-optimal strategies.

Usage:
    python3 replay.py                    # Replay all observations
    python3 replay.py --days 7           # Last 7 days only
    python3 replay.py --regime V3_T1_Vol3  # Filter by regime
    python3 replay.py --verbose          # Show per-market details

Reads from botdata.db, never touches Kalshi API.
"""

import json
import argparse
import logging
from datetime import datetime, timezone, timedelta

from config import ET, DB_PATH
from db import (
    get_conn, rows_to_list,
    get_observations_for_simulation,
    get_strategy_for_setup,
)
from strategy import (
    simulate_market, _simulate_one, parse_strategy_key,
    get_recommendation, KALSHI_FEE_RATE,
)

log = logging.getLogger("replay")


def load_observations(days: int = 0, regime: str = None,
                      limit: int = 0) -> list:
    """Load resolved observations for replay."""
    since = None
    if days > 0:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    observations = get_observations_for_simulation(since=since, limit=limit)

    if regime:
        observations = [o for o in observations
                        if o.get("regime_label") == regime]

    return observations


def replay_decision(obs: dict, cfg: dict = None) -> dict:
    """
    Replay what the bot would have decided for this market,
    given the regime context recorded at observation time.
    
    Returns a decision record with:
      - regime gate result
      - which strategy would be selected
      - simulated trade outcome
      - comparison to Observatory-optimal strategy
    """
    if cfg is None:
        cfg = _default_replay_cfg()

    regime_label = obs.get("regime_label", "unknown")
    risk_level = obs.get("risk_level", "unknown")
    hour_et = obs.get("hour_et")
    result = obs.get("market_result")
    snapshots = json.loads(obs.get("price_snapshots", "[]"))
    duration = max(s["t"] for s in snapshots) if snapshots else 0

    if not result or result not in ("yes", "no") or duration < 60:
        return {"skipped": True, "reason": "invalid observation"}

    decision = {
        "ticker": obs.get("ticker"),
        "regime_label": regime_label,
        "risk_level": risk_level,
        "hour_et": hour_et,
        "market_result": result,
        "snapshot_count": len(snapshots),
    }

    # 1. Regime gate check
    from bot import check_regime_gate
    risk_actions = cfg.get("risk_level_actions", {})
    overrides = cfg.get("regime_overrides", {})

    # Simple gate check without strategy_risk (replay doesn't have live data)
    gate_action = overrides.get(regime_label, "default")
    if gate_action == "default":
        defaults = {"low": "normal", "moderate": "normal", "high": "normal",
                    "terrible": "skip", "unknown": "skip"}
        gate_action = risk_actions.get(risk_level, defaults.get(risk_level, "normal"))

    decision["gate_action"] = gate_action
    decision["would_trade"] = gate_action not in ("skip", "data")

    if not decision["would_trade"]:
        decision["outcome"] = "skipped"
        decision["pnl_c"] = 0
    else:
        # 2. Strategy selection — what strategy would the bot use?
        rec = get_recommendation(regime_label, hour_et)
        if rec and cfg.get("auto_strategy_enabled", False):
            strat_key = rec["strategy_key"]
            decision["strategy_source"] = "auto"
        else:
            # Manual strategy from config
            strat_key = _build_replay_strategy_key(cfg)
            decision["strategy_source"] = "manual"

        decision["strategy_key"] = strat_key

        # 3. Simulate the selected strategy
        parsed = parse_strategy_key(strat_key)
        if parsed:
            sim = _simulate_one(
                snapshots, result, duration,
                parsed["entry_time_rule"],
                parsed["entry_price_max"],
                parsed["sell_target"],
            )
            if sim:
                decision["entered"] = sim["entered"]
                decision["won"] = sim["won"]
                decision["pnl_c"] = sim["pnl_c"]
                decision["outcome"] = ("win" if sim["won"] else "loss") if sim["entered"] else "no_entry"
            else:
                decision["entered"] = False
                decision["outcome"] = "sim_error"
                decision["pnl_c"] = 0
        else:
            decision["outcome"] = "bad_strategy_key"
            decision["pnl_c"] = 0

    # 4. Find Observatory-optimal strategy for comparison
    all_sims = simulate_market(obs)
    entered_sims = [s for s in all_sims if s["entered"]]
    if entered_sims:
        best = max(entered_sims, key=lambda s: s["pnl_c"])
        decision["optimal_strategy"] = best["strategy_key"]
        decision["optimal_pnl_c"] = best["pnl_c"]
        decision["optimal_won"] = best["won"]
    else:
        decision["optimal_strategy"] = None
        decision["optimal_pnl_c"] = 0
        decision["optimal_won"] = False

    return decision


def run_replay(days: int = 0, regime: str = None, verbose: bool = False,
               cfg: dict = None) -> dict:
    """
    Run full replay across observations. Returns summary report.
    """
    observations = load_observations(days=days, regime=regime)
    if not observations:
        return {"error": "No observations found", "count": 0}

    decisions = []
    for obs in observations:
        d = replay_decision(obs, cfg=cfg)
        decisions.append(d)
        if verbose and not d.get("skipped"):
            _log_decision(d)

    # Compute summary
    valid = [d for d in decisions if not d.get("skipped")]
    traded = [d for d in valid if d.get("entered")]
    skipped_by_gate = [d for d in valid if d.get("outcome") == "skipped"]
    no_entry = [d for d in valid if d.get("outcome") == "no_entry"]

    wins = [d for d in traded if d.get("won")]
    losses = [d for d in traded if d.get("entered") and not d.get("won")]
    total_pnl = sum(d.get("pnl_c", 0) for d in traded)
    opt_pnl = sum(d.get("optimal_pnl_c", 0) for d in valid)

    report = {
        "observations": len(observations),
        "valid": len(valid),
        "skipped_invalid": len(decisions) - len(valid),
        "skipped_by_gate": len(skipped_by_gate),
        "no_entry": len(no_entry),
        "traded": len(traded),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(traded), 4) if traded else 0,
        "total_pnl_c": total_pnl,
        "avg_pnl_c": round(total_pnl / len(traded), 1) if traded else 0,
        "optimal_total_pnl_c": opt_pnl,
        "optimal_avg_pnl_c": round(opt_pnl / len(valid), 1) if valid else 0,
        "efficiency_pct": round(total_pnl / opt_pnl * 100, 1) if opt_pnl > 0 else 0,
    }

    # Per-regime breakdown
    regimes = {}
    for d in valid:
        r = d.get("regime_label", "unknown")
        if r not in regimes:
            regimes[r] = {"n": 0, "traded": 0, "wins": 0, "pnl_c": 0}
        regimes[r]["n"] += 1
        if d.get("entered"):
            regimes[r]["traded"] += 1
            regimes[r]["pnl_c"] += d.get("pnl_c", 0)
            if d.get("won"):
                regimes[r]["wins"] += 1

    for r, data in regimes.items():
        data["win_rate"] = round(data["wins"] / data["traded"], 4) if data["traded"] else 0
        data["avg_pnl_c"] = round(data["pnl_c"] / data["traded"], 1) if data["traded"] else 0

    report["by_regime"] = regimes
    report["decisions"] = decisions

    return report


def print_report(report: dict):
    """Pretty-print replay report to console."""
    print("\n" + "=" * 60)
    print("  REPLAY REPORT")
    print("=" * 60)

    if "error" in report:
        print(f"\n  Error: {report['error']}")
        return

    print(f"\n  Observations:  {report['observations']}")
    print(f"  Valid:         {report['valid']}")
    print(f"  Gate skipped:  {report['skipped_by_gate']}")
    print(f"  No entry:      {report['no_entry']}")
    print(f"  Traded:        {report['traded']}")
    print(f"\n  Wins:          {report['wins']}")
    print(f"  Losses:        {report['losses']}")
    print(f"  Win rate:      {report['win_rate']:.1%}")
    print(f"\n  Total PnL:     {report['total_pnl_c']:+}¢")
    print(f"  Avg PnL:       {report['avg_pnl_c']:+.1f}¢/trade")
    print(f"\n  Optimal PnL:   {report['optimal_total_pnl_c']:+}¢")
    print(f"  Optimal avg:   {report['optimal_avg_pnl_c']:+.1f}¢/trade")
    print(f"  Efficiency:    {report['efficiency_pct']:.1f}%")

    if report.get("by_regime"):
        print(f"\n  {'Regime':<25} {'N':>4} {'Traded':>7} {'WR':>6} {'PnL':>8}")
        print("  " + "-" * 54)
        for r, data in sorted(report["by_regime"].items(),
                               key=lambda x: x[1]["pnl_c"], reverse=True):
            wr = f"{data['win_rate']:.0%}" if data['traded'] else "—"
            pnl = f"{data['pnl_c']:+}¢" if data['traded'] else "—"
            print(f"  {r:<25} {data['n']:>4} {data['traded']:>7} {wr:>6} {pnl:>8}")

    print("\n" + "=" * 60)


def _default_replay_cfg() -> dict:
    """Load current config from DB for replay."""
    from db import get_config
    from config import DEFAULT_BOT_CONFIG
    cfg = dict(DEFAULT_BOT_CONFIG)
    for key in cfg:
        val = get_config(key)
        if val is not None:
            cfg[key] = val
    return cfg


def _build_replay_strategy_key(cfg: dict) -> str:
    """Build strategy key from config (mirrors bot.build_strategy_key)."""
    timing_map = {0: "early", 5: "mid", 10: "late"}
    delay = int(cfg.get("entry_delay_minutes", 0))
    timing = timing_map.get(delay, "early")
    entry_max = int(cfg.get("entry_price_max_c", 45))
    sell_target = int(cfg.get("sell_target_c", 90))
    sell_str = "hold" if sell_target == 0 else str(sell_target)
    return f"{timing}:{entry_max}:{sell_str}"


def _log_decision(d: dict):
    """Log a single replay decision."""
    pnl = d.get("pnl_c", 0)
    opt = d.get("optimal_pnl_c", 0)
    print(f"  {d.get('ticker', '?'):<30} "
          f"{d.get('regime_label', '?'):<20} "
          f"{d.get('outcome', '?'):<10} "
          f"pnl={pnl:+}¢  opt={opt:+}¢  "
          f"strat={d.get('strategy_key', '?')}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Replay historical markets")
    parser.add_argument("--days", type=int, default=0, help="Last N days (0=all)")
    parser.add_argument("--regime", type=str, default=None, help="Filter by regime label")
    parser.add_argument("--verbose", action="store_true", help="Show per-market details")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S")

    report = run_replay(days=args.days, regime=args.regime, verbose=args.verbose)
    print_report(report)
