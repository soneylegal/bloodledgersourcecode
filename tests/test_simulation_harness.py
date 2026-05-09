"""Unit tests for the v0.2.0 FI-01 simulation harness."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bioledger.config import ChannelParameters, ImportanceProposal, SimulationParameters
from bioledger.simulator import BioChannelSimulator
from bioledger.statistics import build_convergence_series, importance_sampling_estimate


class BioChannelSimulatorContractTest(unittest.TestCase):
    """Contract tests for channel simulation outputs."""

    def test_simulation_output_shapes_and_ranges(self) -> None:
        """Validate shape and numerical ranges of one simulation batch."""
        base = ChannelParameters().normalized()
        proposal = ImportanceProposal.from_base(base, gb_scale=4.0, bg_scale=0.8, burst_scale=2.0)
        sim = SimulationParameters(sample_count=128, sequence_length=256, checkpoint_interval=32, seed=123)

        simulator = BioChannelSimulator(base, proposal, seed=sim.seed)
        result = simulator.simulate_batch(sim)

        self.assertEqual(result.decode_fail.shape, (sim.sample_count,))
        self.assertEqual(result.bit_error_rate.shape, (sim.sample_count,))
        self.assertEqual(result.max_burst_length.shape, (sim.sample_count,))

        self.assertTrue(np.all(result.weights > 0.0))
        self.assertTrue(np.all(result.bit_error_rate >= 0.0))
        self.assertTrue(np.all(result.bit_error_rate <= 1.0))


class ImportanceSamplingStatisticsContractTest(unittest.TestCase):
    """Contract tests for importance sampling statistics."""

    def test_unity_weights_match_monte_carlo_mean(self) -> None:
        """Verify IS estimate equals MC estimate for unity weights."""
        indicator = np.array([0, 1, 0, 1, 1, 0], dtype=np.int8)
        weights = np.ones(indicator.size, dtype=np.float64)

        summary = importance_sampling_estimate(indicator, weights)
        self.assertAlmostEqual(summary.probability_estimate, float(np.mean(indicator)), places=12)
        self.assertGreaterEqual(summary.ucb95, summary.probability_estimate)

    def test_convergence_series_returns_checkpoints(self) -> None:
        """Validate checkpoint generation for convergence tracking."""
        indicator = np.array([0, 1, 0, 1, 1, 0, 0, 1], dtype=np.int8)
        weights = np.full(indicator.size, 1.0, dtype=np.float64)

        series = build_convergence_series(indicator, weights, checkpoint_interval=3)
        self.assertGreaterEqual(len(series), 2)
        self.assertEqual(int(series[-1]["sample_count"]), indicator.size)


if __name__ == "__main__":
    unittest.main()
