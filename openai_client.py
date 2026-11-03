"""Shared OpenAI credentials, HTTP transport, and image encoding."""

import base64
from dataclasses import dataclass, field
import io
import json
import os
from pathlib import Path
import re
from urllib import error, request

from PIL import Image


@dataclass(frozen=True)
class ApiConfig:
    """Credentials for the fixed OpenAI endpoints."""

    api_key: str = field(default="", repr=False)
    project_id: str = ""


def load_config(env_file: Path = Path(__file__).with_name(".env")) -> ApiConfig:
    values = {}
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.lstrip().startswith("#"):
                continue
            name, separator, value = line.strip().removeprefix("export ").partition("=")
            if separator:
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[name.strip()] = value
    values.update(os.environ)
    return ApiConfig(
        api_key=values.get("OPENAI_API_KEY", "").strip(),
        project_id=values.get("OPENAI_PROJECT_ID", "").strip(),
    )


def validate_config(config: ApiConfig) -> None:
    if not config.api_key:
        raise ValueError("Set OPENAI_API_KEY in the project .env file.")


def model_image(source: Image.Image) -> Image.Image:
    image = source.convert("RGB")
    image.thumbnail((960, 960))
    return image


def encode_image(source: Image.Image) -> str:
    image = model_image(source)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def image_content(image: Image.Image) -> dict:
    return {"type": "input_image", "image_url": "data:image/jpeg;base64," + encode_image(image)}


def text_content(value) -> dict:
    """Text block for local observation logs."""
    return {"type": "text", "text": json.dumps(value, ensure_ascii=False)}


def response_usage(response: dict) -> dict:
    usage = response.get("usage", {})
    if not isinstance(usage, dict):
        return {}
    result = {key: usage[key] for key in ("input_tokens", "output_tokens")
              if type(usage.get(key)) is int and usage[key] >= 0}
    details = usage.get("input_tokens_details", {})
    if isinstance(details, dict) and type(details.get("cached_tokens")) is int:
        result["cache_read_input_tokens"] = details["cached_tokens"]
    return result


def encode_payload(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ApiError(RuntimeError):
    """Only allowlisted diagnostic fields are safe to log or include in reports."""

    def __init__(self, details: dict):
        self.details = details
        fields = ", ".join(f"{name}={value}" for name, value in details.items())
        super().__init__(f"OpenAI API error: {fields}.")


def api_error(exc: error.HTTPError) -> ApiError:
    details = {"http_status": exc.code}
    try:
        body = json.loads(exc.read(16384))
    except (ValueError, OSError):
        body = {}
    failure = body.get("error", {}) if isinstance(body, dict) else {}
    if isinstance(failure, dict):
        kind = failure.get("type")
        allowed = {"invalid_request_error", "authentication_error", "permission_error", "not_found_error",
                   "request_too_large", "rate_limit_error", "api_error", "overloaded_error",
                   "timeout_error", "billing_error", "conflict_error"}
        if isinstance(kind, str) and kind in allowed:
            details["error_type"] = kind
        # Classify common account problems without exposing free-form provider text.
        message = failure.get("message", "")
        if isinstance(message, str):
            message = message.lower()
            if "credit balance" in message and any(term in message for term in ("low", "insufficient", "exhaust")):
                details["reason"] = "insufficient_credits"
            elif "spend" in message and any(term in message for term in ("limit", "cap")):
                details["reason"] = "spend_limit"
            elif "project" in message and "required" in message:
                details["reason"] = "project_id_required"
    headers = exc.headers or {}
    retry = headers.get("retry-after", "")
    if re.fullmatch(r"[0-9]{1,7}(?:\.[0-9]{1,3})?", retry):
        details["retry_after_s"] = float(retry)
    request_id = headers.get("x-request-id", "")
    if re.fullmatch(r"req_[A-Za-z0-9]{8,96}", request_id):
        details["request_id"] = request_id
    return ApiError(details)


def post_json(endpoint: str, payload: dict, config: ApiConfig) -> dict:
    validate_config(config)
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": "WaitLess", "Authorization": "Bearer " + config.api_key}
    if config.project_id:
        headers["OpenAI-Project"] = config.project_id
    req = request.Request(endpoint, data=encode_payload(payload), headers=headers, method="POST")
    try:
        with request.build_opener(NoRedirect()).open(req, timeout=None) as response:
            return json.load(response)
    except error.HTTPError as exc:
        raise api_error(exc) from None
    except (error.URLError, TimeoutError):
        raise RuntimeError("API connection failed or timed out.") from None
