"""Statistical utilities for rare-event estimation in FI-01 campaigns.

Implements the mathematical engine required by Gate G1 (Framework v8.0):
- UCB_{1-alpha}(P_UE) for Safety
- LCB_{1-alpha}(1 - P_unavail) for Liveness
- beta_shared -> rho_bar -> N_eff for Common Cause
- Relative precision and ESS diagnostics for convergence

G0 SLA constants (sealed, read-only):
    epsilon_target = 1e-11
    A_target       = 0.9999
    alpha          = 0.05
    T_star         = 10 years
    N_eff_ratio    >= 0.8
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy import stats as sp_stats

# ---------------------------------------------------------------------------
# G0 Sealed SLA Constants (matriz_auditoria §2.5)
# ---------------------------------------------------------------------------
EPSILON_TARGET: float = 1e-11
A_TARGET: float = 0.9999
ALPHA: float = 0.05
Z_ALPHA: float = float(sp_stats.norm.ppf(1.0 - ALPHA))  # 1.6449 for one-sided 95%
N_EFF_RATIO_MIN: float = 0.8
BETA_SHARED_MAX: float = 1e-6


@dataclass(slots=True)
class ImportanceSamplingSummary:
    """Summary statistics for an Importance Sampling run.

    Attributes:
        probability_estimate: Estimated event probability (P_UE point estimate).
        variance_estimate: Estimated variance of the probability estimator.
        std_error: Standard error of the estimator.
        ucb: One-sided upper confidence bound UCB_{1-alpha}(P_UE).
        lcb_availability: Lower confidence bound LCB_{1-alpha}(1 - P_unavail).
        ess: Effective sample size induced by weighting.
        ess_ratio: ESS / N (weight degeneracy diagnostic).
        proposal_fail_rate: Unweighted failure rate under proposal sampling.
        relative_precision: z * se / estimate (convergence diagnostic).
    """

    probability_estimate: float
    variance_estimate: float
    std_error: float
    ucb: float
    lcb_availability: float
    ess: float
    ess_ratio: float
    proposal_fail_rate: float
    relative_precision: float


# ---------------------------------------------------------------------------
# Core IS estimators
# ---------------------------------------------------------------------------

def effective_sample_size(weights: np.ndarray) -> float:
    """Compute effective sample size for importance weights.

    ESS = (sum w_i)^2 / sum(w_i^2)

    Args:
        weights: Non-negative importance weights.

    Returns:
        Effective sample size value.
    """
    numerator = float(np.sum(weights) ** 2)
    denominator = float(np.sum(weights**2))
    if denominator == 0.0:
        return 0.0
    return numerator / denominator


def importance_sampling_estimate(
    event_indicator: np.ndarray,
    weights: np.ndarray,
    alpha: float = ALPHA,
) -> ImportanceSamplingSummary:
    """Estimate event probability and confidence bounds via Importance Sampling.

    Computes simultaneously:
    - UCB_{1-alpha}(P_UE) for Safety (Framework v8.0 §7)
    - LCB_{1-alpha}(1 - P_unavail) for Liveness (Framework v8.0 §7)

    Args:
        event_indicator: Binary event indicator array (1 = failure).
        weights: Likelihood-ratio weights.
        alpha: Statistical confidence level (default from G0 SLA).

    Returns:
        ImportanceSamplingSummary with point and dispersion estimates.
    """
    if event_indicator.ndim != 1 or weights.ndim != 1:
        raise ValueError("event_indicator and weights must be one-dimensional arrays")
    if event_indicator.size != weights.size:
        raise ValueError("event_indicator and weights must have the same size")

    z = float(sp_stats.norm.ppf(1.0 - alpha))

    weighted_samples = event_indicator.astype(np.float64) * weights
    count = weighted_samples.size
    probability_estimate = float(np.mean(weighted_samples))

    if count > 1:
        variance_estimate = float(np.var(weighted_samples, ddof=1) / count)
    else:
        variance_estimate = 0.0

    std_error = float(np.sqrt(max(variance_estimate, 0.0)))
    ucb = probability_estimate + z * std_error
    lcb_availability = 1.0 - ucb

    ess = effective_sample_size(weights)
    ess_ratio = ess / count if count > 0 else 0.0

    if probability_estimate > 0.0:
        relative_precision = z * std_error / probability_estimate
    else:
        relative_precision = float("inf")

    return ImportanceSamplingSummary(
        probability_estimate=probability_estimate,
        variance_estimate=variance_estimate,
        std_error=std_error,
        ucb=float(ucb),
        lcb_availability=float(lcb_availability),
        ess=float(ess),
        ess_ratio=float(ess_ratio),
        proposal_fail_rate=float(np.mean(event_indicator.astype(np.float64))),
        relative_precision=float(relative_precision),
    )


# ---------------------------------------------------------------------------
# Common Cause: beta_shared -> rho_bar -> N_eff (Framework v8.0 §4)
# ---------------------------------------------------------------------------

def calculate_rho_bar(
    beta_components: dict[str, float],
    coupling_coefficients: dict[str, float],
    rho_floor: float = 0.01,
) -> float:
    """Calculate residual correlation from common cause decomposition.

    rho_bar = rho_floor + sum_j(k_j * beta_j)

    Args:
        beta_components: Dict mapping beta component names to values
            (substrato, amostra, pipeline, modelo, chave, adversarial).
        coupling_coefficients: Dict mapping component names to k_j >= 0.
        rho_floor: Baseline residual correlation floor.

    Returns:
        Residual correlation rho_bar.
    """
    rho = rho_floor
    for component, beta_val in beta_components.items():
        k_j = coupling_coefficients.get(component, 0.0)
        if k_j < 0.0:
            raise ValueError(f"Coupling coefficient k_{component} must be >= 0, got {k_j}")
        rho += k_j * beta_val
    return min(rho, 1.0)


def calculate_n_eff(n_nodes: int, rho_bar: float) -> float:
    """Calculate effective number of nodes from residual correlation.

    N_eff = N / (1 + (N-1) * rho_bar)

    Args:
        n_nodes: Nominal number of nodes (N).
        rho_bar: Residual correlation.

    Returns:
        Effective number of nodes (N_eff).
    """
    if n_nodes <= 0:
        return 0.0
    rho_bar = max(rho_bar, 0.0)
    return float(n_nodes / (1.0 + (n_nodes - 1.0) * rho_bar))


def beta_shared_total(beta_components: dict[str, float]) -> float:
    """Sum all beta_shared components.

    beta_shared = beta_substrato + beta_amostra + beta_pipeline
                + beta_modelo + beta_chave + beta_adversarial

    Args:
        beta_components: Dict mapping component names to values.

    Returns:
        Total beta_shared.
    """
    return sum(beta_components.values())


# ---------------------------------------------------------------------------
# Epsilon budget closure (Framework v8.0 §3)
# ---------------------------------------------------------------------------

def check_epsilon_budget(
    epsilon_estimates: dict[str, float],
    epsilon_target: float = EPSILON_TARGET,
) -> dict[str, Any]:
    """Check if epsilon budget closes: sum(epsilon_i) <= epsilon_target.

    Args:
        epsilon_estimates: Dict of {domain: estimated_value} for the 7 domains.
        epsilon_target: Maximum tolerable undetected error (from G0 SLA).

    Returns:
        Dict with total, target, margin, pass status, and per-domain breakdown.
    """
    total = sum(epsilon_estimates.values())
    margin = epsilon_target - total
    return {
        "epsilon_total": total,
        "epsilon_target": epsilon_target,
        "margin": margin,
        "margin_factor": epsilon_target / total if total > 0 else float("inf"),
        "pass": total <= epsilon_target,
        "per_domain": dict(epsilon_estimates),
    }


# ---------------------------------------------------------------------------
# Convergence series
# ---------------------------------------------------------------------------

def build_convergence_series(
    event_indicator: np.ndarray,
    weights: np.ndarray,
    checkpoint_interval: int,
    alpha: float = ALPHA,
) -> list[dict[str, float]]:
    """Build prefix-wise convergence diagnostics for estimator variance.

    Args:
        event_indicator: Binary event indicator array.
        weights: Importance weights.
        checkpoint_interval: Number of samples between checkpoints.
        alpha: Statistical confidence level.

    Returns:
        List of convergence checkpoints with estimator values.
    """
    if checkpoint_interval < 1:
        raise ValueError("checkpoint_interval must be >= 1")

    z = float(sp_stats.norm.ppf(1.0 - alpha))
    weighted = event_indicator.astype(np.float64) * weights
    checkpoints: list[dict[str, float]] = []
    total = weighted.size

    def _checkpoint(prefix: np.ndarray, n: int) -> dict[str, float]:
        estimate = float(np.mean(prefix))
        variance = float(np.var(prefix, ddof=1) / n) if n > 1 else 0.0
        std_error = float(np.sqrt(max(variance, 0.0)))
        ucb = estimate + z * std_error
        rel_prec = (z * std_error / estimate) if estimate > 0 else float("inf")
        return {
            "sample_count": float(n),
            "estimate": estimate,
            "variance": variance,
            "std_error": std_error,
            "ucb": ucb,
            "relative_precision": rel_prec,
        }

    for sample_count in range(checkpoint_interval, total + 1, checkpoint_interval):
        checkpoints.append(_checkpoint(weighted[:sample_count], sample_count))

    if total % checkpoint_interval != 0:
        checkpoints.append(_checkpoint(weighted, total))

    return checkpoints


# ---------------------------------------------------------------------------
# G1 Go/No-Go verdict (Framework v8.0 §7)
# ---------------------------------------------------------------------------

def g1_verdict(
    summary: ImportanceSamplingSummary,
    epsilon_budget: dict[str, Any],
    n_eff: float,
    n_nodes: int,
    beta_shared: float,
    ess_min: float = 0.2,
    r_target: float = 0.05,
) -> dict[str, Any]:
    """Evaluate Gate G1 acceptance criteria.

    Checks simultaneously (Framework v8.0 §7):
    1. UCB_{1-alpha}(P_UE(T*)) <= epsilon_target
    2. LCB_{1-alpha}(1 - P_unavail(T*)) >= A_target
    3. N_eff / N >= 0.8
    4. beta_shared <= 1e-6
    5. ESS / N >= ESS_min (weight degeneracy)
    6. Relative precision <= r_target (convergence)

    Args:
        summary: IS estimation summary.
        epsilon_budget: Output from check_epsilon_budget.
        n_eff: Effective number of nodes.
        n_nodes: Nominal number of nodes.
        beta_shared: Total beta_shared value.
        ess_min: Minimum ESS ratio threshold.
        r_target: Maximum relative precision for convergence.

    Returns:
        Dict with per-criterion pass/fail and overall verdict.
    """
    n_eff_ratio = n_eff / n_nodes if n_nodes > 0 else 0.0

    criteria = {
        "safety_ucb_pass": bool(summary.ucb <= EPSILON_TARGET),
        "safety_ucb_value": summary.ucb,
        "safety_ucb_target": EPSILON_TARGET,
        "liveness_lcb_pass": bool(summary.lcb_availability >= A_TARGET),
        "liveness_lcb_value": summary.lcb_availability,
        "liveness_lcb_target": A_TARGET,
        "epsilon_budget_pass": bool(epsilon_budget["pass"]),
        "epsilon_budget_total": epsilon_budget["epsilon_total"],
        "n_eff_pass": bool(n_eff_ratio >= N_EFF_RATIO_MIN),
        "n_eff_value": n_eff,
        "n_eff_ratio": n_eff_ratio,
        "beta_shared_pass": bool(beta_shared <= BETA_SHARED_MAX),
        "beta_shared_value": beta_shared,
        "ess_ratio_pass": bool(summary.ess_ratio >= ess_min),
        "ess_ratio_value": summary.ess_ratio,
        "ess_ratio_target": ess_min,
        "convergence_pass": bool(summary.relative_precision <= r_target),
        "relative_precision": summary.relative_precision,
        "r_target": r_target,
    }

    all_pass = bool(all(v for k, v in criteria.items() if k.endswith("_pass")))
    criteria["g1_go"] = all_pass

    return criteria
