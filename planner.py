"""Astra negotiates a self-contained visual condition before monitoring begins."""

import json

from intent import REVIEW_SCHEMA, json_response, validate_review
from openai_client import image_content, model_image, post_json

PLANNING_MODEL = "gpt-6-astra"
RESPONSES_ENDPOINT = "https://api.openai.com/v1/responses"
EFFORT = "medium"
MAX_OUTPUT_TOKENS = 8192

PLANNER_DEVELOPER_INSTRUCTIONS = """Use the image and conversation to understand exactly what the photographer wants to capture.
Ask clarifying questions when needed and refine a concrete visual plan for their approval.
The approved plan will be passed to a monitor that evaluates one preview at a time, so make it
self-contained and assessable from a single image.

Respond conversationally in reply. While clarifying, set plan_ready to false, leave condition empty,
and put any clarification question in question. Generate a shooting plan only when enough information is available.
When you have enough information, naturally introduce the shooting plan in reply, provide the complete
plan in condition, set plan_ready to true, and leave question empty. The app will offer a review button in the conversation;
the photographer opens the plan when ready to confirm or request edits. After requested edits, revise the plan and present it again when ready.
Optionally highlight visible subjects in targets using absolute pixel coordinates within the supplied image dimensions."""


def make_planning_payload(request_text, image, conversation, context):
    messages = []
    for turn in conversation or [{"role": "user", "text": request_text}]:
        if turn["role"] == "assistant":
            content = {"plan_ready": turn.get("plan_ready", False), "reply": turn["text"], "condition": turn["condition"], "question": turn["question"],
                       "highlights_normalized": turn.get("targets", []), "reference_id": turn.get("reference_id")}
            messages.append({"role": "assistant", "content": json.dumps(content, ensure_ascii=False)})
        else:
            messages.append({"role": "user", "content": [{"type": "input_text", "text": turn["text"]}]})
    evidence = [{"type": "input_text", "text": json.dumps(
        {"image_width": image.width, "image_height": image.height, **context}, ensure_ascii=False)}, image_content(image)]
    if messages[-1]["role"] == "user":
        messages[-1]["content"].extend(evidence)
    else:
        messages.append({"role": "user", "content": evidence})
    return {"model": PLANNING_MODEL, "max_output_tokens": MAX_OUTPUT_TOKENS,
            "instructions": PLANNER_DEVELOPER_INSTRUCTIONS, "reasoning": {"effort": EFFORT},
            "input": messages, "store": False,
            "text": {"format": {"type": "json_schema", "name": "review",
                                "strict": True, "schema": REVIEW_SCHEMA}}}


def review_intent(request_text, image, config, *, conversation=None, previous=None, reference_changed=False):
    image = model_image(image)
    payload = make_planning_payload(request_text, image, conversation,
                                   {"previous_interpretation": previous, "previous_box_units": "normalized_0_1",
                                    "reference": "NEW reference image" if reference_changed else "CURRENT saved reference image"})
    response = post_json(RESPONSES_ENDPOINT, payload, config)
    return validate_review(json_response(response), width=image.width, height=image.height)
