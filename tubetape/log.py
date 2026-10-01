"""Centralized logging for TubeTape.

Every module logs through the ``tubetape`` logger (or a child such as
``tubetape.scanner``). :func:`setup_logging` wires up two destinations:

* **stderr** — concise console output. By default only WARNING and above are
  shown so the terminal stays clean; ``-v`` raises that to INFO and ``-vv`` to
  DEBUG.
* **a log file** — full DEBUG detail (timestamps, logger name, message),
  written next to the database by default. This is the "what is the program
  doing right now" audit trail.
"""

from __future__ import annotations

import logging
import sys

LOGGER_NAME = "tubetape"

_CONSOLE_FORMAT = "%(asctime)s %(levelname)-8s %(message)s"
_FILE_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Third-party libraries are extremely chatty at DEBUG/INFO; keep them quiet.
_NOISY_LOGGERS = (
    "googleapiclient",
    "google.auth",
    "google_auth_oauthlib",
    "urllib3",
    "oauthlib",
    "requests",
    "watchdog",
)

_configured = False


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a logger under the ``tubetape`` namespace."""
    if name is None:
        return logging.getLogger(LOGGER_NAME)
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


def setup_logging(verbose: int = 0, log_file: str | None = None) -> None:
    """Configure the ``tubetape`` logger hierarchy.

    Args:
        verbose: 0 → console shows WARNING+, 1 → INFO+, 2+ → DEBUG+.
        log_file: path to the detailed log file (always DEBUG+). If omitted,
            only console logging is configured.
    """
    global _configured

    root = logging.getLogger(LOGGER_NAME)
    root.setLevel(logging.DEBUG)
    root.propagate = False

    # Re-configuring (e.g. ``main`` called twice in one process, or the watch
    # loop re-running the pipeline) must not stack duplicate handlers.
    if _configured:
        for handler in list(root.handlers):
            handler.close()
            root.removeHandler(handler)
    _configured = True

    if verbose >= 2:
        console_level = logging.DEBUG
    elif verbose == 1:
        console_level = logging.INFO
    else:
        console_level = logging.WARNING

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(console_level)
    console.setFormatter(logging.Formatter(_CONSOLE_FORMAT, _DATE_FORMAT))
    root.addHandler(console)

    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(_FILE_FORMAT, _DATE_FORMAT))
        root.addHandler(file_handler)

    for noisy in _NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.WARNING)
