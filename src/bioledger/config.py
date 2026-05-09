"""Configuration models for the Gate G1 simulation harness.

The models are intentionally simple and serializable so that the same
configuration structure can be reused by future C++/CUDA backends.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


def _clamp_probability(value: float, eps: float = 1e-12) -> float:
    """Clamp a floating-point value into a valid probability range.

    Args:
        value: Candidate probability.
        eps: Lower and upper tolerance margin.

    Returns:
        A probability in the range [eps, 1 - eps].
    """
    return min(max(value, eps), 1.0 - eps)


@dataclass(frozen=True)
class ChannelParameters:
    """Base channel parameters for the Digital Twin.

    Attributes:
        p_mut_good: Point mutation probability in the good state.
        p_mut_bad: Point mutation probability in the bad state.
        p_gb: Markov transition probability from good to bad.
        p_bg: Markov transition probability from bad to good.
        burst_start_bad: Burst start probability while in bad state.
        burst_length_mean: Mean burst length in symbols.
        burst_force_flip: Flip probability for symbols affected by burst.
        base_infra_flip: Baseline infrastructure flip probability.
        infra_sensitivity: Coupling factor between noise amplitude and flips.
        noise_phi: AR(1) coefficient for infrastructure noise.
        noise_amplitude: Sinusoidal noise amplitude.
        noise_sigma: Gaussian innovation sigma for AR(1) process.
        noise_phase: Sinusoidal phase in radians.
        delta_t: Discrete step duration in seconds.
        line_frequency_hz: Power-line frequency applied to sinusoidal noise.
    """

    p_mut_good: float = 2.0e-4
    p_mut_bad: float = 6.0e-2
    p_gb: float = 8.0e-4
    p_bg: float = 2.2e-1
    burst_start_bad: float = 2.5e-2
    burst_length_mean: float = 14.0
    burst_force_flip: float = 0.98
    base_infra_flip: float = 1.0e-4
    infra_sensitivity: float = 0.03
    noise_phi: float = 0.92
    noise_amplitude: float = 0.18
    noise_sigma: float = 0.055
    noise_phase: float = 0.0
    delta_t: float = 1.0 / 1000.0
    line_frequency_hz: float = 60.0

    def normalized(self) -> "ChannelParameters":
        """Return a numerically safe copy with clamped probabilities.

        Returns:
            A normalized parameter set.
        """
        return ChannelParameters(
            p_mut_good=_clamp_probability(self.p_mut_good),
            p_mut_bad=_clamp_probability(self.p_mut_bad),
            p_gb=_clamp_probability(self.p_gb),
            p_bg=_clamp_probability(self.p_bg),
            burst_start_bad=_clamp_probability(self.burst_start_bad),
            burst_length_mean=max(self.burst_length_mean, 1.0),
            burst_force_flip=_clamp_probability(self.burst_force_flip),
            base_infra_flip=_clamp_probability(self.base_infra_flip),
            infra_sensitivity=max(self.infra_sensitivity, 0.0),
            noise_phi=min(max(self.noise_phi, -0.999), 0.999),
            noise_amplitude=max(self.noise_amplitude, 0.0),
            noise_sigma=max(self.noise_sigma, 1e-9),
            noise_phase=self.noise_phase,
            delta_t=max(self.delta_t, 1e-9),
            line_frequency_hz=max(self.line_frequency_hz, 0.0),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable dictionary representation.

        Returns:
            Dictionary with scalar configuration values.
        """
        return asdict(self)


@dataclass(frozen=True)
class ImportanceProposal:
    """Proposal distribution parameters used for Importance Sampling.

    Attributes:
        p_gb: Proposal transition probability from good to bad.
        p_bg: Proposal transition probability from bad to good.
        burst_start_bad: Proposal burst start probability in bad state.
    """

    p_gb: float
    p_bg: float
    burst_start_bad: float

    @staticmethod
    def from_base(
        base: ChannelParameters,
        gb_scale: float = 16.0,
        bg_scale: float = 0.4,
        burst_scale: float = 6.0,
    ) -> "ImportanceProposal":
        """Create a proposal distribution tilted toward burst failures.

        Args:
            base: Base channel parameter set.
            gb_scale: Scale factor for p_gb.
            bg_scale: Scale factor for p_bg.
            burst_scale: Scale factor for burst_start_bad.

        Returns:
            A proposal parameter set that increases failure incidence.
        """
        return ImportanceProposal(
            p_gb=_clamp_probability(base.p_gb * gb_scale),
            p_bg=_clamp_probability(base.p_bg * bg_scale),
            burst_start_bad=_clamp_probability(base.burst_start_bad * burst_scale),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable dictionary representation.

        Returns:
            Dictionary with proposal values.
        """
        return asdict(self)


@dataclass(frozen=True)
class SimulationParameters:
    """Execution parameters for FI-01 campaigns.

    Attributes:
        sample_count: Number of synthetic sequences to sample.
        sequence_length: Sequence length in symbols.
        decode_fail_ber_threshold: BER threshold for decode failure event.
        decode_fail_burst_threshold: Max burst threshold for decode failure.
        checkpoint_interval: Prefix size interval for convergence checkpoints.
        seed: Random seed for reproducibility.
    """

    sample_count: int = 25000
    sequence_length: int = 1024
    decode_fail_ber_threshold: float = 0.085
    decode_fail_burst_threshold: int = 88
    checkpoint_interval: int = 500
    seed: int = 20260421

    def normalized(self) -> "SimulationParameters":
        """Return a numerically safe copy.

        Returns:
            A normalized simulation parameter set.
        """
        return SimulationParameters(
            sample_count=max(self.sample_count, 1),
            sequence_length=max(self.sequence_length, 4),
            decode_fail_ber_threshold=min(max(self.decode_fail_ber_threshold, 1e-9), 1.0),
            decode_fail_burst_threshold=max(self.decode_fail_burst_threshold, 1),
            checkpoint_interval=max(self.checkpoint_interval, 1),
            seed=self.seed,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable dictionary representation.

        Returns:
            Dictionary with simulation controls.
        """
        return asdict(self)
