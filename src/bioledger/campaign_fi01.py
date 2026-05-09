"""FI-01 campaign runner for burst-error fault injection.

Campaign scope (espec_G1 §4.2, FI-01):
    Target threat: BIO-02 (Burst errors and structural rearrangements)
    Primary PO: PO-1 (Coding and Synchronization)
    Proposal: Proposal-BIO tilted toward epsilon_ch and epsilon_sync
    Convergence: ESS/N >= 0.2, relative precision <= 0.05

Memory-safe design:
    Large sample counts are processed in batches (default 10k per batch).
    Per-batch arrays are discarded after accumulating sufficient statistics
    (Welford online mean/variance, ESS sums, epsilon domain sums).
    Peak RAM usage is O(batch_size * sequence_length), not O(N * sequence_length).

Output: fi01_summary.json structured as evidence dossier mapped to PO-1..PO-5
with G1 go/no-go verdict per Framework v8.0 §7.
"""

from __future__ import annotations

import csv
import gc
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib
import numpy as np

from bioledger.config import ChannelParameters, ImportanceProposal, SimulationParameters
from bioledger.simulator import BioChannelSimulator
from bioledger.statistics import (
    ALPHA,
    A_TARGET,
    EPSILON_TARGET,
    ImportanceSamplingSummary,
    Z_ALPHA,
    beta_shared_total,
    calculate_n_eff,
    calculate_rho_bar,
    check_epsilon_budget,
    g1_verdict,
)

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


# ---------------------------------------------------------------------------
# FI-01 Campaign constants (espec_G1 §6.3)
# ---------------------------------------------------------------------------
ESS_MIN: float = 0.2
R_TARGET: float = 0.05
DEFAULT_BATCH_SIZE: int = 10_000

# G1 initial estimates for common cause (modelo_ameacas §5)
DEFAULT_BETA_COMPONENTS: dict[str, float] = {
    "substrato": 1e-7,
    "amostra": 5e-8,
    "pipeline": 2e-7,
    "modelo": 3e-7,
    "chave": 5e-8,
    "adversarial": 1e-7,
}
DEFAULT_COUPLING_COEFFICIENTS: dict[str, float] = {
    "substrato": 0.15,
    "amostra": 0.10,
    "pipeline": 0.20,
    "modelo": 0.25,
    "chave": 0.05,
    "adversarial": 0.10,
}
DEFAULT_N_NODES: int = 10
DEFAULT_RHO_FLOOR: float = 0.01

# 7 epsilon domain names for budget tracking
_EPSILON_DOMAINS = (
    "epsilon_ch", "epsilon_sync", "epsilon_ver",
    "epsilon_cons", "epsilon_key", "epsilon_ops", "epsilon_adv",
)


@dataclass(slots=True)
class _OnlineAccumulator:
    """Welford-style online accumulator for batched IS estimation.

    Tracks sufficient statistics to compute mean, variance, ESS,
    and per-domain epsilon estimates without holding all samples in memory.
    """

    count: int = 0
    # Welford accumulators for weighted indicator (w_i * I_i)
    mean: float = 0.0
    m2: float = 0.0
    # ESS accumulators: sum(w_i), sum(w_i^2)
    sum_w: float = 0.0
    sum_w2: float = 0.0
    # Proposal fail count (unweighted)
    fail_count: int = 0
    # Per-domain weighted sums
    eps_sums: dict[str, float] | None = None
    # Convergence checkpoints
    checkpoints: list[dict[str, float]] | None = None

    def __post_init__(self) -> None:
        if self.eps_sums is None:
            self.eps_sums = {d: 0.0 for d in _EPSILON_DOMAINS}
        if self.checkpoints is None:
            self.checkpoints = []


def _update_accumulator(
    acc: _OnlineAccumulator,
    decode_fail: np.ndarray,
    weights: np.ndarray,
    result: Any,
    checkpoint_interval: int,
) -> None:
    """Update accumulator with a single batch of results.

    Uses Welford's online algorithm for numerically stable
    mean and variance computation.

    Args:
        acc: Accumulator to update in-place.
        decode_fail: Binary decode failure indicators for this batch.
        weights: IS likelihood-ratio weights for this batch.
        result: ChannelSimulationResult for epsilon domain extraction.
        checkpoint_interval: Interval for convergence checkpoints.
    """
    weighted = decode_fail.astype(np.float64) * weights
    batch_n = weighted.size

    # ESS accumulators
    acc.sum_w += float(np.sum(weights))
    acc.sum_w2 += float(np.sum(weights ** 2))

    # Proposal fail count
    acc.fail_count += int(np.sum(decode_fail))

    # Per-domain weighted sums
    for domain in _EPSILON_DOMAINS:
        domain_arr = getattr(result, domain)
        acc.eps_sums[domain] += float(np.sum(domain_arr.astype(np.float64) * weights))

    # Welford update (sample-by-sample for numerical stability)
    for x in weighted:
        acc.count += 1
        delta = x - acc.mean
        acc.mean += delta / acc.count
        delta2 = x - acc.mean
        acc.m2 += delta * delta2

        # Emit convergence checkpoint
        if acc.count % checkpoint_interval == 0:
            _emit_checkpoint(acc)


def _emit_checkpoint(acc: _OnlineAccumulator) -> None:
    """Record a convergence checkpoint at the current accumulator state."""
    n = acc.count
    estimate = acc.mean
    variance = (acc.m2 / (n - 1)) / n if n > 1 else 0.0
    std_error = float(np.sqrt(max(variance, 0.0)))
    ucb = estimate + Z_ALPHA * std_error
    rel_prec = (Z_ALPHA * std_error / estimate) if estimate > 0 else float("inf")

    acc.checkpoints.append({
        "sample_count": float(n),
        "estimate": estimate,
        "variance": variance,
        "std_error": std_error,
        "ucb": ucb,
        "relative_precision": rel_prec,
    })


def _finalize_summary(acc: _OnlineAccumulator) -> ImportanceSamplingSummary:
    """Convert accumulated statistics into an ImportanceSamplingSummary.

    Args:
        acc: Finalized accumulator.

    Returns:
        ImportanceSamplingSummary computed from accumulated sufficient statistics.
    """
    n = acc.count
    probability_estimate = acc.mean

    if n > 1:
        variance_estimate = (acc.m2 / (n - 1)) / n
    else:
        variance_estimate = 0.0

    std_error = float(np.sqrt(max(variance_estimate, 0.0)))
    ucb = probability_estimate + Z_ALPHA * std_error
    lcb_availability = 1.0 - ucb

    ess = (acc.sum_w ** 2) / acc.sum_w2 if acc.sum_w2 > 0 else 0.0
    ess_ratio = ess / n if n > 0 else 0.0
    proposal_fail_rate = acc.fail_count / n if n > 0 else 0.0

    if probability_estimate > 0.0:
        relative_precision = Z_ALPHA * std_error / probability_estimate
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
        proposal_fail_rate=float(proposal_fail_rate),
        relative_precision=float(relative_precision),
    )


class FI01Campaign:
    """Executes FI-01 burst-error campaign with memory-safe batch processing.

    This campaign targets BIO-02 (burst errors / structural rearrangements)
    and validates PO-1 (Coding and Synchronization) with Importance Sampling
    focused on epsilon_ch and epsilon_sync domains.

    Large sample counts are automatically split into batches to avoid
    OOM conditions. Per-batch arrays are discarded after accumulation.

    Args:
        base_parameters: Base channel parameters.
        proposal_parameters: Proposal distribution parameters.
        simulation_parameters: Runtime controls for campaign execution.
        batch_size: Maximum samples per batch (default 10k).
    """

    def __init__(
        self,
        base_parameters: ChannelParameters,
        proposal_parameters: ImportanceProposal,
        simulation_parameters: SimulationParameters,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self.base_parameters = base_parameters.normalized()
        self.proposal_parameters = proposal_parameters
        self.simulation_parameters = simulation_parameters.normalized()
        self.batch_size = max(batch_size, 100)

    def run(self, output_root: str | Path) -> dict[str, Any]:
        """Run the FI-01 campaign with batched execution.

        Args:
            output_root: Root folder where result folders are created.

        Returns:
            Dictionary containing summary metrics, PO evidence, and G1 verdict.
        """
        output_root = Path(output_root)
        result_dir = self._make_result_dir(output_root)

        total_samples = self.simulation_parameters.sample_count
        seq_len = self.simulation_parameters.sequence_length
        checkpoint_interval = self.simulation_parameters.checkpoint_interval

        # -----------------------------------------------------------
        # Batched simulation with online accumulation
        # -----------------------------------------------------------
        acc = _OnlineAccumulator()
        samples_csv_path = result_dir / "fi01_samples.csv"
        csv_handle = samples_csv_path.open("w", newline="", encoding="utf-8")
        csv_writer = csv.writer(csv_handle)
        csv_writer.writerow([
            "sample_id", "decode_fail", "bit_error_rate",
            "max_burst_length", "bad_state_fraction", "noise_rms",
            "weight", "log_weight", "weighted_indicator",
            "eps_ch", "eps_sync", "eps_ver",
        ])

        processed = 0
        batch_idx = 0
        base_seed = self.simulation_parameters.seed

        n_batches = (total_samples + self.batch_size - 1) // self.batch_size

        while processed < total_samples:
            batch_n = min(self.batch_size, total_samples - processed)

            # Deterministic per-batch seed for reproducibility
            batch_seed = base_seed + batch_idx

            batch_sim_params = SimulationParameters(
                sample_count=batch_n,
                sequence_length=seq_len,
                decode_fail_ber_threshold=self.simulation_parameters.decode_fail_ber_threshold,
                decode_fail_burst_threshold=self.simulation_parameters.decode_fail_burst_threshold,
                checkpoint_interval=checkpoint_interval,
                seed=batch_seed,
            ).normalized()

            simulator = BioChannelSimulator(
                base_parameters=self.base_parameters,
                proposal_parameters=self.proposal_parameters,
                seed=batch_seed,
            )
            result = simulator.simulate_batch(batch_sim_params)

            # Stream CSV rows for this batch
            weighted_ind = result.decode_fail.astype(np.float64) * result.weights
            for sid in range(batch_n):
                csv_writer.writerow([
                    processed + sid,
                    int(result.decode_fail[sid]),
                    float(result.bit_error_rate[sid]),
                    int(result.max_burst_length[sid]),
                    float(result.bad_state_fraction[sid]),
                    float(result.noise_rms[sid]),
                    float(result.weights[sid]),
                    float(result.log_weights[sid]),
                    float(weighted_ind[sid]),
                    int(result.epsilon_ch[sid]),
                    int(result.epsilon_sync[sid]),
                    int(result.epsilon_ver[sid]),
                ])

            # Accumulate sufficient statistics
            _update_accumulator(
                acc, result.decode_fail, result.weights,
                result, checkpoint_interval,
            )

            processed += batch_n
            batch_idx += 1

            # Progress reporting
            pct = 100.0 * processed / total_samples
            print(
                f"  [FI-01] Batch {batch_idx}/{n_batches} done "
                f"({processed:,}/{total_samples:,} samples, {pct:.1f}%)",
                file=sys.stderr,
                flush=True,
            )

            # Release batch memory
            del result, simulator, weighted_ind, batch_sim_params
            gc.collect()

        csv_handle.close()

        # Emit final checkpoint if not already at a boundary
        if acc.count % checkpoint_interval != 0:
            _emit_checkpoint(acc)

        # -----------------------------------------------------------
        # Finalize statistics from accumulators
        # -----------------------------------------------------------
        summary_stats = _finalize_summary(acc)

        # Per-domain epsilon estimates
        n = acc.count
        eps_estimates = {d: acc.eps_sums[d] / n for d in _EPSILON_DOMAINS}

        # IS efficiency diagnostics
        mc_variance_under_proposal = (
            summary_stats.proposal_fail_rate
            * (1.0 - summary_stats.proposal_fail_rate)
            / total_samples
        )
        if summary_stats.variance_estimate > 0.0:
            efficiency_gain = mc_variance_under_proposal / summary_stats.variance_estimate
        else:
            efficiency_gain = None

        # -----------------------------------------------------------
        # Common cause: beta -> rho -> N_eff (Framework v8.0 §4)
        # -----------------------------------------------------------
        rho_bar = calculate_rho_bar(
            beta_components=DEFAULT_BETA_COMPONENTS,
            coupling_coefficients=DEFAULT_COUPLING_COEFFICIENTS,
            rho_floor=DEFAULT_RHO_FLOOR,
        )
        n_eff = calculate_n_eff(DEFAULT_N_NODES, rho_bar)
        b_shared = beta_shared_total(DEFAULT_BETA_COMPONENTS)

        # Epsilon budget closure
        epsilon_budget = check_epsilon_budget(eps_estimates)

        # -----------------------------------------------------------
        # G1 Go/No-Go verdict (Framework v8.0 §7)
        # -----------------------------------------------------------
        verdict = g1_verdict(
            summary=summary_stats,
            epsilon_budget=epsilon_budget,
            n_eff=n_eff,
            n_nodes=DEFAULT_N_NODES,
            beta_shared=b_shared,
            ess_min=ESS_MIN,
            r_target=R_TARGET,
        )

        # -----------------------------------------------------------
        # Persist remaining artifacts
        # -----------------------------------------------------------
        convergence_csv_path = result_dir / "fi01_convergence.csv"
        summary_json_path = result_dir / "fi01_summary.json"
        variance_plot_path = result_dir / "fi01_variance_convergence.png"

        self._write_convergence_csv(convergence_csv_path, acc.checkpoints)
        self._plot_variance_convergence(variance_plot_path, acc.checkpoints)

        # -----------------------------------------------------------
        # Evidence dossier (mapped to PO-1..PO-5)
        # -----------------------------------------------------------
        payload = {
            "campaign_id": "FI-01",
            "campaign_target": "BIO-02 (Burst errors / structural rearrangements)",
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "execution": {
                "total_samples": total_samples,
                "batch_size": self.batch_size,
                "n_batches": batch_idx,
                "mode": "batched_online_accumulation",
            },
            "sla_g0": {
                "alpha": ALPHA,
                "epsilon_target": EPSILON_TARGET,
                "a_target": A_TARGET,
            },
            "proof_obligations": {
                "PO-1": {
                    "description": "Coding & Synchronization (epsilon_ch + epsilon_sync)",
                    "ucb_decode_fail": summary_stats.ucb,
                    "target": EPSILON_TARGET,
                    "pass": bool(summary_stats.ucb <= EPSILON_TARGET),
                    "epsilon_ch_estimate": eps_estimates["epsilon_ch"],
                    "epsilon_sync_estimate": eps_estimates["epsilon_sync"],
                },
                "PO-2": {
                    "description": "Hybrid Verifier & Fail-Stop (epsilon_ver)",
                    "epsilon_ver_estimate": eps_estimates["epsilon_ver"],
                    "p_false_accept_design": 1e-12,
                    "fail_stop_dominant": bool(eps_estimates["epsilon_ver"] < summary_stats.probability_estimate * 0.01),
                },
                "PO-3": {
                    "description": "Consensus & N_eff (epsilon_cons)",
                    "n_nodes_nominal": DEFAULT_N_NODES,
                    "rho_bar": rho_bar,
                    "n_eff": n_eff,
                    "n_eff_ratio": n_eff / DEFAULT_N_NODES,
                    "beta_shared": b_shared,
                    "epsilon_cons_estimate": eps_estimates["epsilon_cons"],
                },
                "PO-4": {
                    "description": "Key Governance (epsilon_key)",
                    "epsilon_key_estimate": eps_estimates["epsilon_key"],
                },
                "PO-5": {
                    "description": "Operational & Adversarial (epsilon_ops, epsilon_adv)",
                    "epsilon_ops_estimate": eps_estimates["epsilon_ops"],
                    "epsilon_adv_estimate": eps_estimates["epsilon_adv"],
                    "lcb_availability": summary_stats.lcb_availability,
                    "a_target": A_TARGET,
                    "pass": bool(summary_stats.lcb_availability >= A_TARGET),
                },
            },
            "epsilon_budget": epsilon_budget,
            "parameters": {
                "base": self.base_parameters.to_dict(),
                "proposal": self.proposal_parameters.to_dict(),
                "simulation": self.simulation_parameters.to_dict(),
            },
            "statistics": {
                "probability_estimate": summary_stats.probability_estimate,
                "variance_estimate": summary_stats.variance_estimate,
                "std_error": summary_stats.std_error,
                "ucb": summary_stats.ucb,
                "lcb_availability": summary_stats.lcb_availability,
                "ess": summary_stats.ess,
                "ess_ratio": summary_stats.ess_ratio,
                "proposal_fail_rate": summary_stats.proposal_fail_rate,
                "relative_precision": summary_stats.relative_precision,
                "mc_variance_under_proposal": mc_variance_under_proposal,
                "importance_sampling_efficiency_gain": efficiency_gain,
            },
            "convergence_criteria": {
                "ess_min": ESS_MIN,
                "ess_ratio_actual": summary_stats.ess_ratio,
                "ess_pass": summary_stats.ess_ratio >= ESS_MIN,
                "r_target": R_TARGET,
                "relative_precision_actual": summary_stats.relative_precision,
                "convergence_pass": summary_stats.relative_precision <= R_TARGET,
            },
            "g1_verdict": verdict,
            "artifacts": {
                "samples_csv": str(samples_csv_path),
                "convergence_csv": str(convergence_csv_path),
                "summary_json": str(summary_json_path),
                "variance_plot_png": str(variance_plot_path),
            },
        }

        with summary_json_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)

        return payload

    def _make_result_dir(self, output_root: Path) -> Path:
        """Create a timestamped output directory.

        Args:
            output_root: Root directory for all FI-01 outputs.

        Returns:
            Path of the created result directory.
        """
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        result_dir = output_root / stamp
        result_dir.mkdir(parents=True, exist_ok=False)
        return result_dir

    def _write_convergence_csv(self, path: Path, convergence: list[dict[str, float]]) -> None:
        """Persist convergence diagnostics.

        Args:
            path: Output CSV path.
            convergence: Prefix-wise diagnostics.
        """
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["sample_count", "estimate", "variance", "std_error", "ucb", "relative_precision"])
            for row in convergence:
                writer.writerow(
                    [
                        int(row["sample_count"]),
                        float(row["estimate"]),
                        float(row["variance"]),
                        float(row["std_error"]),
                        float(row["ucb"]),
                        float(row["relative_precision"]),
                    ]
                )

    def _plot_variance_convergence(self, path: Path, convergence: list[dict[str, float]]) -> None:
        """Render convergence chart for estimator variance.

        Args:
            path: Output PNG path.
            convergence: Prefix-wise diagnostics.
        """
        if not convergence:
            return

        sample_counts = np.array([row["sample_count"] for row in convergence], dtype=np.float64)
        variances = np.array([row["variance"] for row in convergence], dtype=np.float64)

        safe_variances = np.maximum(variances, 1e-30)

        figure, axis = plt.subplots(figsize=(9, 5))
        axis.plot(sample_counts, safe_variances, color="tab:blue", linewidth=2.0)
        axis.set_xlabel("Samples")
        axis.set_ylabel("Estimator variance")
        axis.set_yscale("log")
        axis.set_title("FI-01 variance convergence (Importance Sampling)")
        axis.grid(True, alpha=0.3)
        figure.tight_layout()
        figure.savefig(path, dpi=140)
        plt.close(figure)


def build_default_campaign(seed: int = 20260421, batch_size: int = DEFAULT_BATCH_SIZE) -> FI01Campaign:
    """Build a default FI-01 campaign configuration.

    Args:
        seed: Random seed.
        batch_size: Maximum samples per batch.

    Returns:
        Configured FI01Campaign instance.
    """
    base = ChannelParameters().normalized()
    proposal = ImportanceProposal.from_base(base)
    simulation = SimulationParameters(seed=seed).normalized()
    return FI01Campaign(base, proposal, simulation, batch_size=batch_size)
