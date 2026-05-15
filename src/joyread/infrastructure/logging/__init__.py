"""Logging infrastructure.

Public surface for the rest of the app:

- ``configure_early_logging`` — stderr-only handlers, called from bootstrap
  before ``create_app_context`` so that early lifecycle lines (migrations,
  service init) are visible.
- ``configure_logging`` — adds the rotating file handler once the writable
  logs directory is known. Idempotent with the early call.
- ``get_logger`` — thin facade over :func:`logging.getLogger`; lets us layer
  future cross-cutting concerns (rate-limiting, structured fields,
  per-subsystem filters) without touching every module.
- ``log_timed_block`` — context manager that brackets a block with
  ``start`` / ``done in N ms`` lines.

Existing modules continue to use ``logging.getLogger(__name__)`` directly;
``get_logger`` is additive and opt-in for new code.
"""

from joyread.infrastructure.logging.logging_service import (
    LOG_ENV_VAR,
    LOG_FILE_BACKUP_COUNT,
    LOG_FILE_MAX_BYTES,
    LOG_FILE_NAME,
    configure_early_logging,
    configure_logging,
    get_logger,
    log_timed_block,
)


__all__ = [
    "LOG_ENV_VAR",
    "LOG_FILE_BACKUP_COUNT",
    "LOG_FILE_MAX_BYTES",
    "LOG_FILE_NAME",
    "configure_early_logging",
    "configure_logging",
    "get_logger",
    "log_timed_block",
]
