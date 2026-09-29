# app/llm.py — OpenAI 兼容 API 客户端（httpx 直调，不依赖 openai 包）
import json
import math
from concurrent.futures import ThreadPoolExecutor
from typing import AsyncIterator

import httpx

# 应用层看门狗线程池：httpx 的库级超时在网关"连接活着但不回响应"时可能失效
# （read timeout 会被偶发字节重置，实测导致图谱生成进程挂死 22 小时），
# 因此所有同步 LLM 调用都在工作线程中执行，主线程用 fut.result(hard_timeout) 兜底。
# 看门狗触发后该工作线程会泄漏（罕见事件，进程重启即清理），换取进程不挂死。
_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="llm-call")


class LLMError(RuntimeError):
    pass


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class LLMClient:
    """chat + embedding，通过 OpenAI 兼容 /chat/completions 与 /embeddings。"""

    def __init__(self, cfg: dict, hard_timeout: float | None = None,
                 embed_hard_timeout: float | None = None):
        self.api_base = str(cfg.get("api_base", "")).rstrip("/")
        self.api_key = cfg.get("api_key", "")
        self.chat_model = cfg.get("chat_model", "")
        self.embedding_model = cfg.get("embedding_model", "")
        self.temperature = float(cfg.get("temperature", 0.1))
        self.extra_headers = dict(cfg.get("extra_headers") or {})
        # 看门狗硬超时：默认 660s = 库级 600s + 60s 余量；embed 独立默认 180s
        self.hard_timeout = hard_timeout if hard_timeout is not None else 660.0
        self.embed_hard_timeout = (embed_hard_timeout if embed_hard_timeout is not None
                                   else 180.0)
        if not self.api_base or not self.api_key:
            raise LLMError("缺少 api_base / api_key，请先在界面配置大模型（PUT /api/settings）")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json", **self.extra_headers}

    # ---- chat ----
    def _post_chat(self, payload: dict) -> dict:
        """在工作线程中执行的原始 HTTP 调用（受 fut.result 硬超时保护）。"""
        try:
            resp = httpx.post(
                f"{self.api_base}/chat/completions",
                headers=self._headers(),
                json=payload,
                timeout=600,  # 推理型模型长文生成需要较长时间（部分法规关系抽取超 300s）
            )
        except httpx.HTTPError as e:  # 连接失败/读超时等，统一转 LLMError
            raise LLMError(f"chat 网络错误: {type(e).__name__}: {e}")
        if resp.status_code != 200:
            raise LLMError(f"chat 失败 {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json()["choices"][0]["message"]
        except (KeyError, IndexError, ValueError) as e:
            raise LLMError(f"chat 响应结构异常: {e}")

    def chat(self, messages: list[dict], tools: list | None = None,
             stream: bool = False, model: str | None = None,
             max_tokens: int | None = None):
        """非流式：返回 dict（choices[0].message）。stream=False 时使用。"""
        payload = {
            "model": model or self.chat_model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = tools
        fut = _EXECUTOR.submit(self._post_chat, payload)
        try:
            return fut.result(timeout=self.hard_timeout)
        except TimeoutError:
            fut.cancel()
            raise LLMError(
                f"chat 硬超时（>{self.hard_timeout:.0f}s，网关疑似挂起，看门狗触发）")

    async def chat_stream(self, messages: list[dict], tools: list | None = None,
                          model: str | None = None) -> AsyncIterator[str]:
        """流式：逐 token yield 文本增量。"""
        payload = {
            "model": model or self.chat_model,
            "messages": messages,
            "temperature": self.temperature,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
        async with httpx.AsyncClient(timeout=180) as client:
            async with client.stream(
                "POST", f"{self.api_base}/chat/completions",
                headers=self._headers(), json=payload,
            ) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode(errors="replace")
                    raise LLMError(f"chat 失败 {resp.status_code}: {body[:300]}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data.strip() == "[DONE]":
                        return
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0].get("delta", {})
                        if delta.get("content"):
                            yield delta["content"]
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue

    # ---- embedding ----
    def _post_embed(self, chunk: list[str]) -> list[list[float]]:
        try:
            resp = httpx.post(
                f"{self.api_base}/embeddings",
                headers=self._headers(),
                json={"model": self.embedding_model, "input": chunk},
                timeout=120,
            )
        except httpx.HTTPError as e:
            raise LLMError(f"embedding 网络错误: {type(e).__name__}: {e}")
        if resp.status_code != 200:
            raise LLMError(f"embedding 失败 {resp.status_code}: {resp.text[:300]}")
        try:
            data = sorted(resp.json()["data"], key=lambda d: d["index"])
            return [d["embedding"] for d in data]
        except (KeyError, IndexError, ValueError) as e:
            raise LLMError(f"embedding 响应结构异常: {e}")

    def embed(self, texts: list[str], batch: int = 64) -> list[list[float]]:
        """批量 embedding（同步）。文本截断至 ~3000 字。"""
        out: list[list[float]] = []
        for i in range(0, len(texts), batch):
            chunk = [t[:3000] for t in texts[i:i + batch]]
            fut = _EXECUTOR.submit(self._post_embed, chunk)
            try:
                out.extend(fut.result(timeout=self.embed_hard_timeout))
            except TimeoutError:
                fut.cancel()
                raise LLMError(
                    f"embedding 硬超时（>{self.embed_hard_timeout:.0f}s，网关疑似挂起）")
        return out


if __name__ == "__main__":
    # python -m app.llm --test ：连通性自检（需已配置真实 API）
    import argparse
    from pathlib import Path

    from app.config import load_config

    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    cfg = load_config(Path(__file__).resolve().parent.parent / "data")
    try:
        client = LLMClient(cfg)
    except LLMError as e:
        print("配置不完整:", e)
        raise SystemExit(1)
    if args.test:
        msg = client.chat([{"role": "user", "content": "回复两个字：连通"}])
        print("chat OK:", msg["content"][:50])
        try:
            vec = client.embed(["测试"])
            print("embedding OK: dim =", len(vec[0]))
        except LLMError as e:
            print("embedding 不可用（网关不支持时向量检索自动降级为关键词）:", str(e)[:120])
