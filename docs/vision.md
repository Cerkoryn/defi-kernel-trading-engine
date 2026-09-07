# Design interpretation

The runtime supplies an automation layer above the Kernel's public on-chain orders. Liquidity stays in user-controlled positions and can be consumed by other applications. Quoting against AMMs and eventually composing an order fill with a direct pool swap implements the complementary order-book role described in [How to Launch a DeFi Order Book](https://github.com/fallen-icarus/meditations-blog/tree/8bf6b41d8588cf447e5f5f5ce72ca1cfe7d86f2e/how-to-launch-a-defi-order-book).

The [on-chain intents essay](https://github.com/fallen-icarus/meditations-blog/tree/8bf6b41d8588cf447e5f5f5ce72ca1cfe7d86f2e/why-the-defi-kernel-uses-on-chain-intents) motivates preserving discoverable orders and composable settlement. Hosted Koios access is a practical operator choice, with an explicit provider trust/availability dependency. REST responses are observations, not independently verified ledger snapshots.

[The DeFi Hypothesis](https://github.com/fallen-icarus/meditations-blog/tree/8bf6b41d8588cf447e5f5f5ce72ca1cfe7d86f2e/the-defi-hypothesis) and [The Seed of DeFi](https://github.com/fallen-icarus/meditations-blog/tree/8bf6b41d8588cf447e5f5f5ce72ca1cfe7d86f2e/the-seed-of-defi) motivate small foundational primitives with specialized applications above them. Their economic predictions are motivation, not guarantees of adoption or profitable trading. Lending, permission contracts, and a broad SDK remain outside this increment.

Practical consequences: preserve the address's staking credential, keep strategy code separate from signing, require explicit atomic settlement, account for fees and committed capital, and investigate uncertain outcomes before attempting replacement transactions.
