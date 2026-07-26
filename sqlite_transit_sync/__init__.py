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

__all__ = [
    "MergeReport",
    "SecretPattern",
    "Snapshot",
    "SyncConfig",
    "SyncError",
    "TimestampMergePolicy",
    "TransitSync",
    "load_secret_patterns",
]

__version__ = "0.2.0"

