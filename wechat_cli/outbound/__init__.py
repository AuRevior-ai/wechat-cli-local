"""Safe outbound messaging primitives for WeChat CLI."""

from .models import (
    OutboundError,
    ProbeResult,
    ResolvedTarget,
    SendRequest,
    SendResult,
    SendStatus,
    TargetResolutionError,
)

__all__ = [
    "OutboundError",
    "ProbeResult",
    "ResolvedTarget",
    "SendRequest",
    "SendResult",
    "SendStatus",
    "TargetResolutionError",
]
