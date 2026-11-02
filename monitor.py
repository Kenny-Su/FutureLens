"""Luna evaluates one approved condition against one preview, without session memory."""

from openai_client import image_content, post_json

MONITOR_MODEL = "gpt-6-luna"
DECISIONS_ENDPOINT = "https://api.openai.com/v1/decisions"
MONITOR_QUESTION_INSTRUCTIONS = """Does this preview satisfy the approved shooting plan?
Evaluate only what is visible in this image."""


def make_monitoring_payload(condition, image):
    return {"model": MONITOR_MODEL, "input": [{"role": "user", "content": [
        {"type": "input_text", "text": "Approved visual condition:\n" + condition}, image_content(image),
    ]}], "questions": [{
        "type": "predicate", "name": "condition", "instructions": MONITOR_QUESTION_INSTRUCTIONS,
    }]}


def decision_probability(response):
    answers = response.get("answers") if isinstance(response, dict) else None
    if (not isinstance(answers, list) or len(answers) != 1 or not isinstance(answers[0], dict)
            or answers[0].get("type") != "predicate" or answers[0].get("name") != "condition"
            or type(answers[0].get("probability")) not in (int, float)
            or not 0 <= answers[0]["probability"] <= 1):
        raise ValueError("Missing, refused, or invalid condition decision")
    return answers[0]["probability"]


def evaluate_preview(payload, config, *, timeout=90):
    return post_json(DECISIONS_ENDPOINT, payload, config, timeout)
