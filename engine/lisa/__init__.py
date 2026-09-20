"""LISA — the data-refinery engine.

Pipeline: ingest (Stage 1) -> Shin de-vig & consensus (Stage 2)
          -> precision quality gate (Stage 3) -> telemetry + ledger (Stage 4).

Default runtime is dependency-free (stdlib only); Redis / Postgres adapters
are activated explicitly via configuration.
"""

from .config import Settings, load_settings
from .shin import ShinResult, shin_probabilities

__version__ = "0.1.0"
__all__ = ["Settings", "load_settings", "ShinResult", "shin_probabilities", "__version__"]