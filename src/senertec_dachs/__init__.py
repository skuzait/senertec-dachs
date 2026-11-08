"""Async client library for the SenerTec Dachs MSR2 local GLT interface.

Local access uses the documented HTTP interface of the Dachs MSR2
Ethernet board (SenerTec 11/4798.444.000): HTTP Basic authentication on
port 8080, ``GET /getKey`` with repeated ``k`` parameters, and
form-encoded ``POST /setKeys``.
"""

from .errors import (
    AuthenticationError,
    ConnectionError,
    DachsError,
    HttpError,
    ProtocolError,
    RequestTimeoutError,
    WriteError,
)
from .keys import LOCAL_KEYS
from .cloud.client import CloudClient, CloudStatus
from .device import Dachs, ModuleStatus
from .local.client import LocalClient
from .models import (
    ActuatorsData,
    DAILY_RUN_SLOT_START_HOURS,
    HydraulicsData,
    IdentityData,
    Key,
    KeyType,
    MaintenanceData,
    MeasurementsData,
    ModuleData,
    ModuleStatisticsData,
    Normalization,
    OperatingData,
    Reading,
    ReadingState,
    SecondGeneratorData,
    Snapshot,
    TemperaturesData,
    TemperatureTargetsData,
    YearEndData,
)

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__: str = _pkg_version("senertec-dachs")
except PackageNotFoundError:  # not installed (e.g. running from a source tree)
    __version__ = "unknown"

__all__ = [
    "AuthenticationError",
    "ConnectionError",
    "DachsError",
    "HttpError",
    "ProtocolError",
    "RequestTimeoutError",
    "WriteError",
    "LOCAL_KEYS",
    "Dachs",
    "CloudClient",
    "CloudStatus",
    "ModuleStatus",
    "LocalClient",
    "ActuatorsData",
    "DAILY_RUN_SLOT_START_HOURS",
    "HydraulicsData",
    "IdentityData",
    "Key",
    "KeyType",
    "MaintenanceData",
    "MeasurementsData",
    "ModuleData",
    "ModuleStatisticsData",
    "Normalization",
    "OperatingData",
    "Reading",
    "ReadingState",
    "SecondGeneratorData",
    "Snapshot",
    "TemperaturesData",
    "TemperatureTargetsData",
    "YearEndData",
]
