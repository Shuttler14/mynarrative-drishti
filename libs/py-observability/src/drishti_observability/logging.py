from __future__ import annotations
import logging
import sys
from contextvars import ContextVar

import structlog

request_id_var: ContextVar[str] = ContextVar("request_id", default="")
user_id_var: ContextVar[str] = ContextVar("user_id", default="")

_REDACT = {"password", "password_hash", "otp", "code", "token", "access_token",
           "refresh_token", "authorization", "jwt", "secret", "card", "pan", "cvv"}


def _redactor(_, __, event: dict) -> dict:
    for k in list(event):
        if any(s in k.lower() for s in _REDACT):
            event[k] = "***redacted***"
    return event


def _bind_context(_, __, event: dict) -> dict:
    rid, uid = request_id_var.get(), user_id_var.get()
    if rid:
        event["request_id"] = rid
    if uid:
        event["user_id"] = uid
    return event


def configure(service: str, *, level: str, json_logs: bool, version: str) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout,
                        level=getattr(logging, level.upper(), logging.INFO))

    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        _bind_context,
        _redactor,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer() if json_logs
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper())),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    structlog.contextvars.bind_contextvars(service=service, version=version)
