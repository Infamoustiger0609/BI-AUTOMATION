"""Custom application exceptions for Prompt2PBI."""

from __future__ import annotations


class Prompt2PBIError(Exception):
    """Base class for application-specific errors."""


class APIKeyError(Prompt2PBIError):
    """Raised when the request does not contain a valid API key."""


class RateLimitExceededError(Prompt2PBIError):
    """Raised when a client exceeds the configured request rate."""


class JobNotFoundError(Prompt2PBIError):
    """Raised when a job id cannot be found."""


class JobNotReadyError(Prompt2PBIError):
    """Raised when a job result is not yet available."""


class FileValidationError(Prompt2PBIError):
    """Raised when an uploaded file fails validation."""

