"""
AI Vision Card Recognition Helper (`vault/services/vision_ai.py`).
Allows users or AI agents to upload an image of physical Magic: The Gathering cards
and automatically identify & resolve each card via Gemini Vision + Scryfall.
"""
import base64
import json
from typing import Any
import httpx
from django.conf import settings


def identify_cards_from_image_bytes(
    image_bytes: bytes, mime_type: str = "image/jpeg"
) -> list[dict[str, Any]]:
    """
    Use Gemini Vision REST API (if `GEMINI_API_KEY` is configured in `.env`) to extract
    all visible Magic: The Gathering cards from a photo of a binder page or desk spread.
    Returns a list of `{"name": str, "set_code": str | None, "collector_number": str | None, "quantity": int, "finish": str}`.
    """
    api_key = getattr(settings, "GEMINI_API_KEY", "")
    if not api_key:
        raise ValueError(
            "GEMINI_API_KEY is not configured in environment. You can also use an external AI with your Arcane Vault API key to call POST /api/v1/collection/bulk-add directly."
        )

    b64_img = base64.b64encode(image_bytes).decode("utf-8")
    prompt = (
        "Inspect this image of physical Magic: The Gathering cards. "
        "Return ONLY a JSON array of objects, one for each card visible, with keys: "
        '{"name": "Exact Card Name", "set_code": "3-letter set code or null", '
        '"collector_number": "collector number or null", "quantity": 1, "finish": "nonfoil or foil"}.'
    )
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": mime_type, "data": b64_img}},
                ]
            }
        ],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(url, json=payload)
        resp.raise_for_status()
        result = resp.json()

    text = (
        result.get("candidates", [{}])[0]
        .get("content", {})
        .get("parts", [{}])[0]
        .get("text", "[]")
    )
    parsed = json.loads(text)
    return parsed if isinstance(parsed, list) else []
