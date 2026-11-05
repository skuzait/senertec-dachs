"""Stable public exception hierarchy for senertec-dachs."""

from __future__ import annotations

from collections.abc import Mapping


class DachsError(Exception):
    """Base class for all errors raised by this library.

    Every error carries :attr:`outcome_unknown`. It is ``True`` when the
    library cannot know whether a command reached or changed the device
    (notably for failed writes); callers such as Home Assistant integrations
    should avoid blind retries in that case.
    """

    outcome_unknown: bool

    def __init__(self, message: str, *, outcome_unknown: bool = False) -> None:
        super().__init__(message)
        self.outcome_unknown = outcome_unknown


class AuthenticationError(DachsError):
    """The device rejected the credentials (HTTP 401/403)."""


class ConnectionError(DachsError):
    """The device could not be reached (DNS, routing, refused connection)."""


class RequestTimeoutError(DachsError):
    """The device did not answer within the configured timeout."""


class HttpError(DachsError):
    """The device returned an unexpected HTTP status.

    Redirects are not followed and surface as this error, because the GLT
    interface is a fixed pair of endpoints.
    """

    status: int

    def __init__(
        self, message: str, *, status: int, outcome_unknown: bool = False
    ) -> None:
        super().__init__(message, outcome_unknown=outcome_unknown)
        self.status = status


class ProtocolError(DachsError):
    """The device response violated the documented ``key=value`` protocol."""


class WriteError(DachsError):
    """A write command was not cleanly acknowledged, or its outcome is uncertain.

    :attr:`acknowledgements` maps the acknowledged field names to their
    status text (``"ok"`` or whatever the device returned) when any part of
    the acknowledgement was received. It never contains credentials.
    """

    acknowledgements: Mapping[str, str] | None

    def __init__(
        self,
        message: str,
        *,
        acknowledgements: Mapping[str, str] | None = None,
        outcome_unknown: bool = True,
    ) -> None:
        super().__init__(message, outcome_unknown=outcome_unknown)
        self.acknowledgements = acknowledgements
