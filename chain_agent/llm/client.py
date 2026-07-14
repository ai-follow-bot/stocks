"""
LLM 客户端抽象（provider-agnostic）

- AnthropicClient: Claude 系列
- OpenAICompatibleClient: Kimi/Moonshot/Deepseek/GLM 等 OpenAI 兼容 API

Provider 选择: config.LLM_PROVIDER = auto|anthropic|kimi|none
auto 优先 Anthropic，其次 OpenAI 兼容，均不可用则返回 None（调用方降级模板）
"""

from abc import ABC, abstractmethod
import sys
from typing import Optional

from .. import config


class LLMClient(ABC):
    @abstractmethod
    def synthesize(self, system: str, user: str, temperature: float = None) -> str:
        ...

    def synthesize_with_meta(self, system: str, user: str, temperature: float = None) -> dict:
        """返回 {"text": str, "stop_reason": str|None}。
        stop_reason 取值：
          - anthropic: "end_turn" | "max_tokens" | "stop_sequence" | ...
          - openai:    "stop" | "length" | "tool_calls" | ...
          截断统一规整为 "max_tokens" 方便调用方判断。
        temperature: None=provider 默认；评判等确定性场景传低值（如 0.2）降方差。"""
        text = self.synthesize(system, user, temperature=temperature)
        return {"text": text, "stop_reason": None}

    def synthesize_messages_with_meta(self, system: str, messages: list, temperature: float = None) -> dict:
        """多轮调用，用于 max_tokens 截断时续写拼接。
        messages 是完整的对话历史（含已生成的 assistant 轮）。
        默认实现退化为单轮：取最后一条 user 消息。
        子类应覆盖为真正的多轮 API 调用。"""
        last_user = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        return self.synthesize_with_meta(system, last_user, temperature=temperature)


class AnthropicClient(LLMClient):
    def __init__(self):
        if not config.ANTHROPIC_API_KEY:
            raise ValueError("ANTHROPIC_API_KEY 未设置")
        try:
            from anthropic import Anthropic
        except ImportError as e:
            raise ImportError("anthropic SDK 未安装") from e
        self._client = Anthropic(api_key=config.ANTHROPIC_API_KEY, timeout=config.LLM_REQUEST_TIMEOUT)
        self._model = config.ANTHROPIC_MODEL

    def synthesize(self, system: str, user: str, temperature: float = None) -> str:
        return self.synthesize_with_meta(system, user, temperature=temperature)["text"] or ""

    def synthesize_with_meta(self, system: str, user: str, temperature: float = None) -> dict:
        return self._create(system, [{"role": "user", "content": user}], temperature=temperature)

    def synthesize_messages_with_meta(self, system: str, messages: list, temperature: float = None) -> dict:
        return self._create(system, messages, temperature=temperature)

    def _create(self, system: str, messages: list, temperature: float = None) -> dict:
        kwargs: dict = {
            "model": self._model,
            "max_tokens": config.LLM_MAX_TOKENS,
            "system": system,
            "messages": messages,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        resp = self._client.messages.create(**kwargs)
        text = resp.content[0].text if resp.content else ""
        stop = getattr(resp, "stop_reason", None)
        if stop == "max_tokens":
            print(f"[LLM] [截断] Anthropic 响应被 max_tokens={config.LLM_MAX_TOKENS} 截断",
                  file=sys.stderr)
        return {"text": text, "stop_reason": stop}


class OpenAICompatibleClient(LLMClient):
    """覆盖 Kimi/Moonshot/Deepseek/GLM 等 OpenAI 兼容 API"""

    def __init__(self):
        if not config.OPENAI_API_KEY:
            raise ValueError("OPENAI_API_KEY/KIMI_API_KEY 未设置")
        try:
            from openai import OpenAI
        except ImportError as e:
            raise ImportError("openai SDK 未安装") from e
        self._client = OpenAI(
            api_key=config.OPENAI_API_KEY,
            base_url=config.OPENAI_BASE_URL,
            timeout=config.LLM_REQUEST_TIMEOUT,
        )
        self._model = config.OPENAI_MODEL

    def synthesize(self, system: str, user: str, temperature: float = None) -> str:
        return self.synthesize_with_meta(system, user, temperature=temperature)["text"] or ""

    def synthesize_with_meta(self, system: str, user: str, temperature: float = None) -> dict:
        return self._create([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ], temperature=temperature)

    def synthesize_messages_with_meta(self, system: str, messages: list, temperature: float = None) -> dict:
        # OpenAI 兼容 API 把 system 作为 messages 首条；若 messages 已含 system 则跳过
        if not any(m.get("role") == "system" for m in messages):
            messages = [{"role": "system", "content": system}] + messages
        return self._create(messages, temperature=temperature)

    def _create(self, messages: list, temperature: float = None) -> dict:
        # kimi-k2.6 默认开思考(reasoning_content)，思考 token 计入 max_tokens，评分/JSON 场景
        # 下思考吃光 max_tokens 致答案截断+重试爆炸+超时。对 kimi 默认关思考（答案直出 content）。
        kwargs: dict = {
            "model": self._model,
            "max_tokens": config.LLM_MAX_TOKENS,
            "messages": messages,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if self._model.startswith("kimi") and not config.KIMI_THINKING_ENABLED:
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        resp = self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0] if resp.choices else None
        text = choice.message.content if choice else ""
        finish = getattr(choice, "finish_reason", None) if choice else None
        # OpenAI 兼容: "length" 表示因 max_tokens 截断
        if finish == "length":
            print(f"[LLM] ⚠️ OpenAI 兼容响应被 max_tokens={config.LLM_MAX_TOKENS} 截断",
                  file=sys.stderr)
            finish = "max_tokens"
        return {"text": text, "stop_reason": finish}


def get_llm_client() -> Optional[LLMClient]:
    """
    根据 config.LLM_PROVIDER 返回可用 LLM 客户端，不可用返回 None。
    auto 模式：Anthropic 优先，失败降级 OpenAI 兼容
    """
    provider = config.LLM_PROVIDER

    if provider == "none":
        return None

    if provider in ("auto", "anthropic"):
        try:
            return AnthropicClient()
        except Exception as e:
            if provider == "anthropic":
                print(f"[LLM] Anthropic 不可用: {e}")
                return None
            # auto 模式继续尝试 OpenAI 兼容

    if provider in ("auto", "kimi", "openai"):
        try:
            return OpenAICompatibleClient()
        except Exception as e:
            print(f"[LLM] OpenAI 兼容客户端不可用: {e}")
            return None

    return None
