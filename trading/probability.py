from __future__ import annotations

import math


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def base_probability(
    price: float, strike: float, sigma: float, mu: float, t: float
) -> float:
    if t <= 0 or sigma <= 0 or price <= 0 or strike <= 0:
        return 1.0 if price >= strike else 0.0
    z = (math.log(price / strike) + (mu - 0.5 * sigma**2) * t) / (
        sigma * math.sqrt(t)
    )
    return _norm_cdf(z)


def distance_adjustment(
    price: float,
    strike: float,
    sigma: float,
    t: float,
    weight: float = 0.15,
) -> float:
    if price <= 0 or sigma <= 0 or t <= 0:
        return 0.0
    distance = (price - strike) / price
    expected_move = sigma * math.sqrt(t)
    if expected_move == 0:
        return 0.0
    return math.tanh(distance / expected_move) * weight


def imbalance_adjustment(
    bid_vol: float, ask_vol: float, weight: float = 0.05
) -> float:
    total = bid_vol + ask_vol
    if total == 0:
        return 0.0
    imbalance = (bid_vol - ask_vol) / total
    return imbalance * weight


def microprice_adjustment(
    microprice: float, midprice: float, weight: float = 10.0
) -> float:
    if midprice == 0:
        return 0.0
    return (microprice - midprice) / midprice * weight


def momentum_adjustment(
    returns: list[float], weight: float = 5.0
) -> float:
    if not returns:
        return 0.0
    return (sum(returns) / len(returns)) * weight


def final_probability(
    price: float,
    strike: float,
    sigma: float,
    mu: float,
    t: float,
    bid_vol: float,
    ask_vol: float,
    microprice: float,
    midprice: float,
    returns: list[float],
) -> float:
    p = base_probability(price, strike, sigma, mu, t)
    p += distance_adjustment(price, strike, sigma, t)
    p += imbalance_adjustment(bid_vol, ask_vol)
    p += microprice_adjustment(microprice, midprice)
    p += momentum_adjustment(returns)
    return max(0.0, min(1.0, p))


def late_market_probability(
    price: float, strike: float, sigma: float, t: float
) -> float | None:
    if t <= 0:
        return 1.0 if price >= strike else 0.0
    distance = abs(price - strike) / price
    expected_move = sigma * math.sqrt(t)
    if expected_move > 0 and distance > expected_move * 1.5:
        return 0.98 if price > strike else 0.02
    return None


def compute_edge(model_prob: float, market_prob: float) -> float:
    return model_prob - market_prob


def kelly_size(
    edge: float, odds: float, fraction: float = 0.25
) -> float:
    if odds <= 0:
        return 0.0
    f = edge / odds
    return max(0.0, f * fraction)
