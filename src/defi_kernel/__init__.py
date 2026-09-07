"""Cardano liquidity runtime. Execution capabilities are explicitly qualified."""

import os

# Dendrite loads dotenv on import. Accept credentials from explicit environment
# references, never an ambient directory's .env file.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
