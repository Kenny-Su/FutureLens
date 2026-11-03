"""Luna evaluates one approved condition against one preview, without session memory."""

import json

from openai_client import image_content, post_json

MONITOR_MODEL = "gpt-6-luna"
DECISIONS_ENDPOINT = "https://api.openai.com/v1/decisions"
MONITOR_QUESTION_INSTRUCTIONS = """Does the current preview (the last image in input) satisfy the approved shooting plan?
Use the instructions and any reference photo and regions to evaluate it. The reference is context, not the preview being evaluated."""


def make_monitoring_payload(plan, image, reference_image=None):
    instructions = MONITOR_QUESTION_INSTRUCTIONS + "\n\nApproved shooting instructions:\n" + plan["instructions"]
    content = []
    if plan["reference"] is not None:
        if reference_image is None:
            raise ValueError("The approved plan requires its reference photo.")
        instructions += ("\n\nUse the reference photo as follows:\n" + plan["reference"]["purpose"]
                         + "\nRegions on the reference, with normalized [x1,y1,x2,y2] coordinates:\n"
                         + json.dumps(plan["reference"]["regions"]))
        content.extend([{"type": "input_text", "text": "Planning reference photo:"}, image_content(reference_image)])
    content.extend([{"type": "input_text", "text": "Current preview to evaluate:"}, image_content(image)])
    return {"model": MONITOR_MODEL, "input": [{"role": "user", "content": content}], "questions": [{
        "type": "predicate", "name": "condition", "instructions": instructions,
    }]}


def decision_probability(response):
    answers = response.get("answers") if isinstance(response, dict) else None
    if (not isinstance(answers, list) or len(answers) != 1 or not isinstance(answers[0], dict)
            or answers[0].get("type") != "predicate" or answers[0].get("name") != "condition"
            or type(answers[0].get("probability")) not in (int, float)
            or not 0 <= answers[0]["probability"] <= 1):
        raise ValueError("Missing, refused, or invalid condition decision")
    return answers[0]["probability"]


def evaluate_preview(payload, config):
    return post_json(DECISIONS_ENDPOINT, payload, config)
