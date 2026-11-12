# Copyright (c) 2026 Eleven Content Ltd. Licensed under the Common Public Attribution License 1.0
# (CPAL-1.0), which requires keeping the "A Free Eleven Content Project" attribution. See LICENSE.

"""One small interface over Claude, OpenAI and Gemini.

Everything the pipeline asks a model for is either structured JSON (tagging, image
descriptions) or a transcript, so that's all this module exposes.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

from .config import ModelConfig


@dataclass
class Image:
    data: bytes
    mime_type: str


def _closed(schema: dict) -> dict:
    """Structured outputs need `additionalProperties: false` on every object."""
    schema = dict(schema)
    if schema.get("type") == "object":
        schema.setdefault("additionalProperties", False)
        schema["properties"] = {k: _closed(v) for k, v in schema.get("properties", {}).items()}
    if isinstance(schema.get("items"), dict):
        schema["items"] = _closed(schema["items"])
    return schema


def anthropic_client(api_key: str):
    """An organisation-wide key (not tied to a workspace) needs the workspace named on every
    request; ANTHROPIC_WORKSPACE_ID does that. Keys made inside a workspace don't need it."""
    import os

    import anthropic

    workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    return anthropic.Anthropic(api_key=api_key,
                               default_headers={"anthropic-workspace-id": workspace} if workspace else None)


class LLM:
    def __init__(self, config: ModelConfig):
        self.config = config

    def generate_json(self, system: str, prompt: str, schema: dict, images: list[Image] | None = None,
                      max_tokens: int = 4096) -> dict:
        """Returns an object matching `schema` (a JSON Schema for an object)."""
        provider = self.config.provider
        if provider == "anthropic":
            return self._anthropic(system, prompt, schema, images or [], max_tokens)
        if provider == "openai":
            return self._openai(system, prompt, schema, images or [], max_tokens)
        if provider == "gemini":
            return self._gemini(system, prompt, schema, images or [], max_tokens)
        raise ValueError(f"Unknown provider {provider}")

    def _anthropic(self, system, prompt, schema, images, max_tokens) -> dict:

        client = anthropic_client(self.config.api_key)
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": i.mime_type,
                                         "data": base64.b64encode(i.data).decode()}}
            for i in images
        ]
        content.append({"type": "text", "text": prompt})
        response = client.messages.create(
            model=self.config.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"format": {"type": "json_schema", "schema": _closed(schema)}},
        )
        if response.stop_reason in {"refusal", "max_tokens"}:
            raise RuntimeError(f"The model stopped early ({response.stop_reason}).")
        return json.loads(next(b.text for b in response.content if b.type == "text"))

    def _openai(self, system, prompt, schema, images, max_tokens) -> dict:
        from openai import OpenAI

        client = OpenAI(api_key=self.config.api_key)
        content = [
            {"type": "image_url", "image_url": {
                "url": f"data:{i.mime_type};base64,{base64.b64encode(i.data).decode()}"}}
            for i in images
        ]
        content.append({"type": "text", "text": f"{prompt}\n\nRespond with JSON matching this schema:\n{json.dumps(schema)}"})
        response = client.chat.completions.create(
            model=self.config.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": content}],
            response_format={"type": "json_object"},
            max_completion_tokens=max_tokens,
        )
        return json.loads(response.choices[0].message.content)

    def _gemini(self, system, prompt, schema, images, max_tokens) -> dict:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self.config.api_key)
        parts = [types.Part.from_bytes(data=i.data, mime_type=i.mime_type) for i in images]
        parts.append(f"{prompt}\n\nRespond with JSON matching this schema:\n{json.dumps(schema)}")
        response = client.models.generate_content(
            model=self.config.model,
            contents=parts,
            config=types.GenerateContentConfig(system_instruction=system, response_mime_type="application/json",
                                               max_output_tokens=max_tokens),
        )
        return json.loads(response.text)

    def transcribe(self, audio: bytes, mime_type: str, filename: str = "audio") -> str:
        provider = self.config.provider
        if provider == "openai":
            from openai import OpenAI

            client = OpenAI(api_key=self.config.api_key)
            result = client.audio.transcriptions.create(model=self.config.model, file=(filename, audio, mime_type))
            return result.text
        if provider == "gemini":
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=self.config.api_key)
            response = client.models.generate_content(
                model=self.config.model,
                contents=[
                    types.Part.from_bytes(data=audio, mime_type=mime_type),
                    "Transcribe this recording word for word. Label speakers if there is more than one.",
                ],
            )
            return response.text
        raise ValueError("Audio needs an OpenAI or Gemini model, picked under Models in Settings.")
