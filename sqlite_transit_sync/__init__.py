"""Public API for sqlite-transit-sync."""

from .core import (
    MergeReport,
    SecretPattern,
    Snapshot,
    SyncConfig,
    SyncError,
    TimestampMergePolicy,
    TransitSync,
    load_secret_patterns,
)
from .republica import (
    Envelope,
    EnvelopeReceipt,
    RepublicaImport,
    RepublicaSnapshot,
    RepublicaTransit,
    generate_key,
)

__all__ = [
    "Envelope",
    "EnvelopeReceipt",
    "MergeReport",
    "RepublicaImport",
    "RepublicaSnapshot",
    "RepublicaTransit",
    "SecretPattern",
    "Snapshot",
    "SyncConfig",
    "SyncError",
    "TimestampMergePolicy",
    "TransitSync",
    "generate_key",
    "load_secret_patterns",
]

__version__ = "0.4.0"
