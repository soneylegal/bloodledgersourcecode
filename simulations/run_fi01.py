#!/usr/bin/env python3
"""CLI entrypoint for FI-01 burst-error campaign execution.

Supports batched execution for large sample counts to avoid OOM conditions.
"""

from __future__ import annotations

import argparse
import json
import sys

from bioledger.campaign_fi01 import FI01Campaign
from bioledger.config import ChannelParameters, ImportanceProposal, SimulationParameters


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns:
        Parsed namespace.
    """
    parser = argparse.ArgumentParser(description="Run FI-01 burst-error campaign")
    parser.add_argument("--samples", type=int, default=25000, help="Number of sampled sequences")
    parser.add_argument("--batch-size", type=int, default=10000, help="Samples per batch (memory control)")
    parser.add_argument("--length", type=int, default=1024, help="Sequence length")
    parser.add_argument("--checkpoint", type=int, default=500, help="Convergence checkpoint interval")
    parser.add_argument("--seed", type=int, default=20260421, help="Random seed")
    parser.add_argument("--output-dir", default="results/fi01", help="Output root directory")
    parser.add_argument("--ber-threshold", type=float, default=0.085, help="Decode failure BER threshold")
    parser.add_argument("--burst-threshold", type=int, default=88, help="Decode failure max burst threshold")
    parser.add_argument("--proposal-gb-scale", type=float, default=2.2, help="Scale factor for proposal p_gb")
    parser.add_argument("--proposal-bg-scale", type=float, default=0.85, help="Scale factor for proposal p_bg")
    parser.add_argument(
        "--proposal-burst-scale", type=float, default=1.8, help="Scale factor for proposal burst probability",
    )
    return parser.parse_args()


def main() -> int:
    """Run campaign and print summary output.

    Returns:
        Process exit code.
    """
    args = parse_args()

    base = ChannelParameters().normalized()
    proposal = ImportanceProposal.from_base(
        base=base,
        gb_scale=args.proposal_gb_scale,
        bg_scale=args.proposal_bg_scale,
        burst_scale=args.proposal_burst_scale,
    )
    simulation = SimulationParameters(
        sample_count=args.samples,
        sequence_length=args.length,
        decode_fail_ber_threshold=args.ber_threshold,
        decode_fail_burst_threshold=args.burst_threshold,
        checkpoint_interval=args.checkpoint,
        seed=args.seed,
    ).normalized()

    campaign = FI01Campaign(
        base_parameters=base,
        proposal_parameters=proposal,
        simulation_parameters=simulation,
        batch_size=args.batch_size,
    )

    print(
        f"[FI-01] Starting campaign: {args.samples:,} samples "
        f"in batches of {args.batch_size:,} "
        f"(seq_len={args.length})",
        file=sys.stderr,
        flush=True,
    )

    summary = campaign.run(args.output_dir)

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
