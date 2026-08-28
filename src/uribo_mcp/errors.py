"""Domain-specific exceptions with safe, user-facing messages."""


class UriboMcpError(RuntimeError):
    """Base exception for expected application errors."""


class ConfigurationError(UriboMcpError):
    """Configuration is missing or invalid."""


class AuthenticationRequired(UriboMcpError):
    """A source requires a new interactive authentication setup."""


class SyncAlreadyRunning(UriboMcpError):
    """Another process currently owns the sync lock."""


class ContentNotFound(UriboMcpError):
    """Requested local content does not exist."""


class ExportPathError(UriboMcpError):
    """An export target escaped the configured export root."""

