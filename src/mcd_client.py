"""麦当劳 MCP Server 客户端封装。

通过 Streamable HTTP + JSON-RPC 2.0 协议调用麦当劳中国官方 MCP Server。
仅从环境变量读取 Token，绝不硬编码任何真实凭证。
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

import requests


class McdMcpError(RuntimeError):
    """调用麦当劳 MCP Server 失败时抛出。"""


class McdMcpClient:
    """麦当劳 MCP Server 的轻量 JSON-RPC 客户端。

    使用方式::

        client = McdMcpClient.from_env()
        data = client.call_tool("list-nutrition-foods")
    """

    DEFAULT_URL = "https://mcp.mcd.cn"

    def __init__(self, url: str, token: str, timeout: int = 60) -> None:
        if not token or token == "YOUR_MCP_TOKEN":
            raise McdMcpError(
                "未配置有效的 MCD_MCP_TOKEN。请在环境变量或 .env 中填入你申请到的麦当劳 MCP Token。"
            )
        self.url = url.rstrip("/") if url else self.DEFAULT_URL
        self.token = token
        self.timeout = timeout
        self._id = 0

    @classmethod
    def from_env(cls, timeout: int = 60) -> "McdMcpClient":
        """从环境变量 MCD_MCP_URL / MCD_MCP_TOKEN 构建客户端。"""
        url = os.environ.get("MCD_MCP_URL", cls.DEFAULT_URL)
        token = os.environ.get("MCD_MCP_TOKEN", "")
        return cls(url=url, token=token, timeout=timeout)

    # ------------------------------------------------------------------ #
    # 底层 JSON-RPC
    # ------------------------------------------------------------------ #
    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            # 麦当劳 MCP 以 Streamable HTTP 返回，可能是 JSON 或 SSE 帧
            "Accept": "application/json, text/event-stream",
        }

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _rpc(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id(), "method": method}
        if params is not None:
            body["params"] = params

        try:
            resp = requests.post(
                self.url,
                headers=self._headers(),
                data=json.dumps(body),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:  # pragma: no cover - 网络异常
            raise McdMcpError(f"请求麦当劳 MCP Server 失败: {exc}") from exc

        if resp.status_code == 401:
            raise McdMcpError("MCP Token 无效、已过期或未提供 (HTTP 401)。请检查 MCD_MCP_TOKEN。")
        if resp.status_code == 429:
            raise McdMcpError("触发限流 (HTTP 429，600 次/分钟上限)。请降低请求频率后重试。")
        if resp.status_code >= 400:
            raise McdMcpError(f"麦当劳 MCP Server 返回错误 HTTP {resp.status_code}: {resp.text[:300]}")

        payload = self._parse_streamable(resp.text)
        if "error" in payload:
            raise McdMcpError(f"JSON-RPC 错误: {payload['error']}")
        return payload.get("result", {})

    @staticmethod
    def _parse_streamable(text: str) -> Dict[str, Any]:
        """解析 Streamable HTTP 响应：既支持纯 JSON，也支持 SSE 的 data: 帧。"""
        stripped = text.lstrip()
        if stripped.startswith("event:") or stripped.startswith("data:") or "\ndata:" in text:
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    chunk = line[len("data:"):].strip()
                    if chunk and chunk != "[DONE]":
                        try:
                            return json.loads(chunk)
                        except json.JSONDecodeError:
                            continue
            raise McdMcpError(f"无法解析 SSE 响应: {text[:300]}")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise McdMcpError(f"无法解析响应为 JSON: {text[:300]}") from exc

    # ------------------------------------------------------------------ #
    # 高层接口
    # ------------------------------------------------------------------ #
    def initialize(self) -> Dict[str, Any]:
        """执行 MCP 握手。调用业务工具前建议先执行一次。"""
        return self._rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "mcd-fitness-calorie-coach", "version": "1.0.0"},
            },
        )

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> str:
        """调用麦当劳 MCP 工具，返回工具输出的纯文本内容。"""
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        content = result.get("content", [])
        texts = [item.get("text", "") for item in content if item.get("type", "text") == "text"]
        if not texts:
            texts = [item.get("text", "") for item in content]
        return "\n".join(t for t in texts if t)

    def list_nutrition_foods(self) -> str:
        """调用 list-nutrition-foods，返回原始营养信息文本。"""
        return self.call_tool("list-nutrition-foods")

    def now_time_info(self) -> str:
        """调用 now-time-info，返回当前时间信息文本。"""
        return self.call_tool("now-time-info")
