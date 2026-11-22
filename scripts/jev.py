"""Jev's advice for one new worker of the delegated PR workflow: its model, and for an implementer its effort.

The harness owns the workflow and its defaults. This module only builds one request, validates one response and keeps
the valid choices; anything missing or invalid is left to the defaults (flow.default_jev).
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import places

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
KEY_ROOT = places.SHARED  # The key file is relative to the shared data folder and must stay inside it.
DEFAULT_KEY_FILE = "secrets/typesafe-api-key.txt"
MAX_RESPONSE_BYTES = 256_000
TIMEOUT_SECONDS = 20
ROLES = ("implementer", "reviewer", "devops")
EFFORT_ROLES = ("implementer",)  # Jev sets no other role's effort.
PROVIDER_LABELS = {"codex": "OpenAI via Codex", "claudeAgent": "Anthropic via Claude", "acpRegistry_goose": "OpenRouter via goose",
                   "antigravity": "Google via Antigravity"}
EFFORT_OPTIONS = {"low": "Quick.", "medium": "Balanced.", "high": "Thorough.",
                  "xhigh": "Very thorough.", "max": "Most thorough and slowest."}
SCORES_FILE = Path(__file__).resolve().parent.parent / "assets" / "scores.json"  # Refreshed by scores.py.
USE_WINDOWS_CURL = os.name == "nt"


def _eligible(config, role):
    models = config.get("models") if isinstance(config.get("models"), dict) else {}
    return {model_id: spec for model_id, spec in models.items() if isinstance(spec, dict) and role in spec.get("roles", [])}


def _choice(instructions, options):
    return {"type": "choice", "instructions": instructions, "criteria": options}


def _scores():
    """The shipped scores, {"source", "suites", "models"}; {} when they cannot be read."""
    try:
        data = json.loads(SCORES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _board(scores):
    return (scores.get("source") or {}).get("label") or "public"


def _meaning(scores):
    """What the scores in the options measure, suite by suite, ending in a space; "" without any."""
    suites = scores.get("suites") or {}
    if not suites:
        return ""
    return (f"Each option shows its {_board(scores)} scores where known, from 0 to 100, higher being better, on tasks the "
            "board's maintainers run themselves and do not publish: "
            + "; ".join(f"{suite['name']}, {suite['measures']}" for suite in suites.values()) + ". ")


def _label(model_id, spec, scores):
    """The option Jev reads: the model, its scores in each chosen suite (a stand-in's, named as such) and who runs it."""
    found = (scores.get("models") or {}).get(model_id) or {}
    shown = ", ".join(f"{suite['name']} {found[key]:.1f}" for key, suite in (scores.get("suites") or {}).items()
                      if key in found)
    if not shown:
        shown = f"no {_board(scores)} score"
    else:
        shown = (f"{found['borrowedFrom']}'s " if found.get("borrowedFrom") else "") + f"{_board(scores)} {shown}"
    return f"{model_id} ({shown}; {PROVIDER_LABELS.get(spec.get('provider'), spec.get('provider'))})"


def build_request(config, context, purpose="task", role="implementer"):
    """The Jev request for one role of one task. Raises ValueError for another purpose or role, or a context JSON
    cannot hold."""
    if purpose != "task":
        raise ValueError("purpose must be 'task'")
    if role not in ROLES:
        raise ValueError(f"role is one of {', '.join(ROLES)}")
    try:
        context = json.loads(json.dumps(context, ensure_ascii=False))
    except (TypeError, ValueError):
        raise ValueError("context must be JSON serializable") from None
    questions, scores = {}, _scores()
    options = {model_id: _label(model_id, spec, scores) for model_id, spec in _eligible(config, role).items()}
    # No lean toward the defaults or toward the cheaper: Jev weighs every approved model by what it can do.
    questions[role] = _choice(
        f"Choose the approved {role} model most likely to get this task right the first time. Capability comes "
        "first: a stronger model for difficult, risky, large or unclear work, and a lighter one only when it would "
        f"do the task just as well, sooner. Price is not a reason either way. {_meaning(scores)}Choose even when "
        "uncertain, and report your confidence honestly.",
        options,
    )
    if role in EFFORT_ROLES:
        questions[role + "Effort"] = _choice(
            f"Choose the lowest reasoning effort at which the {role} still gets this task right the first time: more for "
            "difficult, risky or large work, less for small, well-specified work. Choose even when uncertain, and report "
            "your confidence honestly.",
            EFFORT_OPTIONS,
        )
    return {"model": config.get("jev", {}).get("model"), "state": {"purpose": purpose, "context": context},
            "questions": questions}


def _load_key(config):
    settings = config.get("jev", {})
    key = os.environ.get(settings.get("keyEnv", "TYPESAFE_API_KEY"), "").strip()
    if not key:
        value = settings.get("keyFile", DEFAULT_KEY_FILE)
        if not isinstance(value, str) or not value or Path(value).is_absolute():
            return None, "invalid_key_file"
        path = (KEY_ROOT / value).resolve()
        if not path.is_relative_to(KEY_ROOT.resolve()):
            return None, "invalid_key_file"
        try:
            with path.open("rb") as source:
                raw = source.read(8193)
            if len(raw) > 8192:
                return None, "invalid_key_file"
            key = raw.decode("utf-8-sig").strip()
        except FileNotFoundError:
            return None, "missing_api_key"
        except (OSError, UnicodeDecodeError):
            return None, "key_file_unavailable"
    if not key:
        return None, "missing_api_key"
    labels = {"typesafe_api_key", settings.get("keyEnv", "TYPESAFE_API_KEY").lower()}
    if any(key.lower().startswith(label + separator) for label in labels for separator in (":", "=")):
        return None, "api_key_contains_label"
    if len(key) > 8192 or not key.isascii() or any(char.isspace() or ord(char) < 32 for char in key):
        return None, "invalid_api_key_format"
    return key, None


def _http_error(status):
    labels = {400: "invalid_request", 401: "authentication_failed", 403: "forbidden",
              404: "endpoint_or_model_unavailable", 413: "request_too_large",
              415: "unsupported_encoding", 422: "invalid_request", 429: "rate_limited"}
    return f"http_{status}_{labels.get(status, 'provider_unavailable' if status >= 500 else 'request_rejected')}"


def _decode_response(raw, status, client=None):
    if len(raw) > MAX_RESPONSE_BYTES:
        return None, "oversize_response"
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "invalid_response_json"
    if not isinstance(envelope, dict):
        return None, "invalid_response_json"
    envelope["_harnessTransport"] = {"httpStatus": status, "attempts": 1}
    if client:
        envelope["_harnessTransport"]["client"] = client
    return envelope, None


def _fetch_windows(key, payload):
    # The tested Windows path uses native Schannel, IPv4 and TLS 1.2. Python's
    # OpenSSL path reset connections here. No fallback/retry can duplicate a call.
    def quote(value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    settings = "\n".join([
        "url = " + quote(ENDPOINT),
        "header = " + quote("Authorization: Bearer " + key),
        'header = "Content-Type: application/json"',
        "data = " + quote(json.dumps(payload)),
    ])
    executable = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe")
    try:
        response = subprocess.run(
            [executable, "--disable", "--config", "-", "--ipv4", "--tlsv1.2", "--tls-max", "1.2",
             "--proto", "=https", "--silent", "--show-error", "--retry", "0",
             "--connect-timeout", "10", "--max-time", str(TIMEOUT_SECONDS),
             "--max-filesize", str(MAX_RESPONSE_BYTES), "--write-out", "\n%{http_code}"],
            input=settings, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=TIMEOUT_SECONDS + 5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return None, "request_timeout"
    except FileNotFoundError:
        return None, "windows_curl_unavailable"
    except OSError:
        return None, "connection_error"
    if response.returncode:
        return None, {28: "request_timeout", 63: "oversize_response"}.get(response.returncode, "connection_error")
    body, separator, code = response.stdout.rpartition("\n")
    if not separator or not code.isdigit() or not 100 <= int(code) <= 599:
        return None, "invalid_transport_response"
    status = int(code)
    if status != 200:
        return None, _http_error(status)
    return _decode_response(body.encode("utf-8"), status, "windows-curl")


def _fetch(config, payload):
    key, error = _load_key(config)
    if error:
        return None, error
    if USE_WINDOWS_CURL:
        return _fetch_windows(key, payload)
    request = Request(
        ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            status = response.status
        return _decode_response(raw, status)
    except HTTPError as error:
        # An error body may contain request material. Preserve only a safe code.
        status = error.code
        error.close()
        return None, _http_error(status)
    except TimeoutError:
        return None, "request_timeout"
    except URLError as error:
        return None, "request_timeout" if isinstance(error.reason, TimeoutError) else "connection_error"
    except OSError:
        return None, "connection_error"


def _answer(envelope, key, expected_options):
    answers = envelope.get("answers") if isinstance(envelope, dict) else None
    answer = answers.get(key) if isinstance(answers, dict) else None
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        return None, None
    choice = answer.get("choice")
    confidence = answer.get("confidence")
    probabilities = answer.get("probabilities")
    if (
        not isinstance(choice, str)
        or choice not in expected_options
        or isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or not isinstance(probabilities, dict)
        or set(probabilities) != set(expected_options)
    ):
        return None, None
    if not 0 <= confidence <= 1:
        return None, None
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 1
        for value in probabilities.values()
    ):
        return None, None
    if abs(sum(probabilities.values()) - 1) > 0.01:
        return None, None
    maximum = max(probabilities.values())
    if probabilities[choice] != maximum:
        return None, None
    return choice, float(confidence)


def evaluate(config, context, purpose="task", response=None, role="implementer"):
    """Jev's valid choices for one role of one task: {"status", "source", "selectedModels", "selectedEfforts", ...}. A
    failed request or an invalid response gives status "unavailable" with a reason and no choices.

    Supplying ``response`` skips networking and marks the result as a simulation (tests and previews).
    """
    source = "simulation" if response is not None else "jev"
    empty = {"purpose": purpose, "source": source, "selectedModels": {}, "selectedEfforts": {}}
    try:
        payload = build_request(config, context, purpose, role)
    except ValueError:
        return {"status": "unavailable", "reason": "invalid_context", **empty}
    if response is None:
        response, error = _fetch(config, payload)
        if error:
            return {"status": "unavailable", "reason": error, **empty}
    if not isinstance(response, dict) or response.get("model") != config.get("jev", {}).get("model"):
        return {"status": "unavailable", "reason": "invalid_response_model", **empty}
    # No confidence threshold: every valid answer applies; a missing or invalid one is left to the defaults.
    parsed = {key: _answer(response, key, question["criteria"]) for key, question in payload["questions"].items()}
    models = {role: parsed[role][0]} if parsed[role][0] is not None else {}
    efforts = {role: parsed[role + "Effort"][0]} if parsed.get(role + "Effort", (None,))[0] is not None else {}
    return {"status": "decided", **empty, "selectedModels": models, "selectedEfforts": efforts,
            "confidence": {key: answer[1] for key, answer in parsed.items()},
            "jevModel": config.get("jev", {}).get("model"), "jevUsage": response.get("usage"),
            "transport": response.get("_harnessTransport", {})}
