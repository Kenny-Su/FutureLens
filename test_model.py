"""Offline checks for live-image decisions and API configuration."""

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


def answer(choice):
    return {"model": "claude-haiku-5-5", "stop_reason": "end_turn", "content": [
        {"type": "text", "text": json.dumps({"shot": choice})}
    ]}


class ModelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = model.ApiConfig(api_key="offline-test-key")

    def test_core_sends_one_image_and_returns_boolean(self):
        image = Image.new("RGB", (1920, 1080), "green")
        with patch("model.call_model", return_value=answer(True)) as api:
            self.assertIs(model.decide(image, "Photograph the green scene", self.config), True)
        payload, _ = api.call_args.args
        self.assertEqual(payload["model"], "claude-haiku-5-5")
        self.assertEqual(len(payload["messages"]), 1)
        content = payload["messages"][0]["content"]
        self.assertEqual(content[1]["text"], "User request: Photograph the green scene")
        images = [item for item in content if item["type"] == "image"]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["source"]["media_type"], "image/jpeg")
        encoded = base64.b64decode(images[0]["source"]["data"])
        with Image.open(io.BytesIO(encoded)) as sent:
            self.assertEqual(sent.size, (960, 540))
        self.assertEqual(image.size, (1920, 1080))
        schema = payload["output_config"]["format"]["schema"]
        self.assertEqual(schema["properties"], {"shot": {"type": "boolean"}})
        self.assertEqual(schema["required"], ["shot"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["output_config"]["effort"], "low")

    def test_refusal_missing_and_non_boolean_answers_are_rejected(self):
        for value in (True, False):
            self.assertIs(model.parse_shot(answer(value)), value)
        invalid = [{}, None, answer("false"), answer(0), answer(None),
                   {**answer(True), "stop_reason": "refusal"},
                   {**answer(True), "stop_reason": "max_tokens"},
                   {**answer(True), "content": [None]},
                   {**answer(True), "content": []},
                   {**answer(True), "content": [{"type": "text", "text": '{"shot": true, "extra": 1}'}]},
                   {**answer(True), "content": [{"type": "text", "text": 'not JSON'}]}]
        for response in invalid:
            with self.subTest(response=response), self.assertRaises(ValueError):
                model.parse_shot(response)

    @patch.dict("os.environ", {}, clear=True)
    def test_anthropic_config_and_literal_env_values(self):
        env = self.root / ".env"
        env.write_text('ANTHROPIC_API_KEY="test-$(do-not-execute)"\n')
        config = model.load_config(env)
        self.assertEqual(config.api_key, "test-$(do-not-execute)")
        self.assertNotIn(config.api_key, repr(config))

    @patch.dict("os.environ", {"ANTHROPIC_BASE_URL": "https://example.com/gateway/v1",
                               "ANTHROPIC_MODEL": "other-model"}, clear=True)
    def test_api_uses_only_anthropic_endpoint_and_haiku(self):
        env = self.root / ".env"
        env.write_text('ANTHROPIC_API_KEY=test-key\nANTHROPIC_WORKSPACE_ID=wrkspc_test\n'
                       'ANTHROPIC_BASE_URL=https://example.com/another-gateway/v1\n'
                       'ANTHROPIC_MODEL=another-model\n')
        config = model.load_config(env)
        with patch("model.request.build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(json.dumps(answer(False)).encode())
            self.assertIs(model.decide(Image.new("RGB", (32, 32)), "person visible", config), False)
        sent = opener.return_value.open.call_args.args[0]
        self.assertEqual(sent.full_url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(json.loads(sent.data)["model"], "claude-haiku-5-5")
        self.assertEqual(sent.get_header("X-api-key"), "test-key")
        self.assertEqual(sent.get_header("Anthropic-version"), model.API_VERSION)
        self.assertEqual(sent.get_header("Anthropic-workspace-id"), "wrkspc_test")
        self.assertTrue(sent.get_header("User-agent").startswith("FutureLens/"))

    @patch.dict("os.environ", {}, clear=True)
    def test_anthropic_key_is_required(self):
        env = self.root / ".env"
        config = model.load_config(env)
        with patch("model.call_model") as call, self.assertRaises(ValueError):
            model.decide(Image.new("RGB", (32, 32)), "person visible", config)
        call.assert_not_called()

    def test_rate_limit_diagnostics_do_not_expose_provider_text(self):
        body = {"error": {"type": "rate_limit_error", "message": "secret-key and private image data"}}
        exc = HTTPError("https://api.anthropic.com/v1/messages", 429, "Too Many Requests",
                        {"retry-after": "12", "request-id": "req_12345678"}, io.BytesIO(json.dumps(body).encode()))
        with patch("model.request.build_opener") as opener:
            opener.return_value.open.side_effect = exc
            with self.assertRaises(model.ApiError) as caught:
                model.call_model({}, self.config)
        self.assertEqual(caught.exception.details, {"http_status": 429, "error_type": "rate_limit_error",
                                                   "retry_after_s": 12.0, "request_id": "req_12345678"})
        self.assertNotIn("secret-key", str(caught.exception))
        self.assertNotIn("private image", str(caught.exception))

    def test_billing_diagnostics_classify_without_logging_error_message(self):
        body = {"error": {"type": "invalid_request_error", "message": "Your credit balance is too low. secret-key"}}
        exc = HTTPError("https://api.anthropic.com/v1/messages", 400, "Bad Request", {}, io.BytesIO(json.dumps(body).encode()))
        failure = model.api_error(exc)
        self.assertEqual(failure.details["reason"], "insufficient_credits")
        self.assertNotIn("secret-key", str(failure))


if __name__ == "__main__":
    unittest.main()
