"""OpenAI planning and typed monitoring decisions over a shared HTTP transport."""

import base64
from dataclasses import dataclass, field
import io
import json
import os
from pathlib import Path
import re
from urllib import error, request

from PIL import Image
from intent import REVIEW_SCHEMA, json_response, validate_review

ALIGNMENT_SYSTEM_PROMPT = """Agree with the photographer on when to fire the shutter.
Explain your understanding concisely, follow corrections, and ask for clarification when needed.
Translate vague transitions into concrete visual criteria assessable from ONE current preview.
The evaluator will receive only the approved condition and that preview, without a reference image or history.
Make condition self-contained: describe the subject, location and visible criteria explicitly.
Do not promise motion tracking, identity tracking, comparisons to earlier images, or duration detection.
When those are essential, ask the user to agree on a static alternative before clearing question.
Highlight visible objects in the preview with boxes if helpful.
Save qualifying photos for the photographer to review until they stop watching or the video ends.
There is no photo-count limit or once-per-occurrence restriction. Keep quantity out of the visual condition.
Timing and sampling are controlled by the user's schedule settings."""

MONITORING_SYSTEM_PROMPT = """Does this preview satisfy all of the approved visual conditions?
Evaluate the visible evidence in this image only. Do not infer changes over time."""

API_ENDPOINT = "https://api.openai.com/v1/decisions"
RESPONSES_ENDPOINT = "https://api.openai.com/v1/responses"
TOKEN_COUNT_ENDPOINT = RESPONSES_ENDPOINT + "/input_tokens"
MODEL = "gpt-6-luna"
PLANNING_MODEL = "gpt-6-astra"
EFFORT = "medium"
MAX_OUTPUT_TOKENS = 8192
MAX_IMAGES = 600
MAX_REQUEST_BYTES = 30_000_000
MAX_INPUT_TOKENS = 990_000


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
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": encode_image(image)}}


def text_content(value) -> dict:
    return {"type": "text", "text": json.dumps(value, ensure_ascii=False)}


def openai_input(messages):
    """Encode the planning discussion and its preview as API input."""
    result = []
    for message in messages:
        content = []
        for block in message["content"]:
            if block["type"] == "image":
                content.append({"type": "input_image", "image_url":
                                "data:image/jpeg;base64," + block["source"]["data"]})
            else:
                content.append({"type": "input_text", "text": block["text"]})
        # Include the speaker label with each recorded planning turn.
        result.append({"role": "user", "content": [
            {"type": "input_text", "text": "Recorded role: " + message["role"]}, *content]})
    return result


def make_monitoring_payload(condition, image):
    return {"model": MODEL, "input": [{"role": "user", "content": [
        {"type": "input_text", "text": condition},
        {"type": "input_image", "image_url": "data:image/jpeg;base64," + encode_image(image)},
    ]}], "questions": [{
        "type": "predicate", "name": "condition", "instructions": MONITORING_SYSTEM_PROMPT,
    }]}


def make_alignment_payload(request_text, image, conversation, context):
    """Alignment has its own discussion and response format, with no capture tools."""
    messages = []
    for turn in conversation or [{"role": "user", "text": request_text}]:
        if turn["role"] == "assistant":
            content = {"reply": turn["text"], "condition": turn["condition"], "question": turn["question"],
                       "highlights_normalized": turn.get("targets", []), "reference_id": turn.get("reference_id")}
        else:
            content = {"message": turn["text"]}
        block = text_content(content)
        if messages and messages[-1]["role"] == turn["role"]:
            messages[-1]["content"].append(block)
        else:
            messages.append({"role": turn["role"], "content": [block]})
    evidence = [text_content({"image_width": image.width, "image_height": image.height, **context}), image_content(image)]
    if messages[-1]["role"] == "user":
        messages[-1]["content"].extend(evidence)
    else:
        messages.append({"role": "user", "content": evidence})
    return {"model": PLANNING_MODEL, "max_output_tokens": MAX_OUTPUT_TOKENS,
            "instructions": ALIGNMENT_SYSTEM_PROMPT, "reasoning": {"effort": EFFORT},
            "input": openai_input(messages), "store": False,
            "text": {"format": {"type": "json_schema", "name": "review",
                                "strict": True, "schema": REVIEW_SCHEMA}}}


def review_intent(request_text, image, config, *, conversation=None, previous=None, reference_changed=False):
    """Explain the shutter condition with optional, model-controlled highlights."""
    image = model_image(image)
    payload = make_alignment_payload(request_text, image, conversation,
                                     {"previous_interpretation": previous, "previous_box_units": "normalized_0_1",
                                      "reference": "NEW reference image" if reference_changed else "CURRENT saved reference image"})
    check_capacity(payload)
    count_tokens(payload, config)
    return validate_review(json_response(call_model(payload, config)), width=image.width, height=image.height)


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


class CapacityReached(RuntimeError):
    def __init__(self, resource, used, limit):
        self.details = {"resource": resource, "used": used, "limit": limit}
        super().__init__(f"Conversation capacity reached: {resource}={used}, limit={limit}")


def check_capacity(payload: dict) -> dict:
    images = sum(block.get("type") == "input_image"
                 for message in payload["input"] for block in message["content"])
    size = len(encode_payload(payload))
    for resource, used, limit in (("images", images, MAX_IMAGES), ("request_bytes", size, MAX_REQUEST_BYTES)):
        if used > limit:
            raise CapacityReached(resource, used, limit)
    return {"images": images, "request_bytes": size}


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


def _post(endpoint: str, payload: dict, config: ApiConfig, timeout: float) -> dict:
    validate_config(config)
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": "WaitLess", "Authorization": "Bearer " + config.api_key}
    if config.project_id:
        headers["OpenAI-Project"] = config.project_id
    req = request.Request(endpoint, data=encode_payload(payload), headers=headers, method="POST")
    try:
        with request.build_opener(NoRedirect()).open(req, timeout=timeout) as response:
            return json.load(response)
    except error.HTTPError as exc:
        raise api_error(exc) from None
    except (error.URLError, TimeoutError):
        raise RuntimeError("API connection failed or timed out.") from None


def decision_probability(response):
    answers = response.get("answers") if isinstance(response, dict) else None
    if (not isinstance(answers, list) or len(answers) != 1 or not isinstance(answers[0], dict)
            or answers[0].get("type") != "predicate" or answers[0].get("name") != "condition"
            or type(answers[0].get("probability")) not in (int, float)
            or not 0 <= answers[0]["probability"] <= 1):
        raise ValueError("Missing, refused, or invalid condition decision")
    return answers[0]["probability"]


def call_model(payload: dict, config: ApiConfig, *, timeout=90) -> dict:
    if "questions" in payload:
        response = _post(API_ENDPOINT, payload, config, timeout)
        decision_probability(response)  # Validate before returning anything to the capture loop.
        return response
    return _post(RESPONSES_ENDPOINT, payload, config, timeout)


def count_tokens(payload: dict, config: ApiConfig, *, timeout=90) -> int:
    counting = {key: payload[key] for key in ("model", "input", "instructions", "text", "reasoning") if key in payload}
    response = _post(TOKEN_COUNT_ENDPOINT, counting, config, timeout)
    tokens = response.get("input_tokens") if isinstance(response, dict) else None
    if type(tokens) is not int or tokens < 0:
        raise ValueError("OpenAI returned an invalid token count")
    if tokens > MAX_INPUT_TOKENS:
        raise CapacityReached("input_tokens", tokens, MAX_INPUT_TOKENS)
    return tokens
