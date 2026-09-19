"""Offline bounded search benchmark; synthetic quotes, no provider or wallet access."""

import argparse
import json
import time
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

from defi_kernel.arbitrage import ArbitrageConfig, Edge, search_routes
from defi_kernel.arbitrage_runtime import DanoQuoteSnapshot
from defi_kernel.config import load_profile
from defi_kernel.domain import KernelError, OutRef
from defi_kernel.protocols import decode_dano


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-hops", type=int, nargs="+", default=[4])
    parser.add_argument(
        "--output", type=Path, default=Path("evidence/preprod-arbitrage-search.json")
    )
    args = parser.parse_args()
    profile = load_profile(Path("config.example.toml"), "preprod")
    evidence = json.loads(Path("evidence/preprod-dano-scan.json").read_text())
    pools = []
    for row in evidence["data"]["rows"]:
        try:
            pools.append(
                DanoQuoteSnapshot.from_pool(
                    decode_dano(row, profile, platform_fee_rate=30)
                )
            )
        except (KernelError, ValueError, KeyError):
            continue
    units = ("lovelace", *(f"{n:056x}41" for n in range(1, 8)))
    edges = []
    for i, left in enumerate(units):
        for j, right in enumerate(units):
            if left == right:
                continue
            ref = OutRef(f"{len(edges) + 1:064x}", 0)
            edges.append(
                Edge(
                    ref,
                    "swaps-v1",
                    left,
                    right,
                    100000000,
                    price=Fraction(98 + (i + j) % 5, 100),
                )
            )
            pool = replace(pools[(i + j) % len(pools)], unit_a=left, unit_b=right)
            edges.append(
                Edge(
                    OutRef(f"{len(edges) + 1:064x}", 0),
                    "dano",
                    left,
                    right,
                    100000000,
                    pool=pool,
                    fixed_fee=100000,
                )
            )
    result = {
        "mode": "offline synthetic mixed-venue graph; not a live throughput guarantee",
        "edge_count": len(edges),
        "samples": [],
    }
    for hops in args.max_hops:
        for cap in (256, 1024, 4096):
            for rotation in range(3):
                started = time.monotonic()
                plans, stats = search_routes(
                    edges,
                    ArbitrageConfig(units, max_hops=hops, max_cycles=cap),
                    rotation=rotation,
                )
                result["samples"].append(
                    {
                        "max_cycles": cap,
                        "max_hops": hops,
                        "candidate_hops": sorted({len(p.hops) for p in plans}),
                        "rotation": rotation,
                        "elapsed_seconds": time.monotonic() - started,
                        **stats,
                        "best_gross_gain_lovelace": max(
                            (p.gross_gain for p in plans), default=None
                        ),
                    }
                )
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
