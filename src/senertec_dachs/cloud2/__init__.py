"""Cloud v2 backend (DachsPortal2 / DachsConnect) for senertec-dachs."""

from .client import DEFAULT_LANG, DEFAULT_TIMEOUT, Cloud2Client
from .catalog import COMPAT_BY_KEY, COMPAT_KEYS, CompatSpec
from .models import CanIpError, CanIpValue, Cloud2Status, UnitInfo

__all__ = [
    "Cloud2Client",
    "DEFAULT_LANG",
    "DEFAULT_TIMEOUT",
    "COMPAT_BY_KEY",
    "COMPAT_KEYS",
    "CompatSpec",
    "CanIpError",
    "CanIpValue",
    "Cloud2Status",
    "UnitInfo",
]
