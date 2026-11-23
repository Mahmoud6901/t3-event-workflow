"""Jev's advice for one new worker of the delegated PR workflow: its model. Jev sets no effort.

The harness owns the workflow and its defaults. This module only builds one request, validates one response and keeps
the valid choices; anything missing or invalid is left to the harness (flow.default_jev): an implementer's model to the
fallback or the best scored (ranked) of its options, the rest to the defaults. Jev reads each option's ability card
(assets/cards.json), its record in the owner's projects, its board scores (assets/scores.json) and its route.
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
SCORES_FILE = Path(__file__).resolve().parent.parent / "assets" / "scores.json"  # Refreshed by scores.py.
CARDS_FILE = Path(__file__).resolve().parent.parent / "assets" / "cards.json"  # One ability card per approved model.
USE_WINDOWS_CURL = os.name == "nt"


def cards():
    """The shipped ability cards, {model: {"maker", "read", "good_at", "weak_at", "sources"}}; {} when they cannot be
    read. A model without a card is never offered."""
    try:
        found = json.loads(CARDS_FILE.read_text(encoding="utf-8")).get("models")
    except (OSError, ValueError, AttributeError):
        return {}
    return {m: card for m, card in found.items() if isinstance(card, dict)} if isinstance(found, dict) else {}


def _eligible(config, role):
    models = config.get("models") if isinstance(config.get("models"), dict) else {}
    return {model_id: spec for model_id, spec in models.items() if isinstance(spec, dict) and role in spec.get("roles", [])}


def _choice(instructions, options):
    return {"type": "choice", "instructions": instructions, "criteria": options}


def _scores():
    """The shipped scores by board, {key: {"label", "scale", "suites", "models", ...}}; {} when they cannot be read."""
    try:
        boards = json.loads(SCORES_FILE.read_text(encoding="utf-8")).get("boards")
    except (OSError, ValueError, AttributeError):
        return {}
    return boards if isinstance(boards, dict) else {}


def ranked(model_ids):
    """The models best first, by their average over every suite score of every board; one no board scored comes last,
    and a tie keeps the given order."""
    boards = _scores()

    def average(model_id):
        found = [score for board in boards.values()
                 for key, score in ((board.get("models") or {}).get(model_id) or {}).items()
                 if key in (board.get("suites") or {}) and isinstance(score, (int, float))]
        return sum(found) / len(found) if found else -math.inf
    return sorted(model_ids, key=average, reverse=True)


def _meaning(boards):
    """What the options' scores are and measure, board by board and suite by suite, ending in a space; "" without any."""
    if not boards:
        return ""
    return ("Scores come from boards whose maintainers run every model on tasks they do not publish, higher being "
            "better; unknown there is not weaker. "
            + " ".join(board["label"] + (f", scored {board['scale']}" if board.get("scale") else "") + ": "
                       + "; ".join(f"{suite['name']}, {suite['measures']}" for suite in (board.get("suites") or {}).values())
                       + "." for board in boards.values()) + " ")


def _scored(model_id, boards):
    """The model's scores on each board, as one line (a stand-in's named as such)."""
    shown = []
    for board in boards.values():
        found = (board.get("models") or {}).get(model_id) or {}
        scored = ", ".join(f"{suite['name']} {found[key]}" for key, suite in (board.get("suites") or {}).items()
                           if key in found)
        stand_in = f"{found['borrowedFrom']}'s " if scored and found.get("borrowedFrom") else ""
        shown.append(f"{stand_in}{board['label']} {scored or 'unknown'}")
    return "; ".join(shown) or "unknown"


def _label(model_id, spec, boards, card):
    """The option Jev reads: the model; what its card says it is good and weak at; its record in the owner's projects;
    its scores; and whether its route is a subscription or paid per token."""
    route = spec.get("route") or {"provider": spec.get("provider")}
    paid = route.get("paid", route.get("provider") == "acpRegistry_goose")
    return (f"{model_id}. Good at: {'; '.join(card.get('good_at') or []) or 'unknown'}. "
            f"Weak at: {'; '.join(card.get('weak_at') or []) or 'unknown'}. "
            f"Record in the owner's projects: {spec.get('record') or 'no record yet'}. "
            f"Scores: {_scored(model_id, boards)}. {'Paid per token' if paid else 'Subscription'}.")


INSTRUCTIONS = (
    "Choose the approved {role} model most likely to get this task right the first time. First size the change from "
    "the task itself, not from how long its brief is or the rules every task of its plan repeats: small and contained "
    "(a fix or tweak in a few places, with a clear finish), large (a feature across many files, a migration, a long "
    "investigation, many steps), or between. Then choose the option whose strengths fit this task's size and kind and "
    "whose weaknesses matter least for it. Each option shows what its card says it is good and weak at (a maker's "
    "claim is marked so), its record in the owner's projects by size, its published scores, and whether it is a "
    "subscription or paid per token. Once a record covers several tasks of this size, it outweighs published scores "
    "and makers' claims. The scores are one piece of evidence, not the rule. {meaning}Price is not a reason either "
    "way. Choose even when uncertain, and report your confidence honestly.")


def build_request(config, context, purpose="task", role="implementer"):
    """The Jev request for one role of one task. Raises ValueError for another purpose or role, or a context JSON
    cannot hold. Each option's spec may carry its route and its record line (flow.Flow.ask_jev adds them)."""
    if purpose != "task":
        raise ValueError("purpose must be 'task'")
    if role not in ROLES:
        raise ValueError(f"role is one of {', '.join(ROLES)}")
    try:
        context = json.loads(json.dumps(context, ensure_ascii=False))
    except (TypeError, ValueError):
        raise ValueError("context must be JSON serializable") from None
    questions, scores, known = {}, _scores(), cards()
    options = {model_id: _label(model_id, spec, scores, known.get(model_id) or {})
               for model_id, spec in _eligible(config, role).items()}
    # No lean toward the defaults or toward the cheaper: Jev weighs every option by what it can do for this task.
    questions[role] = _choice(INSTRUCTIONS.format(role=role, meaning=_meaning(scores)), options)
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
    """Jev's valid choice for one role of one task: {"status", "source", "selectedModels", ...}. A
    failed request or an invalid response gives status "unavailable" with a reason and no choices.

    Supplying ``response`` skips networking and marks the result as a simulation (tests and previews).
    """
    source = "simulation" if response is not None else "jev"
    empty = {"purpose": purpose, "source": source, "selectedModels": {}}
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
    # No confidence threshold: every valid answer applies; a missing or invalid one is left to the harness.
    parsed = {key: _answer(response, key, question["criteria"]) for key, question in payload["questions"].items()}
    models = {role: parsed[role][0]} if parsed[role][0] is not None else {}
    return {"status": "decided", **empty, "selectedModels": models,
            "confidence": {key: answer[1] for key, answer in parsed.items()},
            "jevModel": config.get("jev", {}).get("model"), "jevUsage": response.get("usage"),
            "transport": response.get("_harnessTransport", {})}
