"""LLM text optimizer — streaming OpenAI-compatible API"""
import asyncio
import json
import logging
from typing import Optional
import httpx

logger = logging.getLogger("yuhuang.llm")


class LLMOptimizer:
    """Call LLM to optimize speech recognition text"""

    def __init__(
        self,
        base_url: str = "http://localhost:8000/v1",
        api_key: str = "",
        model: str = "qwen2.5-7b-instruct",
        temperature: float = 0.3,
        max_tokens: int = 2000,
        system_prompt: str = "",
        optimize_delay: float = 0.5,
        auto_commit_delay: float = 0.2,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.optimize_delay = optimize_delay
        self.auto_commit_delay = auto_commit_delay
        self.system_prompt = system_prompt or self._default_prompt()

    @staticmethod
    def _default_prompt() -> str:
        return (
            "You are a speech transcription text optimization assistant. "
            "The user provides raw ASR output which may have:\n"
            '- filler words ("um", "uh", "like", "you know")\n'
            '- stuttering or repetitions\n'
            '- missing or incorrect punctuation\n'
            '- informal conjunctions\n\n'
            "Your task:\n"
            "1. Remove meaningless filler words\n"
            "2. Fix stuttering and repetitions\n"
            "3. Add proper punctuation\n"
            "4. Keep original meaning and tone\n"
            "5. Do NOT add content not in the original\n"
            "6. Do NOT change the original style\n\n"
            "Output only the optimized text, no explanations or prefixes."
        )

    def update_config(self, **kwargs):
        """Update runtime configuration (called when fcitx5 config changes)"""
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
                logger.info(f"LLM config updated: {key}={value}")

    async def optimize(self, text: str) -> Optional[str]:
        """一次性的文本优化（用于最终提交）"""
        if not text or not text.strip():
            return None
        return await self._call_llm(
            f"Please optimize this speech recognition text:\n\n{text}"
        )

    async def stream_optimize(self, new_raw: str, context: str = "") -> Optional[str]:
        """流式增量优化: 结合候选中已有的文本，优化新增的语音识别文本

        Args:
            new_raw: 新增的原始 ASR 文本
            context: 候选框中已有的未上屏文本
        Returns:
            优化后的完整候选文本（用于替换候选框）
        """
        combined = context + new_raw
        if not combined.strip():
            return None

        prompt = (
            "You are optimizing Chinese speech recognition text in real-time.\n"
            "Below is the complete text just transcribed.\n"
            "Optimize it: fix punctuation, remove fillers, correct stutters.\n"
            "Keep meaning unchanged. Output only the optimized text.\n\n"
            f"Text:\n{combined}"
        )
        return await self._call_llm(prompt)

    async def _call_llm(self, user_msg: str) -> Optional[str]:
        """底层 LLM API 调用 (流式)"""
        if not user_msg or not user_msg.strip():
            return None

        if self.optimize_delay > 0:
            await asyncio.sleep(self.optimize_delay)

        try:
            async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": self.system_prompt},
                            {"role": "user", "content": user_msg},
                        ],
                        "temperature": self.temperature,
                        "max_tokens": self.max_tokens,
                        "stream": True,
                    },
                ) as response:
                    response.raise_for_status()

                    optimized_parts = []
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data = line[6:]
                        if data.strip() == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                optimized_parts.append(content)
                        except (json.JSONDecodeError, IndexError, KeyError):
                            continue

                    result = "".join(optimized_parts).strip()
                    if result:
                        logger.info(
                            f"LLM result: {result[:40]}..."
                        )
                        return result

        except httpx.TimeoutException:
            logger.warning("LLM request timed out")
        except httpx.HTTPStatusError as e:
            logger.error(f"LLM HTTP error: {e.response.status_code}")
        except Exception as e:
            logger.error(f"LLM optimization error: {e}")

        return None
