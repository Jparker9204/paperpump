"""Fill model (pure functions, easy to test).

Pool approximation: constant product x*y=k. The SOL side of the pool is about half
of the reported USD liquidity. This is an estimate: Pump.fun-style bonding curves and
concentrated-liquidity pools behave differently.
"""
from dataclasses import dataclass


@dataclass
class Fill:
    side: str            # "buy" or "sell"
    sol: float           # buy: SOL spent (before fees); sell: SOL received (after fees)
    tokens: float        # tokens received (buy) or sold (sell)
    naive: float         # what a naive paper fill would give (tokens for buy, SOL for sell)
    fee_sol: float       # swap fee + priority fee
    impact_pct: float    # price impact vs quoted price
    drift_pct: float     # price move during simulated delay
    price_quote: float   # SOL per token at click time
    price_fill: float    # SOL per token after delay
    priority_fee: float = 0.0


def sol_reserve(liquidity_usd: float, sol_usd: float) -> float:
    if liquidity_usd <= 0 or sol_usd <= 0:
        return 0.0
    return liquidity_usd / 2 / sol_usd


def simulate_buy(sol_in, price_quote, price_after_delay, reserve_sol, fee_rate=0.01, priority_fee=0.001) -> Fill:
    if sol_in <= 0 or price_quote <= 0 or price_after_delay <= 0:
        raise ValueError("amount and prices must be positive")
    naive = sol_in / price_quote
    net = sol_in * (1 - fee_rate)
    if reserve_sol > 0:
        token_reserve = reserve_sol / price_after_delay
        tokens = token_reserve * net / (reserve_sol + net)
    else:
        tokens = net / price_after_delay
    no_impact = net / price_after_delay
    return Fill("buy", sol_in, tokens, naive, sol_in * fee_rate + priority_fee,
                (1 - tokens / no_impact) * 100, (price_after_delay / price_quote - 1) * 100,
                price_quote, price_after_delay, priority_fee)


def simulate_sell(tokens_in, price_quote, price_after_delay, reserve_sol, fee_rate=0.01, priority_fee=0.001) -> Fill:
    if tokens_in <= 0 or price_quote <= 0 or price_after_delay <= 0:
        raise ValueError("amount and prices must be positive")
    naive = tokens_in * price_quote
    if reserve_sol > 0:
        token_reserve = reserve_sol / price_after_delay
        gross = reserve_sol * tokens_in / (token_reserve + tokens_in)
    else:
        gross = tokens_in * price_after_delay
    no_impact = tokens_in * price_after_delay
    fee = gross * fee_rate
    return Fill("sell", gross - fee - priority_fee, tokens_in, naive, fee + priority_fee,
                (1 - gross / no_impact) * 100, (price_after_delay / price_quote - 1) * 100,
                price_quote, price_after_delay, priority_fee)


def pnl(cost_sol: float, value_sol: float) -> tuple[float, float]:
    """Return (PnL in SOL, PnL in %)."""
    d = value_sol - cost_sol
    return d, (d / cost_sol * 100 if cost_sol else 0.0)
