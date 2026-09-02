"""工具注册表与执行器。

三条契约，每条都对应一个产品决策：

1. **参数 schema 是给模型看的文档。** description 写不清，模型就调不对。
   所以每个参数都要说明单位、取值范围、以及"什么时候不该传"。

2. **writes 标记决定要不要人工确认。** 这不是工具作者的自由裁量，
   是执行器强制的：writes=True 的工具永远不会被直接执行，
   它只会返回一个待确认的 pending_action。

3. **domains 决定谁能看到这个工具。** HR 域的 Agent 拿不到 it_create_ticket，
   不是因为它"不该用"，而是因为它在那个域的工具表里根本不存在——
   权限隔离要落在可见性上，不能只落在提示词里。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable


class ToolError(RuntimeError):
    """工具执行失败。带上用户可读的降级文案，而不是把栈抛给用户。"""

    def __init__(self, message: str, *, user_message: str | None = None) -> None:
        super().__init__(message)
        self.user_message = user_message or "这个查询暂时没成功，我先按已知信息回答，稍后可以再试一次。"


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., Any]
    domains: tuple[str, ...] = ("hr", "it", "biz")
    writes: bool = False
    timeout_s: float = 10.0

    def schema(self) -> dict[str, Any]:
        """OpenAI function-calling 格式。writes 会写进 description，
        让模型知道这个动作需要用户确认。"""
        desc = self.description
        if self.writes:
            desc += "（写入类操作：调用后不会立即执行，会先向用户展示待写入内容并等待确认）"
        return {
            "type": "function",
            "function": {"name": self.name, "description": desc, "parameters": self.parameters},
        }


@dataclass
class ToolResult:
    name: str
    ok: bool
    data: Any = None
    error: str | None = None
    user_message: str | None = None
    duration_ms: int = 0
    needs_confirmation: bool = False
    pending_action: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "data": self.data,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "needs_confirmation": self.needs_confirmation,
        }


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"工具重名: {tool.name}")
        self._tools[tool.name] = tool
        return tool

    def add(self, **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.register(Tool(fn=fn, **kw))
            return fn

        return deco

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def for_domain(self, domain: str | None) -> list[Tool]:
        """域可见的工具集——权限隔离的落点。"""
        if not domain:
            return list(self._tools.values())
        return [t for t in self._tools.values() if domain in t.domains]

    def schemas(self, domain: str | None = None) -> list[dict[str, Any]]:
        return [t.schema() for t in self.for_domain(domain)]

    def names(self, domain: str | None = None) -> list[str]:
        return [t.name for t in self.for_domain(domain)]

    def __len__(self) -> int:
        return len(self._tools)

    # -- 执行 ------------------------------------------------------
    def execute(
        self,
        name: str,
        args: dict[str, Any],
        *,
        domain: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            return ToolResult(name, False, error=f"未注册的工具: {name}",
                              user_message="我没有这个能力，换个方式帮你查。")
        if domain and domain not in tool.domains:
            # 越域调用：这是权限事件，要能在 trace 里被看见
            return ToolResult(
                name, False,
                error=f"工具 {name} 不在域 {domain} 的可见范围内",
                user_message="这件事不在我当前的职责范围内，我帮你转到对应的同事。",
            )

        # 写入类工具不真的执行，只生成待确认动作
        if tool.writes:
            preview = tool.fn(**args, _preview=True, _context=context or {})
            return ToolResult(
                name, True, data=preview, needs_confirmation=True,
                pending_action={"tool": name, "args": args, "preview": preview},
            )

        t0 = time.perf_counter()
        try:
            data = tool.fn(**args, _context=context or {})
            return ToolResult(name, True, data=data,
                              duration_ms=int((time.perf_counter() - t0) * 1000))
        except ToolError as exc:
            return ToolResult(name, False, error=str(exc), user_message=exc.user_message,
                              duration_ms=int((time.perf_counter() - t0) * 1000))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                name, False, error=f"{type(exc).__name__}: {exc}",
                user_message="这个查询出了点问题，我先按已知信息回答。",
                duration_ms=int((time.perf_counter() - t0) * 1000),
            )

    def commit(self, pending: dict[str, Any], context: dict[str, Any] | None = None) -> ToolResult:
        """用户确认后，真正执行写入。"""
        name = pending["tool"]
        tool = self.get(name)
        if tool is None:
            return ToolResult(name, False, error="工具已不存在")
        t0 = time.perf_counter()
        try:
            data = tool.fn(**pending["args"], _preview=False, _context=context or {})
            return ToolResult(name, True, data=data,
                              duration_ms=int((time.perf_counter() - t0) * 1000))
        except Exception as exc:  # noqa: BLE001
            return ToolResult(name, False, error=f"{type(exc).__name__}: {exc}",
                              user_message="提交没成功，你可以稍后再试或直接联系 IT 服务台。")


registry = Registry()

from . import builtin  # noqa: E402,F401  —— 导入即注册

__all__ = ["Tool", "ToolResult", "ToolError", "Registry", "registry"]
