"""One append-only conversation per session, with signed native compaction."""

from copy import deepcopy
from datetime import datetime, timezone

from history import append_json, local_message
from model import (INSTRUCTIONS, COMPACTION_INSTRUCTIONS, COMPACTION_BETA, MODEL, MAX_OUTPUT_TOKENS, check_capacity, make_payload,
                   make_compaction_payload, parse_compaction, parse_turn, response_usage)

COMPACT_INPUT_TOKENS = 100_000
COMPACT_REQUEST_BYTES = 20_000_000
COMPACT_IMAGES = 400
KEEP_OBSERVATIONS = 4
COMPACTION_RETRY_SECONDS = 60


class SessionAgent:
    def __init__(self, output, preview_mode):
        self.output, self.preview_mode = output, preview_mode
        self.messages, self.seen_ids, self.boundaries = [], set(), []
        self.revision = 0
        self.estimated_tokens = 0
        self.retry_at = 0
        self.last_attempt_observation = -1
        self.log = output / "conversation.jsonl"
        payload = self.payload()
        self.record("session", model=MODEL, system=INSTRUCTIONS, tools=payload["tools"],
                    thinking=payload["thinking"], output_config=payload["output_config"],
                    compaction_instructions=COMPACTION_INSTRUCTIONS, beta=COMPACTION_BETA)

    def record(self, kind, **details):
        append_json(self.log, {"type": kind, "context_revision": self.revision,
                              "timestamp": datetime.now(timezone.utc).isoformat(), **details})

    def append(self, message, observation_id, images=()):
        message = deepcopy(message)
        self.record("message", observation_id=observation_id, index=len(self.messages),
                    message=local_message(message, self.output, images))
        self.messages.append(message)

    def payload(self):
        return make_payload(self.messages, preview_mode=self.preview_mode)

    def accept_response(self, response, observation_id, input_tokens):
        call = parse_turn(response, self.seen_ids, preview_mode=self.preview_mode)
        # This is exactly a previously sent request's prefix. Its last user message
        # has resolved all preceding tool calls; the kept tail starts with this reply.
        self.record("request", observation_id=observation_id, message_count=len(self.messages),
                    input_tokens=input_tokens)
        if response["content"]:
            self.boundaries.append((observation_id, len(self.messages)))
            self.append({"role": "assistant", "content": response["content"]}, observation_id)
        else:
            # Empty end_turn is a valid no-action result, but an empty assistant
            # message must not be replayed in the middle of the next API request.
            self.record("empty_response", observation_id=observation_id)
        usage = response_usage(response)
        self.estimated_tokens = input_tokens + usage.get("output_tokens", MAX_OUTPUT_TOKENS)
        return call

    def compaction_candidate(self, observation_id, now):
        metrics = check_capacity(self.payload())
        if (now < self.retry_at or observation_id <= self.last_attempt_observation
                or not (self.estimated_tokens >= COMPACT_INPUT_TOKENS
                        or metrics["images"] >= COMPACT_IMAGES
                        or metrics["request_bytes"] >= COMPACT_REQUEST_BYTES)):
            return None
        candidates = [cut for frame_id, cut in self.boundaries
                      if frame_id <= observation_id - KEEP_OBSERVATIONS]
        if not candidates:
            return None
        cut = max(candidates)
        return cut, make_compaction_payload(self.messages[:cut], preview_mode=self.preview_mode)

    def apply_compaction(self, response, cut):
        summary = deepcopy(parse_compaction(response))
        # Persist the replacement before changing the active context. The old log
        # remains intact; replay message appends and these replacements to reconstruct it.
        self.record("compaction", replaced_messages=cut, next_context_revision=self.revision + 1,
                    message=summary)
        self.messages = [summary, *self.messages[cut:]]
        self.boundaries = [(frame, index - cut + 1) for frame, index in self.boundaries if index > cut]
        self.revision += 1
        self.estimated_tokens = 0  # Recount the resulting context before the next inference.
        self.retry_at = 0

    def compaction_failed(self, now):
        self.retry_at = now + COMPACTION_RETRY_SECONDS
