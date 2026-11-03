"""Astra negotiates a self-contained visual condition before monitoring begins."""

import json

from intent import TARGET_SCHEMA, object_schema, validate_intent, validate_review
from openai_client import image_content, model_image, post_json

PLANNING_MODEL = "gpt-6-astra"
RESPONSES_ENDPOINT = "https://api.openai.com/v1/responses"
EFFORT = "medium"
MAX_OUTPUT_TOKENS = 8192

PLANNER_DEVELOPER_INSTRUCTIONS = """Work with the photographer to establish a shared understanding of the desired photograph, including any visual details that materially affect which moments qualify.
Develop a concrete shooting plan for their approval without adding unrequested aesthetic preferences or requirements.
The monitor evaluates one preview at a time, so the plan must be self-contained and assessable from a single image.
Discuss details and clarify preferences in the conversation. Use tools to highlight regions and present a plan for approval as needed.
Only the photographer can authorize monitoring."""

PROPOSE_PLAN = {
    "type": "function", "name": "propose_plan",
    "description": "Present a shooting plan for the photographer to review. Waits for approval or requested edits; approval starts monitoring.",
    "strict": True,
    "parameters": object_schema({
        "condition": {"type": "string", "description": "Complete shooting plan for approval."},
        "reply": {"type": "string", "description": "Conversational introduction to the proposed plan."},
    }),
}
HIGHLIGHT_REGIONS = {
    "type": "function", "name": "highlight_regions",
    "description": "Highlight visible regions on the photographer's preview. Replaces the current highlights; an empty list clears them. Returns immediately.",
    "strict": True,
    "parameters": object_schema({"targets": {"type": "array", "items": TARGET_SCHEMA}}),
}


def make_planning_payload(request_text, image, conversation, context):
    messages = []
    pending_call = None
    for turn in conversation or [{"role": "user", "text": request_text}]:
        if turn["role"] == "assistant" and turn.get("tool_turn"):
            messages.extend(turn["tool_turn"])
            pending_call = next((item["call_id"] for item in turn["tool_turn"]
                                 if item["type"] == "function_call" and item["name"] == "propose_plan"), None)
        elif turn["role"] == "assistant":
            content = {"plan_ready": turn.get("plan_ready", False), "reply": turn["text"], "condition": turn["condition"],
                       "highlights_normalized": turn.get("targets", []), "reference_id": turn.get("reference_id")}
            messages.append({"role": "assistant", "content": json.dumps(content, ensure_ascii=False)})
        elif pending_call:
            messages.append({"type": "function_call_output", "call_id": pending_call, "output": turn["text"]})
            pending_call = None
        else:
            messages.append({"role": "user", "content": [{"type": "input_text", "text": turn["text"]}]})
    evidence = [{"type": "input_text", "text": json.dumps(
        {"image_width": image.width, "image_height": image.height, **context}, ensure_ascii=False)}, image_content(image)]
    if messages[-1].get("role") == "user":
        messages[-1]["content"].extend(evidence)
    else:
        messages.append({"role": "user", "content": evidence})
    return {"model": PLANNING_MODEL, "max_output_tokens": MAX_OUTPUT_TOKENS,
            "instructions": PLANNER_DEVELOPER_INSTRUCTIONS, "reasoning": {"effort": EFFORT},
            "input": messages, "store": False, "include": ["reasoning.encrypted_content"],
            "tools": [HIGHLIGHT_REGIONS, PROPOSE_PLAN],
            "tool_choice": "auto", "parallel_tool_calls": False}


def review_intent(request_text, image, config, *, conversation=None, previous=None, reference_changed=False,
                  targets=None, on_highlight):
    image = model_image(image)
    payload = make_planning_payload(request_text, image, conversation,
                                   {"previous_interpretation": previous, "previous_box_units": "normalized_0_1",
                                    "reference": "NEW reference image" if reference_changed else "CURRENT saved reference image"})
    targets = targets or []
    tool_turn = []
    while True:
        response = post_json(RESPONSES_ENDPOINT, payload, config)
        if response.get("status") != "completed":
            raise ValueError("The interpretation was incomplete. Try again.")
        calls = [item for item in response["output"] if item["type"] == "function_call"]
        tool_turn.extend(response["output"])
        if not calls:
            blocks = [block for item in response["output"] if item["type"] == "message" for block in item["content"]]
            if not blocks or any(block["type"] != "output_text" for block in blocks):
                raise ValueError("The planner did not return a reply. Try again.")
            value = {"reply": "\n".join(block["text"] for block in blocks), "condition": "",
                     "plan_ready": False, "targets": targets}
            return validate_review(value), tool_turn
        if len(calls) != 1:
            raise ValueError("Unexpected planner tool calls.")
        call = calls[0]
        value = json.loads(call["arguments"])
        if call["name"] == "propose_plan":
            value.update(plan_ready=True, targets=targets)
            return validate_review(value), tool_turn
        if call["name"] != "highlight_regions":
            raise ValueError("Unexpected planner tool call.")
        targets = validate_intent({"condition": "", **value}, image.width, image.height, allow_empty=True)["targets"]
        on_highlight(targets)
        result = {"type": "function_call_output", "call_id": call["call_id"], "output": "Highlights updated."}
        tool_turn.append(result)
        payload["input"].extend([*response["output"], result])
