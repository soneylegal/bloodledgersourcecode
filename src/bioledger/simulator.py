"""Digital Twin channel simulator for FI-01 burst-error campaigns.

Models the 5-layer Digital Twin architecture (espec_G1 §2.1):
    Layer 1 - Biological Channel (non-IID): mutation, burst errors, rearrangements
    Layer 2 - Infrastructure: AR(1) noise, 60Hz interference, thermal drift
    Layer 3 - Logic/Crypto: frame sync, hash collision, Hybrid Verifier
    Layer 4 - Consensus: inter-node correlation (stub for FI-01)
    Layer 5 - Operational: provenance, chain of custody (stub for FI-01)

Tracks the 7 risk domains (modelo_ameacas §3.2):
    epsilon_ch, epsilon_sync, epsilon_ver, epsilon_cons,
    epsilon_key, epsilon_ops, epsilon_adv
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from bioledger.config import ChannelParameters, ImportanceProposal, SimulationParameters
from bioledger.types import ChannelSimulationResult


@dataclass(slots=True)
class _TransitionCounts:
    """Transition counts for Markov likelihood evaluation."""

    n00: np.ndarray
    n01: np.ndarray
    n10: np.ndarray
    n11: np.ndarray


class BioChannelSimulator:
    """Simulates a non-IID biological communication channel (Digital Twin).

    Implements Layers 1-3 of the Digital Twin for the FI-01 campaign scope.
    Layers 4 (Consensus) and 5 (Operational) emit zero-valued domain
    indicators in FI-01 and are activated in subsequent campaigns (FI-07..FI-09).

    The Hybrid Verifier (PO-2) simulates two-stage fail-stop:
        Stage 1 (Physical): Biochemical markers (Toehold Switches / Molecular Barcodes)
        Stage 2 (Computational): ZKP verification in-silico post-sequencing

    Args:
        base_parameters: Base parameterization of the physical channel.
        proposal_parameters: Proposal distribution used for importance sampling.
        seed: Random seed for reproducibility.
    """

    # Hybrid Verifier fail-stop probabilities (PO-2, espec_G1 §2.5)
    # Stage 1: Physical bypass probability (biochemical marker miss)
    P_BYPASS_PHYSICAL: float = 1e-4
    # Stage 2: Computational bypass probability (ZKP false accept)
    P_BYPASS_COMPUTATIONAL: float = 1e-8
    # Combined false-accept probability (PO-2 design target):
    # P(false_accept) = P_bypass_physical * P_bypass_computational
    # Ensures: P(false_accept) << P(false_abort) (fail-stop preferential)

    def __init__(
        self,
        base_parameters: ChannelParameters,
        proposal_parameters: ImportanceProposal,
        seed: int,
    ) -> None:
        """Initialize the simulator with base and proposal channel models."""
        self.base = base_parameters.normalized()
        self.proposal = proposal_parameters
        self.rng = np.random.default_rng(seed)

    def simulate_batch(self, simulation_parameters: SimulationParameters) -> ChannelSimulationResult:
        """Simulate one FI-01 batch under proposal distribution.

        Executes the full 5-layer pipeline and classifies each sample into
        the 7 epsilon domains for budget tracking.

        Args:
            simulation_parameters: Runtime controls and decode-failure thresholds.

        Returns:
            ChannelSimulationResult containing per-sample metrics, IS weights,
            and per-domain epsilon indicators.
        """
        sim = simulation_parameters.normalized()
        n_samples = sim.sample_count
        seq_len = sim.sequence_length

        # ---------------------------------------------------------------
        # Layer 1: Biological Channel (non-IID Markov + burst)
        # ---------------------------------------------------------------
        payload = self._generate_synthetic_sequences(n_samples, seq_len)

        states = self._sample_markov_states(
            sequence_count=n_samples,
            sequence_length=seq_len,
            p_gb=self.proposal.p_gb,
            p_bg=self.proposal.p_bg,
        )
        burst_starts = self._sample_burst_starts(states, self.proposal.burst_start_bad)
        burst_mask = self._build_burst_mask(burst_starts, self.base.burst_length_mean)

        # ---------------------------------------------------------------
        # Layer 2: Infrastructure Noise (AR(1) + 60Hz)
        # ---------------------------------------------------------------
        noise = self._simulate_infrastructure_noise(n_samples, seq_len)

        # ---------------------------------------------------------------
        # Combined flip mask (Layers 1+2)
        # ---------------------------------------------------------------
        flip_mask = self._sample_flip_mask(states=states, noise=noise, burst_mask=burst_mask)
        corrupted = np.bitwise_xor(payload, flip_mask.astype(np.uint8))

        bit_error_rate = np.mean(payload != corrupted, axis=1)
        max_burst_length = self._compute_max_burst_lengths(flip_mask)

        # ---------------------------------------------------------------
        # Domain classification (modelo_ameacas §3.2)
        # ---------------------------------------------------------------
        # epsilon_ch: channel error domain (BER threshold exceeded)
        eps_ch = bit_error_rate >= sim.decode_fail_ber_threshold
        # epsilon_sync: synchronization error domain (burst length exceeded)
        eps_sync = max_burst_length >= sim.decode_fail_burst_threshold
        # decode_fail: union of channel and sync failures
        has_error = np.logical_or(eps_ch, eps_sync)
        decode_fail = has_error

        # Layer 3: Hybrid Verifier - Ultra-Fail-Stop (espec_G1 §2.5)
        # Two-stage verification with preferential silence:
        #   P(false_accept) = P_bypass_physical * P_bypass_computational
        #   Guarantees: P(accept|error) << P(abort|error)  # noqa: ERA001
        # -----------------------------------------------------------------

        # Stage 1: Physical marker screening (Toehold Switches / Barcodes)
        physical_draw = self.rng.random(n_samples)
        physical_bypass = physical_draw < self.P_BYPASS_PHYSICAL

        # Stage 2: Computational ZKP verification (in-silico post-sequencing)
        computational_draw = self.rng.random(n_samples)
        computational_bypass = computational_draw < self.P_BYPASS_COMPUTATIONAL

        # epsilon_ver: verifier error — error present AND both stages bypassed
        both_bypassed = np.logical_and(physical_bypass, computational_bypass)
        eps_ver = np.logical_and(has_error, both_bypassed)

        # ---------------------------------------------------------------
        # Layers 4-5: Consensus & Operational (stubs for FI-01)
        # Activated in campaigns FI-07..FI-09
        # ---------------------------------------------------------------
        eps_cons = np.zeros(n_samples, dtype=bool)
        eps_key = np.zeros(n_samples, dtype=bool)
        eps_ops = np.zeros(n_samples, dtype=bool)
        eps_adv = np.zeros(n_samples, dtype=bool)

        # ---------------------------------------------------------------
        # Importance Sampling weights
        # ---------------------------------------------------------------
        weights, log_weights = self._compute_importance_weights(states, burst_starts)

        return ChannelSimulationResult(
            bit_error_rate=bit_error_rate.astype(np.float64),
            max_burst_length=max_burst_length.astype(np.int32),
            decode_fail=decode_fail.astype(np.int8),
            bad_state_fraction=np.mean(states, axis=1).astype(np.float64),
            noise_rms=np.sqrt(np.mean(noise**2, axis=1)).astype(np.float64),
            weights=weights.astype(np.float64),
            log_weights=log_weights.astype(np.float64),
            epsilon_ch=eps_ch.astype(np.int8),
            epsilon_sync=eps_sync.astype(np.int8),
            epsilon_ver=eps_ver.astype(np.int8),
            epsilon_cons=eps_cons.astype(np.int8),
            epsilon_key=eps_key.astype(np.int8),
            epsilon_ops=eps_ops.astype(np.int8),
            epsilon_adv=eps_adv.astype(np.int8),
        )

    # -------------------------------------------------------------------
    # Layer 1: Biological Channel primitives
    # -------------------------------------------------------------------

    def _generate_synthetic_sequences(
        self,
        sample_count: int,
        sequence_length: int,
    ) -> np.ndarray:
        """Generate binary synthetic payloads.

        Args:
            sample_count: Number of independent synthetic sequences.
            sequence_length: Number of symbols per sequence.

        Returns:
            A binary matrix of shape (sample_count, sequence_length).
        """
        return self.rng.integers(
            low=0,
            high=2,
            size=(sample_count, sequence_length),
            dtype=np.uint8,
        )

    def _sample_markov_states(
        self,
        sequence_count: int,
        sequence_length: int,
        p_gb: float,
        p_bg: float,
    ) -> np.ndarray:
        """Sample two-state Markov trajectories (good=0, bad=1).

        Args:
            sequence_count: Number of trajectories.
            sequence_length: Number of symbols per trajectory.
            p_gb: Transition probability from good to bad.
            p_bg: Transition probability from bad to good.

        Returns:
            State matrix with shape (sequence_count, sequence_length).
        """
        states = np.zeros((sequence_count, sequence_length), dtype=np.uint8)
        transition_random = self.rng.random((sequence_count, sequence_length - 1))

        for step in range(1, sequence_length):
            previous = states[:, step - 1]
            probability_bad = np.where(previous == 0, p_gb, 1.0 - p_bg)
            states[:, step] = (transition_random[:, step - 1] < probability_bad).astype(np.uint8)

        return states

    def _sample_burst_starts(self, states: np.ndarray, burst_start_bad: float) -> np.ndarray:
        """Sample burst-start indicators conditioned on bad-state occupancy.

        Args:
            states: Markov states for each symbol.
            burst_start_bad: Burst start probability when state is bad.

        Returns:
            Boolean matrix indicating burst start positions.
        """
        draw = self.rng.random(states.shape)
        result: np.ndarray = np.logical_and(states == 1, draw < burst_start_bad)
        return result

    def _build_burst_mask(self, burst_starts: np.ndarray, mean_length: float) -> np.ndarray:
        """Expand burst start indicators into contiguous burst spans.

        Args:
            burst_starts: Boolean burst start matrix.
            mean_length: Mean length for geometric burst-length distribution.

        Returns:
            Boolean mask where True indicates burst-affected symbols.
        """
        mask = np.zeros_like(burst_starts, dtype=bool)
        start_positions = np.argwhere(burst_starts)

        if start_positions.shape[0] == 0:
            return mask

        geometric_probability = min(max(1.0 / max(mean_length, 1.0), 1e-6), 1.0)
        lengths = self.rng.geometric(geometric_probability, size=start_positions.shape[0])

        limit = mask.shape[1]
        for idx, (row, col) in enumerate(start_positions):
            end = min(limit, col + int(lengths[idx]))
            mask[row, col:end] = True

        return mask

    def _sample_flip_mask(
        self,
        states: np.ndarray,
        noise: np.ndarray,
        burst_mask: np.ndarray,
    ) -> np.ndarray:
        """Sample symbol flips from mutation, infrastructure, and burst effects.

        Combines Layer 1 (biological mutation) and Layer 2 (infrastructure noise)
        into a unified flip decision per symbol.

        Args:
            states: Markov states matrix.
            noise: Infrastructure noise matrix.
            burst_mask: Boolean burst-affected positions.

        Returns:
            Boolean matrix with effective symbol flips.
        """
        mutation_probability = np.where(states == 1, self.base.p_mut_bad, self.base.p_mut_good)
        infra_probability = np.clip(
            self.base.base_infra_flip + self.base.infra_sensitivity * np.abs(noise),
            0.0,
            0.5,
        )
        total_probability = np.clip(mutation_probability + infra_probability, 0.0, 0.999999)

        random_field = self.rng.random(states.shape)
        stochastic_flips = random_field < total_probability

        burst_draw = self.rng.random(states.shape)
        burst_flips = np.logical_and(burst_mask, burst_draw < self.base.burst_force_flip)

        result: np.ndarray = np.logical_or(stochastic_flips, burst_flips)
        return result

    # -------------------------------------------------------------------
    # Layer 2: Infrastructure Noise
    # -------------------------------------------------------------------

    def _simulate_infrastructure_noise(self, sample_count: int, sequence_length: int) -> np.ndarray:
        """Simulate AR(1) plus 60Hz periodic noise process.

        The model follows (espec_G1 §2.3):
            n_t = phi * n_{t-1} + A*sin(2*pi*f*t*delta_t + phase) + eta_t

        Args:
            sample_count: Number of trajectories.
            sequence_length: Number of symbols in each trajectory.

        Returns:
            Noise matrix of shape (sample_count, sequence_length).
        """
        time_index = np.arange(sequence_length, dtype=np.float64) * self.base.delta_t
        sinusoid = self.base.noise_amplitude * np.sin(
            2.0 * np.pi * self.base.line_frequency_hz * time_index + self.base.noise_phase
        )

        innovations = self.rng.normal(
            loc=0.0,
            scale=self.base.noise_sigma,
            size=(sample_count, sequence_length),
        )

        noise = np.zeros((sample_count, sequence_length), dtype=np.float64)
        noise[:, 0] = innovations[:, 0] + sinusoid[0]

        for step in range(1, sequence_length):
            noise[:, step] = (
                self.base.noise_phi * noise[:, step - 1]
                + sinusoid[step]
                + innovations[:, step]
            )

        return noise

    # -------------------------------------------------------------------
    # Diagnostics
    # -------------------------------------------------------------------

    def _compute_max_burst_lengths(self, flip_mask: np.ndarray) -> np.ndarray:
        """Compute longest contiguous run of flips for each sample.

        Args:
            flip_mask: Effective flip matrix.

        Returns:
            Integer array of longest run lengths per sample.
        """
        result = np.zeros(flip_mask.shape[0], dtype=np.int32)

        for row_idx, row_mask in enumerate(flip_mask):
            padded = np.concatenate(([0], row_mask.astype(np.int8), [0]))
            delta = np.diff(padded)
            starts = np.flatnonzero(delta == 1)
            ends = np.flatnonzero(delta == -1)
            if starts.size == 0:
                result[row_idx] = 0
                continue
            result[row_idx] = int(np.max(ends - starts))

        return result

    # -------------------------------------------------------------------
    # Importance Sampling likelihood ratio
    # -------------------------------------------------------------------

    def _compute_importance_weights(
        self,
        states: np.ndarray,
        burst_starts: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Compute likelihood-ratio weights between base and proposal models.

        Args:
            states: Proposal-sampled Markov trajectories.
            burst_starts: Proposal-sampled burst-start indicators.

        Returns:
            Tuple of (weights, log_weights).
        """
        base_log = self._markov_log_likelihood(states, self.base.p_gb, self.base.p_bg)
        base_log += self._burst_start_log_likelihood(states, burst_starts, self.base.burst_start_bad)

        proposal_log = self._markov_log_likelihood(states, self.proposal.p_gb, self.proposal.p_bg)
        proposal_log += self._burst_start_log_likelihood(
            states,
            burst_starts,
            self.proposal.burst_start_bad,
        )

        log_weights = np.clip(base_log - proposal_log, -700.0, 700.0)
        weights = np.exp(log_weights)
        return weights, log_weights

    def _markov_log_likelihood(self, states: np.ndarray, p_gb: float, p_bg: float) -> np.ndarray:
        """Evaluate Markov path log-likelihood for each sample.

        Args:
            states: State trajectories.
            p_gb: Transition probability good->bad.
            p_bg: Transition probability bad->good.

        Returns:
            Log-likelihood vector per sample.
        """
        counts = self._count_markov_transitions(states)

        p_gb = min(max(p_gb, 1e-12), 1.0 - 1e-12)
        p_bg = min(max(p_bg, 1e-12), 1.0 - 1e-12)

        log_likelihood: np.ndarray = (
            counts.n00 * np.log(1.0 - p_gb)
            + counts.n01 * np.log(p_gb)
            + counts.n10 * np.log(p_bg)
            + counts.n11 * np.log(1.0 - p_bg)
        )
        return log_likelihood

    def _count_markov_transitions(self, states: np.ndarray) -> _TransitionCounts:
        """Count transition types for each sampled trajectory.

        Args:
            states: State matrix.

        Returns:
            Transition-count vectors.
        """
        previous = states[:, :-1]
        current = states[:, 1:]

        n00 = np.sum(np.logical_and(previous == 0, current == 0), axis=1).astype(np.float64)
        n01 = np.sum(np.logical_and(previous == 0, current == 1), axis=1).astype(np.float64)
        n10 = np.sum(np.logical_and(previous == 1, current == 0), axis=1).astype(np.float64)
        n11 = np.sum(np.logical_and(previous == 1, current == 1), axis=1).astype(np.float64)

        return _TransitionCounts(n00=n00, n01=n01, n10=n10, n11=n11)

    def _burst_start_log_likelihood(
        self,
        states: np.ndarray,
        burst_starts: np.ndarray,
        burst_start_bad: float,
    ) -> np.ndarray:
        """Evaluate burst-start Bernoulli log-likelihood per sample.

        Args:
            states: State matrix.
            burst_starts: Burst-start indicator matrix.
            burst_start_bad: Bernoulli probability while state is bad.

        Returns:
            Log-likelihood vector per sample.
        """
        probability = min(max(burst_start_bad, 1e-12), 1.0 - 1e-12)

        bad_positions = states == 1
        starts_in_bad = np.logical_and(burst_starts, bad_positions)

        bad_count = np.sum(bad_positions, axis=1).astype(np.float64)
        start_count = np.sum(starts_in_bad, axis=1).astype(np.float64)

        log_likelihood: np.ndarray = (
            start_count * np.log(probability) + (bad_count - start_count) * np.log(1.0 - probability)
        )
        return log_likelihood
