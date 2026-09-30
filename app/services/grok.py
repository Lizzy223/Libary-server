"""Thin client for the xAI (Grok) chat completions API, which is OpenAI-compatible."""
import json
import re

import httpx

from ..config import get_config


class GrokUnavailable(Exception):
    """Raised when the LLM is not configured or cannot be reached. Callers degrade gracefully."""


def is_configured() -> bool:
    return bool(get_config().xai_api_key)


async def chat(messages: list[dict], *, json_mode: bool = False, temperature: float = 0.2) -> str:
    cfg = get_config()
    if not cfg.xai_api_key:
        raise GrokUnavailable("AI features are not configured. Set XAI_API_KEY on the server.")

    payload: dict = {"model": cfg.grok_model, "messages": messages, "temperature": temperature}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {cfg.xai_api_key}", "Content-Type": "application/json"}
    url = cfg.xai_base_url.rstrip("/") + "/chat/completions"

    try:
        async with httpx.AsyncClient(timeout=cfg.grok_timeout_seconds) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code == 400 and json_mode:
                # Some models reject response_format; retry once and rely on the prompt + tolerant parser
                payload.pop("response_format", None)
                resp = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise GrokUnavailable("The AI service could not be reached. Please try again.") from exc

    if resp.status_code in (401, 403):
        raise GrokUnavailable("The AI service rejected the server's API key.")
    if resp.status_code == 429:
        raise GrokUnavailable("The AI service is busy. Please try again shortly.")
    if resp.status_code >= 400:
        raise GrokUnavailable(f"The AI service returned an error ({resp.status_code}).")
    try:
        return resp.json()["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, ValueError) as exc:
        raise GrokUnavailable("The AI service returned an unexpected response.") from exc


def parse_json(text: str) -> dict:
    """Tolerant JSON extraction: handles code fences and stray prose around the object."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        data = json.loads(text)
    except ValueError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise GrokUnavailable("The AI service returned an unreadable answer.") from None
        try:
            data = json.loads(match.group(0))
        except ValueError:
            raise GrokUnavailable("The AI service returned an unreadable answer.") from None
    if not isinstance(data, dict):
        raise GrokUnavailable("The AI service returned an unreadable answer.")
    return data
