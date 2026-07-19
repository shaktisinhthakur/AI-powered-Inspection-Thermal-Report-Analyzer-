import base64
import json
import logging
import time
from abc import ABC, abstractmethod
from typing import Optional

logger = logging.getLogger(__name__)


def _detect_image_mime(data: bytes) -> str:
    """Detect image MIME type from bytes header."""
    if data[:4] == b'\x89PNG':
        return "image/png"
    if data[:2] in (b'\xFF\xD8',):
        return "image/jpeg"
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return "image/webp"
    if data[:4] == b'GIF8':
        return "image/gif"
    if data[:2] == b'BM':
        return "image/bmp"
    return "image/png"  # safest default


# ───────────────────────────── Retry helper ─────────────────────────────

def retry_on_transient(max_retries: int = 2, base_delay: float = 1.0):
    """Decorator for LLM methods: retries on rate-limit / timeout / 5xx."""
    def decorator(func):
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exc = e
                    err_str = str(e).lower()
                    is_transient = (
                        "rate limit" in err_str or
                        "rate_limit" in err_str or
                        "timeout" in err_str or
                        "429" in err_str or
                        "503" in err_str or
                        "502" in err_str or
                        "service unavailable" in err_str or
                        "too many requests" in err_str
                    )
                    if not is_transient or attempt == max_retries:
                        raise
                    delay = base_delay * (2 ** attempt)
                    logger.warning(
                        "Transient error on attempt %d/%d: %s. Retrying in %.1fs...",
                        attempt + 1, max_retries + 1, e, delay
                    )
                    time.sleep(delay)
            raise last_exc
        return wrapper
    return decorator


# ───────────────────────────── Abstract base ─────────────────────────────

class LLMProvider(ABC):
    """Abstract interface for all LLM providers."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._supports_vision = True

    @abstractmethod
    def extract_structured(self, text: str, schema: dict) -> dict:
        """Stage 2: extract structured observations from document text."""

    @abstractmethod
    def caption_image(self, image_bytes: bytes, prompt: str) -> dict:
        """Stage 3: caption an image and return {caption, area_tag, keywords}."""

    @abstractmethod
    def merge_and_reason(self, payload: dict, schema: dict) -> dict:
        """Stage 4: merge observations, reason, and produce DDR JSON."""

    @abstractmethod
    def validate_key(self) -> bool:
        """Cheap call to verify the API key is valid."""

    @property
    def supports_vision(self) -> bool:
        return self._supports_vision


# ═════════════════════════════ Anthropic ═════════════════════════════

class AnthropicProvider(LLMProvider):
    MODEL = "claude-sonnet-4-20250514"

    def __init__(self, api_key: str):
        super().__init__(api_key)
        try:
            import anthropic
            self.client = anthropic.Anthropic(api_key=api_key)
        except ImportError:
            raise ImportError("anthropic package not installed")

    def _call_tool(self, system: str, user_msg: str, schema: dict) -> dict:
        resp = self.client.messages.create(
            model=self.MODEL,
            max_tokens=4096,
            system=[{"type": "text", "text": system}],
            messages=[{"role": "user", "content": user_msg}],
            tools=[{
                "name": "extract_data",
                "description": "Extract structured data from the document",
                "input_schema": schema
            }],
            tool_choice={"type": "tool", "name": "extract_data"}
        )
        for block in resp.content:
            if block.type == "tool_use" and block.name == "extract_data":
                return block.input
        raise ValueError("No tool_use block returned by Anthropic")

    @retry_on_transient()
    def extract_structured(self, text: str, schema: dict) -> dict:
        return self._call_tool(
            system="You extract structured observations from building reports.",
            user_msg=f"Extract all observations from the following report text:\n\n{text}",
            schema=schema
        )

    @retry_on_transient()
    def caption_image(self, image_bytes: bytes, prompt: str) -> dict:
        b64 = base64.standard_b64encode(image_bytes).decode("ascii")
        media_type = _detect_image_mime(image_bytes)
        resp = self.client.messages.create(
            model=self.MODEL,
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": b64
                    }},
                    {"type": "text", "text": prompt}
                ]
            }],
            tools=[{
                "name": "describe_image",
                "description": "Describe the inspection image",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "caption": {"type": "string"},
                        "area_tag": {"type": "string"},
                        "is_thermal_image": {"type": "boolean"},
                        "visible_temp_reading": {"type": "string"}
                    },
                    "required": ["caption", "area_tag", "is_thermal_image"]
                }
            }],
            tool_choice={"type": "tool", "name": "describe_image"}
        )
        for block in resp.content:
            if block.type == "tool_use" and block.name == "describe_image":
                return block.input
        raise ValueError("No tool_use block for describe_image")

    @retry_on_transient()
    def merge_and_reason(self, payload: dict, schema: dict) -> dict:
        return self._call_tool(
            system="You produce Detailed Diagnostic Reports from merged observations.",
            user_msg=json.dumps(payload, indent=2),
            schema=schema
        )

    def validate_key(self) -> bool:
        try:
            self.client.messages.create(
                model="claude-3-haiku-20240307",
                max_tokens=1,
                messages=[{"role": "user", "content": "."}]
            )
            return True
        except Exception as e:
            err_str = str(e).lower()
            if "authentication" in err_str or "unauthorized" in err_str or "invalid" in err_str or "401" in err_str:
                return False
            logger.warning("Anthropic validate_key non-auth error: %s", err_str[:120])
            return True  # non-auth errors (rate-limit, model, etc.) mean the key is valid


# ═════════════════════════════ OpenAI ════════════════════════════════

class OpenAIProvider(LLMProvider):
    MODEL = "gpt-4o"

    def __init__(self, api_key: str):
        super().__init__(api_key)
        try:
            from openai import OpenAI
            self.client = OpenAI(api_key=api_key)
        except ImportError:
            raise ImportError("openai package not installed")

    def _call_structured(self, system: str, user_msg: str, schema: dict) -> dict:
        resp = self.client.chat.completions.create(
            model=self.MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg}
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_output",
                    "schema": schema,
                    "strict": True
                }
            }
        )
        raw = resp.choices[0].message.content
        return json.loads(raw)

    @retry_on_transient()
    def extract_structured(self, text: str, schema: dict) -> dict:
        return self._call_structured(
            system="You extract structured observations from building reports.",
            user_msg=f"Extract all observations from the following report text:\n\n{text}",
            schema=schema
        )

    @retry_on_transient()
    def caption_image(self, image_bytes: bytes, prompt: str) -> dict:
        b64 = base64.standard_b64encode(image_bytes).decode("ascii")
        mime = _detect_image_mime(image_bytes)
        data_url = f"data:{mime};base64,{b64}"
        schema = {
            "type": "object",
            "properties": {
                "caption": {"type": "string"},
                "area_tag": {"type": "string"},
                "is_thermal_image": {"type": "boolean"},
                "visible_temp_reading": {"type": "string"}
            },
            "required": ["caption", "area_tag", "is_thermal_image"]
        }
        resp = self.client.chat.completions.create(
            model=self.MODEL,
            messages=[
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": "Describe this inspection image."}
                    ]
                }
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "image_caption",
                    "schema": schema,
                    "strict": True
                }
            }
        )
        raw = resp.choices[0].message.content
        return json.loads(raw)

    @retry_on_transient()
    def merge_and_reason(self, payload: dict, schema: dict) -> dict:
        return self._call_structured(
            system="You produce Detailed Diagnostic Reports from merged observations.",
            user_msg=json.dumps(payload, indent=2),
            schema=schema
        )

    def validate_key(self) -> bool:
        try:
            self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "."}],
                max_tokens=1
            )
            return True
        except Exception as e:
            err_str = str(e).lower()
            if "authentication" in err_str or "unauthorized" in err_str or "invalid" in err_str or "401" in err_str:
                return False
            logger.warning("OpenAI validate_key non-auth error: %s", err_str[:120])
            return True


# ═════════════════════════════ Groq (OpenAI-compatible) ══════════════

class GroqProvider(LLMProvider):
    MODEL = "llama-3.2-90b-vision-preview"
    VISION_MODEL = "llama-3.2-90b-vision-preview"

    def __init__(self, api_key: str):
        super().__init__(api_key)
        try:
            from openai import OpenAI
            self.client = OpenAI(
                api_key=api_key,
                base_url="https://api.groq.com/openai/v1"
            )
        except ImportError:
            raise ImportError("openai package not installed (Groq uses OpenAI-compatible SDK)")

    def _call_function(self, system: str, user_msg: str, schema: dict) -> dict:
        resp = self.client.chat.completions.create(
            model=self.MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg}
            ],
            tools=[{
                "type": "function",
                "function": {
                    "name": "extract_data",
                    "description": "Extract structured data",
                    "parameters": schema
                }
            }],
            tool_choice={"type": "function", "function": {"name": "extract_data"}}
        )
        msg = resp.choices[0].message
        if msg.tool_calls:
            for tc in msg.tool_calls:
                if tc.function.name == "extract_data":
                    return json.loads(tc.function.arguments)
        raise ValueError("No function call returned by Groq")

    @retry_on_transient()
    def extract_structured(self, text: str, schema: dict) -> dict:
        return self._call_function(
            system="You extract structured observations from building reports.",
            user_msg=f"Extract all observations from the following report text:\n\n{text}",
            schema=schema
        )

    @retry_on_transient()
    def caption_image(self, image_bytes: bytes, prompt: str) -> dict:
        b64 = base64.standard_b64encode(image_bytes).decode("ascii")
        mime = _detect_image_mime(image_bytes)
        data_url = f"data:{mime};base64,{b64}"
        caption_schema = {
            "type": "object",
            "properties": {
                "caption": {"type": "string"},
                "area_tag": {"type": "string"},
                "is_thermal_image": {"type": "boolean"},
                "visible_temp_reading": {"type": "string"}
            },
            "required": ["caption", "area_tag", "is_thermal_image"]
        }
        resp = self.client.chat.completions.create(
            model=self.VISION_MODEL,
            messages=[
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": "Describe this inspection image."}
                    ]
                }
            ],
            tools=[{
                "type": "function",
                "function": {
                    "name": "describe_image",
                    "description": "Describe an inspection image",
                    "parameters": caption_schema
                }
            }],
            tool_choice={"type": "function", "function": {"name": "describe_image"}}
        )
        msg = resp.choices[0].message
        if msg.tool_calls:
            for tc in msg.tool_calls:
                if tc.function.name == "describe_image":
                    return json.loads(tc.function.arguments)
        raise ValueError("No function call returned by Groq for image caption")

    @retry_on_transient()
    def merge_and_reason(self, payload: dict, schema: dict) -> dict:
        return self._call_function(
            system="You produce Detailed Diagnostic Reports from merged observations.",
            user_msg=json.dumps(payload, indent=2),
            schema=schema
        )

    def validate_key(self) -> bool:
        try:
            self.client.chat.completions.create(
                model="llama-3.2-3b-preview",
                messages=[{"role": "user", "content": "."}],
                max_tokens=1
            )
            return True
        except Exception as e:
            err_str = str(e).lower()
            if "authentication" in err_str or "unauthorized" in err_str or "invalid" in err_str or "401" in err_str:
                return False
            logger.warning("Groq validate_key non-auth error: %s", err_str[:120])
            return True


# ═════════════════════════════ Gemini ════════════════════════════════

class GeminiProvider(LLMProvider):
    MODEL = "gemini-2.0-flash"

    def __init__(self, api_key: str):
        super().__init__(api_key)
        try:
            import google.generativeai as genai
            genai.configure(api_key=api_key)
            self.genai = genai
        except ImportError:
            raise ImportError("google-generativeai package not installed")

    def _call_schema(self, system: str, user_msg: str, schema: dict) -> dict:
        model = self.genai.GenerativeModel(
            self.MODEL,
            system_instruction=system
        )
        resp = model.generate_content(
            user_msg,
            generation_config=self.genai.GenerationConfig(
                response_mime_type="application/json",
                response_schema=schema
            )
        )
        raw = resp.text
        return json.loads(raw)

    @retry_on_transient()
    def extract_structured(self, text: str, schema: dict) -> dict:
        return self._call_schema(
            system="You extract structured observations from building reports.",
            user_msg=f"Extract all observations from the following report text:\n\n{text}",
            schema=schema
        )

    @retry_on_transient()
    def caption_image(self, image_bytes: bytes, prompt: str) -> dict:
        import PIL.Image
        import io
        img = PIL.Image.open(io.BytesIO(image_bytes))
        caption_schema = {
            "type": "object",
            "properties": {
                "caption": {"type": "string"},
                "area_tag": {"type": "string"},
                "is_thermal_image": {"type": "boolean"},
                "visible_temp_reading": {"type": "string"}
            },
            "required": ["caption", "area_tag", "is_thermal_image"]
        }
        model = self.genai.GenerativeModel(self.MODEL)
        resp = model.generate_content(
            [prompt, img],
            generation_config=self.genai.GenerationConfig(
                response_mime_type="application/json",
                response_schema=caption_schema
            )
        )
        raw = resp.text
        return json.loads(raw)

    @retry_on_transient()
    def merge_and_reason(self, payload: dict, schema: dict) -> dict:
        return self._call_schema(
            system="You produce Detailed Diagnostic Reports from merged observations.",
            user_msg=json.dumps(payload, indent=2),
            schema=schema
        )

    def validate_key(self) -> bool:
        try:
            model = self.genai.GenerativeModel(self.MODEL)
            model.generate_content(".", generation_config=self.genai.GenerationConfig(
                max_output_tokens=1
            ))
            return True
        except Exception as e:
            err_str = str(e).lower()
            if "authentication" in err_str or "unauthorized" in err_str or "invalid" in err_str or "401" in err_str or "api_key" in err_str:
                return False
            logger.warning("Gemini validate_key non-auth error: %s", err_str[:120])
            return True


# ───────────────────────────── Factory ─────────────────────────────

def get_provider(provider_name: str, api_key: str) -> LLMProvider:
    providers = {
        "anthropic": AnthropicProvider,
        "openai": OpenAIProvider,
        "groq": GroqProvider,
        "gemini": GeminiProvider,
    }
    cls = providers.get(provider_name.lower())
    if cls is None:
        raise ValueError(
            f"Unsupported provider: '{provider_name}'. "
            f"Supported: {list(providers.keys())}"
        )
    return cls(api_key=api_key)
