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
from .replica import (
    ReplicaImport,
    ReplicaSnapshot,
    ReplicaTransit,
    generate_key,
)

__all__ = [
    "MergeReport",
    "ReplicaImport",
    "ReplicaSnapshot",
    "ReplicaTransit",
    "SecretPattern",
    "Snapshot",
    "SyncConfig",
    "SyncError",
    "TimestampMergePolicy",
    "TransitSync",
    "generate_key",
    "load_secret_patterns",
]

__version__ = "0.3.0"
