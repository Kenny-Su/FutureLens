"""Offline checks for native tool turns, capacity preflight, and API transport."""

import base64
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from PIL import Image

import model


def tool(name, arguments=None, call_id="call-1"):
    return {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": call_id,
            "name": name, "input": arguments or {}}]}


def answer(action):
    if action == "wait":
        return {"model": model.MODEL, "stop_reason": "end_turn", "content": [{"type": "text", "text": "No photo."}]}
    return tool(action)


def compacted(text="Prior session observations", signature="signed-summary"):
    return {"stop_reason": "compaction", "content": [
        {"type": "compaction", "content": text, "signature": signature}],
        "usage": {"input_tokens": 0, "output_tokens": 0, "iterations": [
            {"type": "compaction", "input_tokens": 1000, "output_tokens": 80}]}}


class ModelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = model.ApiConfig(api_key="offline-test-key")

    def test_image_encoding_and_cached_conversation_payload(self):
        image = Image.new("RGB", (1920, 1080), "green")
        messages = [{"role": "user", "content": [model.image_content(image)]}]
        payload = model.make_payload(messages)
        self.assertEqual(payload["model"], "claude-sonnet-5-5")
        self.assertEqual([t["name"] for t in payload["tools"]], ["list_history", "get_history", "shoot", "set_preview_interval"])
        self.assertEqual(payload["tool_choice"], {"type": "auto", "disable_parallel_tool_use": True})
        self.assertEqual(payload["cache_control"], {"type": "ephemeral"})
        encoded = base64.b64decode(payload["messages"][0]["content"][0]["source"]["data"])
        with Image.open(io.BytesIO(encoded)) as sent:
            self.assertEqual(sent.size, (960, 540))
        self.assertEqual(image.size, (1920, 1080))
        self.assertNotIn("format", payload["output_config"])
        self.assertEqual(payload["thinking"], {"type": "adaptive", "display": "omitted"})
        self.assertEqual(payload["output_config"]["effort"], "high")
        self.assertEqual(payload["max_tokens"], 8192)
        messages.append({"role": "assistant", "content": answer("wait")["content"]})
        self.assertEqual(len(payload["messages"]), 1)

    def test_only_one_valid_tool_or_end_turn_is_accepted(self):
        self.assertIsNone(model.parse_turn(answer("wait"), set()))
        self.assertEqual(model.parse_turn(answer("shoot"), set())["name"], "shoot")
        invalid = [{}, None, answer("unknown"),
                   {**answer("shoot"), "stop_reason": "refusal"},
                   {**answer("shoot"), "stop_reason": "max_tokens"},
                   {**answer("shoot"), "stop_reason": "end_turn"},
                   {**answer("shoot"), "content": [None]},
                   {**answer("shoot"), "content": []},
                   {**answer("shoot"), "content": answer("shoot")["content"] * 2},
                   tool("shoot", {"extra": 1}), tool("get_history", {"frame_ids": float("nan")})]
        for response in invalid:
            with self.subTest(response=response), self.assertRaises(ValueError):
                model.parse_turn(response, set())
        seen = set()
        model.parse_turn(tool("list_history"), seen)
        with self.assertRaises(ValueError):
            model.parse_turn(tool("shoot"), seen)

    def test_image_and_exact_encoded_byte_capacity(self):
        payload = model.make_payload([{"role": "user", "content": [model.image_content(Image.new("RGB", (8, 8)))]}])
        size = len(model.encode_payload(payload))
        with patch("model.MAX_REQUEST_BYTES", size):
            self.assertEqual(model.check_capacity(payload), {"images": 1, "request_bytes": size})
        with patch("model.MAX_REQUEST_BYTES", size - 1), self.assertRaises(model.CapacityReached) as failure:
            model.check_capacity(payload)
        self.assertEqual(failure.exception.details, {"resource": "request_bytes", "used": size, "limit": size - 1})
        image = payload["messages"][0]["content"][0]
        payload["messages"][0]["content"] = [{"type": "tool_result", "tool_use_id": "images", "content": [image] * 600}]
        self.assertEqual(model.check_capacity(payload)["images"], 600)
        payload["messages"][0]["content"][0]["content"].append(image)
        with self.assertRaises(model.CapacityReached) as failure:
            model.check_capacity(payload)
        self.assertEqual(failure.exception.details["resource"], "images")
        self.assertEqual(len(payload["messages"][0]["content"][0]["content"]), 601)

    def test_thinking_blocks_are_context_and_do_not_relax_tool_validation(self):
        blocks = [{"type": "thinking", "thinking": "", "signature": "opaque-signature"},
                  {"type": "redacted_thinking", "data": "opaque-encrypted-data"}]
        for action in (answer("wait"), tool("set_preview_interval", {"seconds": 10}), answer("shoot")):
            response = {**action, "content": blocks + action["content"]}
            parsed = model.parse_turn(response, set())
            self.assertEqual(parsed, action["content"][0] if action["stop_reason"] == "tool_use" else None)
            self.assertEqual(response["content"][:2], blocks)
        invalid = [{"type": "thinking", "thinking": ""},
                   {"type": "thinking", "thinking": None, "signature": "signed"},
                   {"type": "thinking", "thinking": "", "signature": ""},
                   {"type": "redacted_thinking", "data": None},
                   {"type": "redacted_thinking", "data": ""}]
        for block in invalid:
            with self.subTest(block=block), self.assertRaises(ValueError):
                model.parse_turn({**answer("shoot"), "content": [block, *answer("shoot")["content"]]}, set())
        for content in (blocks + answer("shoot")["content"] * 2, blocks + tool("shoot", {"extra": 1})["content"]):
            with self.subTest(content=content), self.assertRaises(ValueError):
                model.parse_turn({"stop_reason": "tool_use", "content": content}, set())
        with self.assertRaises(ValueError):
            model.parse_turn({"stop_reason": "max_tokens", "content": blocks + answer("shoot")["content"]}, set())

    def test_token_count_checks_the_same_prompt_format_and_cache_settings(self):
        payload = model.make_payload([{"role": "user", "content": [model.text_content("sunrise")]}])
        with patch("model.request.build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(b'{"input_tokens":990000}')
            self.assertEqual(model.count_tokens(payload, self.config, timeout=7), 990000)
        sent = opener.return_value.open.call_args.args[0]
        self.assertEqual(sent.full_url, model.TOKEN_COUNT_ENDPOINT)
        self.assertEqual(json.loads(sent.data), {k: v for k, v in payload.items() if k != "max_tokens"})
        self.assertEqual(opener.return_value.open.call_args.kwargs["timeout"], 7)
        with patch("model._post", return_value={"input_tokens": 990001}), self.assertRaises(model.CapacityReached):
            model.count_tokens(payload, self.config)
        for invalid in ({}, None, {"input_tokens": True}, {"input_tokens": -1}, {"input_tokens": "2"}):
            with self.subTest(response=invalid), patch("model._post", return_value=invalid), self.assertRaises(ValueError):
                model.count_tokens(payload, self.config)

    @patch.dict("os.environ", {}, clear=True)
    def test_anthropic_config_and_literal_env_values(self):
        env = self.root / ".env"
        env.write_text('ANTHROPIC_API_KEY="test-$(do-not-execute)"\n')
        config = model.load_config(env)
        self.assertEqual(config.api_key, "test-$(do-not-execute)")
        self.assertNotIn(config.api_key, repr(config))

    @patch.dict("os.environ", {"ANTHROPIC_BASE_URL": "https://example.com/gateway/v1",
                               "ANTHROPIC_MODEL": "other-model"}, clear=True)
    def test_api_uses_only_anthropic_endpoint_and_sonnet(self):
        env = self.root / ".env"
        env.write_text('ANTHROPIC_API_KEY=test-key\nANTHROPIC_WORKSPACE_ID=wrkspc_test\n'
                       'ANTHROPIC_BASE_URL=https://example.com/another-gateway/v1\n'
                       'ANTHROPIC_MODEL=another-model\n')
        config = model.load_config(env)
        payload = model.make_payload([])
        with patch("model.request.build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(json.dumps(answer("wait")).encode())
            response = model.call_model(payload, config, timeout=2.5)
            self.assertIsNone(model.parse_turn(response, set()))
            self.assertEqual(opener.return_value.open.call_args.kwargs["timeout"], 2.5)
        sent = opener.return_value.open.call_args.args[0]
        self.assertEqual(sent.full_url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(sent.data, model.encode_payload(payload))
        self.assertEqual(json.loads(sent.data)["model"], "claude-sonnet-5-5")
        self.assertEqual(sent.get_header("X-api-key"), "test-key")
        self.assertEqual(sent.get_header("Anthropic-version"), model.API_VERSION)
        self.assertEqual(sent.get_header("Anthropic-beta"), model.COMPACTION_BETA)
        self.assertEqual(sent.get_header("Anthropic-workspace-id"), "wrkspc_test")

    def test_compaction_request_and_signed_result_protocol(self):
        messages = [{"role": "user", "content": [model.text_content("session")]}]
        ordinary = model.make_payload(messages)
        summary = model.make_compaction_payload(messages)
        self.assertEqual({k: v for k, v in summary.items() if k != "compaction"}, ordinary)
        self.assertEqual(summary["compaction"]["type"], "summarize")
        self.assertNotIn("context_management", summary)
        response = compacted()
        self.assertEqual(model.parse_compaction(response), {"role": "assistant", "content": response["content"]})
        self.assertEqual(model.response_usage(response)["output_tokens"], 80)
        response["usage"]["output_tokens"] = 80
        self.assertEqual(model.response_usage(response)["output_tokens"], 80)  # No double counting.
        for invalid in (None, {}, answer("shoot"), {**response, "stop_reason": "max_tokens"},
                        {**response, "content": []},
                        {**response, "content": [{"type": "compaction", "content": "summary"}]},
                        {**response, "content": [{"type": "compaction", "content": "", "signature": "x"}]},
                        {**response, "content": response["content"] + answer("shoot")["content"]}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                model.parse_compaction(invalid)

    @patch.dict("os.environ", {}, clear=True)
    def test_both_endpoints_require_a_key(self):
        config = model.load_config(self.root / ".env")
        for call in (model.call_model, model.count_tokens):
            with patch("model.request.build_opener") as opener, self.assertRaises(ValueError):
                call({}, config)
            opener.assert_not_called()

    def test_rate_limit_diagnostics_do_not_expose_provider_text(self):
        body = {"error": {"type": "rate_limit_error", "message": "secret-key and private image data"}}
        exc = HTTPError(model.API_ENDPOINT, 429, "Too Many Requests",
                        {"retry-after": "12", "request-id": "req_12345678"}, io.BytesIO(json.dumps(body).encode()))
        with patch("model.request.build_opener") as opener:
            opener.return_value.open.side_effect = exc
            with self.assertRaises(model.ApiError) as caught:
                model.call_model({}, self.config)
        self.assertEqual(caught.exception.details, {"http_status": 429, "error_type": "rate_limit_error",
                                                   "retry_after_s": 12.0, "request_id": "req_12345678"})
        self.assertNotIn("secret-key", str(caught.exception))
        self.assertNotIn("private image", str(caught.exception))
        self.assertEqual(opener.return_value.open.call_count, 1)

    def test_billing_diagnostics_classify_without_logging_error_message(self):
        body = {"error": {"type": "invalid_request_error", "message": "Your credit balance is too low. secret-key"}}
        exc = HTTPError(model.API_ENDPOINT, 400, "Bad Request", {}, io.BytesIO(json.dumps(body).encode()))
        failure = model.api_error(exc)
        self.assertEqual(failure.details["reason"], "insufficient_credits")
        self.assertNotIn("secret-key", str(failure))


if __name__ == "__main__":
    unittest.main()
