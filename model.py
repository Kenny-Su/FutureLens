"""Separate alignment and monitoring modes over the shared Claude transport."""

import base64
from dataclasses import dataclass, field
import io
import json
import os
from pathlib import Path
import re
from urllib import error, request

from PIL import Image
from intent import (REVIEW_SCHEMA, SNAPSHOT_SCHEMA, WAIT_SCHEMA,
                    json_response, validate_review)

ALIGNMENT_SYSTEM_PROMPT = """Agree with the photographer on when to fire the shutter.
Explain your understanding concisely, follow corrections, and ask for clarification when needed.
Highlight visible objects in the preview with boxes if helpful.
Keep condition as the complete agreed shutter rule."""

MONITORING_SYSTEM_PROMPT = """Evaluate the CURRENT preview against the approved shutter condition.
Use the REFERENCE image and earlier previews as context.
Call shoot when the condition is met and a requested photo is still due; otherwise return a wait assessment.
Respect the agreed quantity, repetition, and timing. Report uncertain when you cannot determine whether the condition is met."""

API_VERSION = "2026-10-21"
COMPACTION_BETA = "compact-2026-09-04"
API_ENDPOINT = "https://api.anthropic.com/v1/messages"
TOKEN_COUNT_ENDPOINT = API_ENDPOINT + "/count_tokens"
MODEL = "claude-haiku-5-5"
EFFORT = "medium"  # Haiku's standard API default.
THINKING = {"type": "adaptive", "display": "omitted"}
MAX_OUTPUT_TOKENS = 8192
MAX_IMAGES = 600
MAX_REQUEST_BYTES = 30_000_000
MAX_INPUT_TOKENS = 990_000
MAX_CALLS_PER_OBSERVATION = 6
MONITORING_TOOLS = [
    {"name": "list_history", "description": "List earlier preview IDs, timestamps, and capture outcomes, newest first. Time filters are inclusive seconds since session start; cursor is an exclusive preview ID. Use next_cursor for pagination. Default limit: 20.",
     "input_schema": {"type": "object", "properties": {
         "start_s": {"type": "number", "minimum": 0}, "end_s": {"type": "number", "minimum": 0},
         "cursor": {"type": "integer", "minimum": 0},
         "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, "additionalProperties": False}},
    {"name": "get_history", "description": "Retrieve earlier preview images and capture outcomes by frame ID.",
     "input_schema": {"type": "object", "properties": {
         "frame_ids": {"type": "array", "items": {"type": "integer", "minimum": 0},
                       "minItems": 1, "maxItems": 8, "uniqueItems": True}},
         "required": ["frame_ids"], "additionalProperties": False}},
    {"name": "shoot", "description": "Request one photograph based on the latest preview. Ends this observation even if capture is skipped. Only downloaded confirms success. Do not retry an uncertain capture.",
     "input_schema": {"type": "object", "properties": {"snapshot": SNAPSHOT_SCHEMA},
                      "required": ["snapshot"], "additionalProperties": False}},
]


@dataclass(frozen=True)
class ApiConfig:
    """Credentials for the fixed Anthropic endpoint and model."""

    api_key: str = field(default="", repr=False)
    workspace_id: str = ""


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
        api_key=values.get("ANTHROPIC_API_KEY", "").strip(),
        workspace_id=values.get("ANTHROPIC_WORKSPACE_ID", "").strip(),
    )


def validate_config(config: ApiConfig) -> None:
    if not config.api_key:
        raise ValueError("Set ANTHROPIC_API_KEY in the project .env file.")


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


def _base_payload(messages, *, system, schema):
    """Shared model settings; each mode supplies its own task and response format."""
    return {
        "model": MODEL,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "system": system,
        "thinking": dict(THINKING),
        "messages": list(messages),
        "cache_control": {"type": "ephemeral"},
        "output_config": {"effort": EFFORT, "format": {"type": "json_schema", "schema": schema}},
    }


def make_monitoring_payload(messages):
    payload = _base_payload(messages, system=MONITORING_SYSTEM_PROMPT, schema=WAIT_SCHEMA)
    payload.update(tools=list(MONITORING_TOOLS),
                   tool_choice={"type": "auto", "disable_parallel_tool_use": True})
    return payload


def make_compaction_payload(messages):
    payload = make_monitoring_payload(messages)
    payload["output_config"].pop("format")
    payload["compaction"] = {"type": "summarize"}
    return payload


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
    return _base_payload(messages, system=ALIGNMENT_SYSTEM_PROMPT, schema=REVIEW_SCHEMA)


def review_intent(request_text, image, config, *, conversation=None, previous=None, reference_changed=False):
    """Explain the shutter condition with optional, model-controlled highlights."""
    image = model_image(image)
    payload = make_alignment_payload(request_text, image, conversation,
                                     {"previous_interpretation": previous, "previous_box_units": "normalized_0_1",
                                      "reference": "NEW reference image" if reference_changed else "CURRENT saved reference image"})
    check_capacity(payload)
    count_tokens(payload, config)
    return validate_review(json_response(call_model(payload, config)), width=image.width, height=image.height)


def parse_compaction(response: dict) -> dict:
    if not isinstance(response, dict) or response.get("stop_reason") != "compaction":
        raise ValueError("Compaction did not produce a summary")
    content = response.get("content")
    if (not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict)
            or content[0].get("type") != "compaction"
            or not isinstance(content[0].get("content"), str) or not content[0]["content"].strip()
            or not isinstance(content[0].get("signature"), str) or not content[0]["signature"]):
        raise ValueError("Invalid compaction block")
    return {"role": "assistant", "content": content}


def response_usage(response: dict) -> dict:
    """Count iterations once, including cache tokens; never copy provider text."""
    usage = response.get("usage", {})
    if not isinstance(usage, dict) or not usage:
        return {}
    iterations = usage.get("iterations")
    rows = iterations if isinstance(iterations, list) else [usage]
    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    return {key: sum(row[key] for row in rows if isinstance(row, dict)
                     and type(row.get(key)) is int and row[key] >= 0) for key in keys}


def encode_payload(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class CapacityReached(RuntimeError):
    def __init__(self, resource, used, limit):
        self.details = {"resource": resource, "used": used, "limit": limit}
        super().__init__(f"Conversation capacity reached: {resource}={used}, limit={limit}")


def check_capacity(payload: dict) -> dict:
    def count_images(blocks):
        return sum(1 if block["type"] == "image" else
                   count_images(block["content"]) if block["type"] == "tool_result" and isinstance(block.get("content"), list)
                   else 0 for block in blocks)
    images = sum(count_images(message["content"]) for message in payload["messages"])
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
        super().__init__(f"Anthropic API error: {fields}.")


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
            elif "workspace" in message and "required" in message:
                details["reason"] = "workspace_id_required"
    headers = exc.headers or {}
    retry = headers.get("retry-after", "")
    if re.fullmatch(r"[0-9]{1,7}(?:\.[0-9]{1,3})?", retry):
        details["retry_after_s"] = float(retry)
    request_id = headers.get("request-id", "")
    if re.fullmatch(r"req_[A-Za-z0-9]{8,96}", request_id):
        details["request_id"] = request_id
    return ApiError(details)


def _post(endpoint: str, payload: dict, config: ApiConfig, timeout: float) -> dict:
    validate_config(config)
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": "WaitLess", "x-api-key": config.api_key,
               "anthropic-version": API_VERSION, "anthropic-beta": COMPACTION_BETA}
    if config.workspace_id:
        headers["anthropic-workspace-id"] = config.workspace_id
    req = request.Request(endpoint, data=encode_payload(payload), headers=headers, method="POST")
    try:
        with request.build_opener(NoRedirect()).open(req, timeout=timeout) as response:
            return json.load(response)
    except error.HTTPError as exc:
        raise api_error(exc) from None
    except (error.URLError, TimeoutError):
        raise RuntimeError("API connection failed or timed out.") from None


def call_model(payload: dict, config: ApiConfig, *, timeout=90) -> dict:
    return _post(API_ENDPOINT, payload, config, timeout)


def count_tokens(payload: dict, config: ApiConfig, *, timeout=90) -> int:
    counting = {key: value for key, value in payload.items() if key != "max_tokens"}
    response = _post(TOKEN_COUNT_ENDPOINT, counting, config, timeout)
    tokens = response.get("input_tokens") if isinstance(response, dict) else None
    if type(tokens) is not int or tokens < 0:
        raise ValueError("Anthropic returned an invalid token count")
    if tokens > MAX_INPUT_TOKENS:
        raise CapacityReached("input_tokens", tokens, MAX_INPUT_TOKENS)
    return tokens


def parse_turn(response: dict, seen_ids: set) -> dict | None:
    """Validate the complete turn before any tool can cause a side effect."""
    if not isinstance(response, dict) or response.get("stop_reason") not in ("end_turn", "tool_use"):
        raise ValueError("Claude did not complete a turn (refused, truncated, or invalid response).")
    content = response.get("content")
    if not isinstance(content, list):
        raise ValueError("Invalid model content")
    calls = []
    for block in content:
        if not isinstance(block, dict):
            raise ValueError("Invalid model content block")
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            continue
        # Thinking blocks are opaque context, not actions. Keep the original blocks
        # unchanged in the assistant message so subsequent tool turns retain them.
        if block.get("type") == "thinking":
            if (not isinstance(block.get("thinking"), str)
                    or not isinstance(block.get("signature"), str) or not block["signature"]):
                raise ValueError("Invalid thinking block")
            continue
        if block.get("type") == "redacted_thinking":
            if not isinstance(block.get("data"), str) or not block["data"]:
                raise ValueError("Invalid redacted thinking block")
            continue
        if block.get("type") != "tool_use":
            raise ValueError("Unexpected model content block")
        if (block.get("name") not in {tool["name"] for tool in MONITORING_TOOLS}
                or not isinstance(block.get("id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", block["id"])
                or not isinstance(block.get("input"), dict)):
            raise ValueError("Invalid tool call")
        json.dumps(block["input"], allow_nan=False)
        if block["name"] == "shoot" and set(block["input"]) - {"snapshot"}:
            raise ValueError("shoot accepts only an observation snapshot")
        calls.append(block)
    if response["stop_reason"] == "end_turn":
        if calls:
            raise ValueError("Tool call in a completed turn")
        return None
    if len(calls) != 1:
        raise ValueError("Expected exactly one tool call")
    call = calls[0]
    if call["id"] in seen_ids:
        raise ValueError("Repeated tool call ID; refusing to execute it again")
    seen_ids.add(call["id"])
    return call
