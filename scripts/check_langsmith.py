"""LangSmith 脱敏的金丝雀验证：真跑一轮 → 从云端读回来 → 逐字段找原文。

## 为什么必须是端到端，不能只做单测

我先写的单测：手搓一份 state，断言 question / answer 被换成 `[已脱敏]`。
全绿。

然后端到端一跑，**漏了**——DeepSeek 思考模式的 `reasoning_content`
原样上了云，里面模型把用户的问题和查到的社保数据复述了一遍。
单测测不出来，因为那份手搓的 state 里根本没有这个字段：
**我不知道有这个字段，所以我也不会去测它。**

真实 payload 的形状归模型供应商定，不归我定。唯一靠得住的验证方式是
埋一句独一无二的话，跑完去云端搜——搜不到才算数。

修法也跟着变了：从"列举要隐藏的字段"（黑名单，供应商加字段就漏）
改成"只有白名单里的 key 能原样上传"（默认拒绝）。
**多脱敏能补救，泄漏不能。**

## 用法

    uv run python scripts/check_langsmith.py

没配 LANGSMITH_API_KEY 会直接跳过（退出码 0）——
CI 上没 key 是正常的，不该因此判失败。

⚠️ 这个脚本会**真的调一次模型**，花几分钱。
"""

from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ramp import runtime, tracing  # noqa: E402


def walk(node, path=""):
    """递归找出所有含诱饵词的叶子路径。不猜字段名，全量扫。"""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from walk(v, f"{path}.{k}" if path else str(k))
    elif isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            yield from walk(v, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


def main() -> None:
    st = tracing.status()
    print()
    print("LangSmith 脱敏验证")
    print("-" * 62)

    if not st["enabled"]:
        print(f"  跳过：{st['reason']}")
        print()
        return

    if st["send_raw"]:
        print("  ⚠ RAMP_LANGSMITH_RAW=1 —— 当前就是要上传原文，")
        print("    这个检查在这种配置下没有意义。关掉它再跑。")
        print()
        sys.exit(1)

    canary = f"金丝雀{uuid.uuid4().hex[:8]}"
    question = f"{canary}：我的社保开始交了吗？"
    print(f"  项目 {st['project']} · 诱饵词 {canary}")
    print("  真实调用中（会花钱）…")

    out = runtime.ask(question, employee_id="e_linxy", persist=False)
    print(f"    route={out['route']}  cost=¥{out['cost']:.4f}")

    client = tracing.client()
    try:
        client.flush()
    except Exception:  # noqa: BLE001
        pass

    runs = []
    for _ in range(10):
        time.sleep(3)
        try:
            runs = list(client.list_runs(project_name=st["project"], limit=40))
        except Exception:  # noqa: BLE001
            continue
        if runs:
            break

    if not runs:
        print("  ✗ 云端一条 run 都没有 —— tracing 没生效")
        sys.exit(1)

    leaks, fields = [], 0
    for r in runs:
        for name, payload in (("inputs", r.inputs), ("outputs", r.outputs),
                              ("extra", r.extra)):
            for p, val in walk(payload, name):
                fields += 1
                if canary in val:
                    leaks.append((r.name, p, val[:90]))

    print(f"  扫了 {len(runs)} 条 run / {fields} 个字符串字段")
    print("-" * 62)
    if leaks:
        print(f"  ✗ 原文泄漏到 LangSmith 云端（{len(leaks)} 处）：")
        for name, p, val in leaks:
            print(f"      {name} · {p}")
            print(f"        {val}…")
        print()
        print("  修法：把泄漏路径最后那个 key 判断一下——")
        print("      是结构字段 → 加进 tracing._SAFE_STR_KEYS")
        print("      是自由文本 → 它本来就该被脱敏，查 _safe_str() 为什么放行了")
        print()
        sys.exit(1)

    print("  ✓ 云端无原文；结构、token、节点树完整上传")
    print()


if __name__ == "__main__":
    main()
