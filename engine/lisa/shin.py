"""Shin's method — mathematically strip the bookmaker margin (de-vig).

Model (Shin 1993, "Prices of State-Contingent Claims with Insider Traders, and
the Favourite-Longshot Bias", Economic Journal 103): a bookmaker prices against
two populations — insiders who only bet when they hold an edge, and noise
bettors. The fraction of insider money ``z`` is baked into every line, and its
effect is not uniform: the margin is skewed hardest onto longshots, which is
exactly the favourite-longshot bias we want to remove.

Algorithm (verified against the reference implementation ``mberk/shin``):
  * inputs: decimal odds o_i >= 1, n outcomes (n >= 2)
  * inverse odds io_i = 1/o_i,  S = sum(io_i)
  * n == 2: closed-form  z = ((S-1)(d^2 - S)) / (S (d^2 - 1)),  d = io_0 - io_1
  * n >= 3: fixed point  z <- (sum_i sqrt(z^2 + 4(1-z) io_i^2 / S) - 2) / (n - 2)
  * true probs: p_i = (sqrt(z^2 + 4(1-z) io_i^2 / S) - z) / (2 (1 - z))

Degenerate or numerically unstable inputs fall back to simple proportional
de-vig (p_i = io_i / S) so the pipeline can always proceed — a recoverable
degradation, never an exception storm.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

DEFAULT_MAX_ITERATIONS = 1000
DEFAULT_TOLERANCE = 1e-12
MAX_Z = 0.5  # insider share above this is not credible -> distrust model


class ShinValidationError(ValueError):
    """Raised when odds fall outside the supported, sane domain."""


@dataclass(frozen=True)
class ShinResult:
    probabilities: tuple[float, ...]
    z: float
    method: str  # "closed" | "iterative" | "proportional"
    iterations: int
    converged: bool


def validate_odds(odds, lo: float = 1.01, hi: float = 1001.0) -> list[float]:
    if len(odds) < 2:
        raise ShinValidationError(f"need at least 2 outcomes, got {len(odds)}")
    out: list[float] = []
    for o in odds:
        o = float(o)
        if not math.isfinite(o) or o < lo or o > hi:
            raise ShinValidationError(f"odds {o!r} outside sane range [{lo}, {hi}]")
        out.append(o)
    return out


def _proportional(inv: list[float]) -> ShinResult:
    total = sum(inv)
    if total <= 0.0 or not math.isfinite(total):
        raise ShinValidationError("degenerate inverse-odds sum")
    return ShinResult(
        probabilities=tuple(v / total for v in inv),
        z=0.0,
        method="proportional",
        iterations=0,
        converged=True,
    )


def _normalise(p: list[float]) -> list[float]:
    total = sum(p)
    if total <= 0.0 or not math.isfinite(total):
        raise ShinValidationError("degenerate probability sum")
    if abs(total - 1.0) > 1e-9:
        p = [v / total for v in p]
    return p


def _finish(inv: list[float], s: float, z: float, method: str,
            iterations: int, converged: bool) -> ShinResult:
    """Validate/clamp z and compute true probabilities from the Shin model."""
    if not math.isfinite(z):
        return _proportional(inv)
    if z < 0.0:
        z = 0.0
    if z >= MAX_Z:  # model not credible: fall back to proportional
        return _proportional(inv)
    denom = 2.0 * (1.0 - z)
    if denom <= 0.0:
        return _proportional(inv)
    p: list[float] = []
    for io in inv:
        radicand = z * z + 4.0 * (1.0 - z) * io * io / s
        if radicand < 0.0:
            return _proportional(inv)
        p.append((math.sqrt(radicand) - z) / denom)
    p = _normalise(p)
    if any(not math.isfinite(v) or v <= 0.0 or v >= 1.0 for v in p):
        return _proportional(inv)
    return ShinResult(probabilities=tuple(p), z=z, method=method,
                      iterations=iterations, converged=converged)


def _fixed_point(inv: list[float], s: float, n: int,
                 max_iterations: int, tolerance: float) -> tuple[float, int, bool]:
    z = 0.0
    delta = float("inf")
    iterations = 0
    while delta > tolerance and iterations < max_iterations:
        z0 = z
        total = sum(math.sqrt(z * z + 4.0 * (1.0 - z) * io * io / s) for io in inv)
        z = (total - 2.0) / (n - 2.0)
        delta = abs(z - z0)
        iterations += 1
        if not math.isfinite(z):
            break
    return z, iterations, delta <= tolerance


def shin_probabilities(odds, *, lo: float = 1.01, hi: float = 1001.0,
                       max_iterations: int = DEFAULT_MAX_ITERATIONS,
                       tolerance: float = DEFAULT_TOLERANCE) -> ShinResult:
    """Strip the vig from ``odds`` (decimal) and return true probabilities."""
    odds = validate_odds(odds, lo=lo, hi=hi)
    inv = [1.0 / o for o in odds]
    s = sum(inv)
    n = len(odds)

    if n == 2:
        d = inv[0] - inv[1]
        denom = s * (d * d - 1.0)
        if abs(denom) < 1e-12:
            # flat market (or pathological): z = 0 == proportional
            return _finish(inv, s, 0.0, "closed", 0, True)
        z = ((s - 1.0) * (d * d - s)) / denom
        return _finish(inv, s, z, "closed", 0, True)

    z, iterations, converged = _fixed_point(inv, s, n, max_iterations, tolerance)
    return _finish(inv, s, z, "iterative", iterations, converged)