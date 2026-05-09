"""Typed data containers for simulation outputs."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(slots=True)
class ChannelSimulationResult:
    """Stores all arrays required for FI-01 post-processing.

    Attributes:
        bit_error_rate: Bit error rate per sample.
        max_burst_length: Longest contiguous flip span per sample.
        decode_fail: Boolean indicator for decode failure event.
        bad_state_fraction: Fraction of positions sampled in bad Markov state.
        noise_rms: RMS noise value per sample.
        weights: Importance Sampling likelihood ratios.
        log_weights: Log-likelihood ratios.
    """

    bit_error_rate: np.ndarray
    max_burst_length: np.ndarray
    decode_fail: np.ndarray
    bad_state_fraction: np.ndarray
    noise_rms: np.ndarray
    weights: np.ndarray
    log_weights: np.ndarray
    epsilon_ch: np.ndarray
    epsilon_sync: np.ndarray
    epsilon_ver: np.ndarray
    epsilon_cons: np.ndarray
    epsilon_key: np.ndarray
    epsilon_ops: np.ndarray
    epsilon_adv: np.ndarray
