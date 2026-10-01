import os
import re

from . import config


class SafetyError(Exception):
    pass


RE_UNIT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}")
RE_CONTAINER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")

REDACTIONS = (
    (
        "private-key",
        re.compile(
            r"-----BEGIN[A-Z ]*PRIVATE KEY-----.*?-----END[A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    ("url-credentials", re.compile(r"(?<=://)[^\s/:@]+:[^\s/@]+(?=@)")),
    ("bearer-token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{12,}")),
    ("assigned-secret", re.compile(
        r"(?i)\b(?:token|password|passwd|secret|api[_-]?key|access[_-]?key|"
        r"private[_-]?key|auth[_-]?token|credential)\b\s*[=:]\s*\S+"
    )),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9._-]{8,}")),
    ("aws-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("long-hex", re.compile(r"\b[0-9a-fA-F]{40,}\b")),
)


def redact(text):
    count = 0
    for _label, pattern in REDACTIONS:
        text, hits = pattern.subn("[REDACTED]", text)
        count += hits
    return text, count


def truncate(text, limit=config.MAX_OUTPUT_CHARS):
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    dropped = len(text) - limit
    marker = "\n[...{} chars omitted ...]\n".format(dropped)
    return text[:head] + marker + text[-tail:]


def check_unit(value):
    if not isinstance(value, str) or not RE_UNIT.fullmatch(value):
        raise SafetyError("invalid unit name: {!r}".format(value))
    return value


def check_container(value):
    if not isinstance(value, str) or not RE_CONTAINER.fullmatch(value):
        raise SafetyError("invalid container name: {!r}".format(value))
    return value


def check_since(value):
    if value not in config.JOURNAL_SINCE_CHOICES:
        raise SafetyError(
            "invalid since value {!r}, expected one of {}".format(
                value, ", ".join(config.JOURNAL_SINCE_CHOICES)
            )
        )
    return value


def int_arg(name, low, high):
    def check(value):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise SafetyError("{} must be an integer, got {!r}".format(name, value))
        if not low <= parsed <= high:
            raise SafetyError(
                "{} must be between {} and {}, got {}".format(name, low, high, parsed)
            )
        return parsed

    return check


def optional_port(value):
    if value is None or value == "":
        return None
    return int_arg("port", 1, 65535)(value)


def check_log_path(value):
    if not isinstance(value, str) or not value:
        raise SafetyError("path must be a non-empty string")
    resolved = os.path.realpath(value)
    for allowed in config.LOG_DIRS:
        root = os.path.realpath(allowed)
        if resolved == root or resolved.startswith(root + os.sep):
            if not os.path.isfile(resolved):
                raise SafetyError("not a readable file: {}".format(resolved))
            return resolved
    raise SafetyError(
        "path {} is outside the allowed log directories {}".format(
            resolved, ", ".join(config.LOG_DIRS)
        )
    )


def validate(spec, args):
    if not isinstance(args, dict):
        raise SafetyError("arguments must be an object, got {}".format(type(args).__name__))

    properties = spec.parameters.get("properties", {})
    required = spec.parameters.get("required", [])

    unknown = sorted(set(args) - set(properties))
    if unknown:
        raise SafetyError(
            "unknown argument(s) for {}: {}".format(spec.name, ", ".join(unknown))
        )

    missing = sorted(set(required) - set(args))
    if missing:
        raise SafetyError(
            "{} requires argument(s): {}".format(spec.name, ", ".join(missing))
        )

    cleaned = {}
    for key, value in args.items():
        cleaned[key] = spec.validators[key](value)
    return cleaned