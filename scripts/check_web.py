"""前端静态自检：路径对不对、JS 能不能解析。

## 为什么需要这个

前端从"一个 index.html 装五个视角"拆成"一页一个角色"之后，
出现了两类只有点到那一页才会暴露的错：

1. **路径写错。** 单页时代所有 fetch 挤在一个文件里，互相抄还能抄对；
   拆开之后每页各写各的，一个 typo 要等用户点到那个标签页才发现。
   拆页当天就抓到两处真问题——新人页在调 mentor 的接口、
   mentor 页在调 HR 的接口。单页时共用一份 JS，谁调谁的没人看得出来。

2. **JS 语法崩。** 一个没配对的引号会让整页脚本停在那一行，
   顶栏不渲染、表格空白，但 HTTP 依然 200。我就这么踩过一次：
   往模板字符串里塞了 `font-family:'IBM Plex Mono'`，
   单引号提前闭合，整个管理后台白屏。

两件事都能在改完的三秒内查出来，不该留到手点。

用法：
    uv run python scripts/check_web.py
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

WEB = ROOT / "ramp" / "web"

CALL = re.compile(r"\bapi\(\s*([^,)]+)")
FETCH = re.compile(r"fetch\(\s*'(/api[^']*)'")
SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)


# ------------------------------------------------------------------ 路径
def split_ternary(expr: str) -> list[str]:
    """按 ? : 切三元分支，但**只切引号外面的**。

    第一版直接 re.split(r'[?:]')，结果把 '/ops/sessions?limit=10' 里的
    查询问号也切了——那条调用被拆成碎片，一条都没匹配上，于是
    **静悄悄地整条跳过了检查**。误报会吵，漏检不会，所以漏检更危险。
    """
    parts, buf, in_quote = [], [], False
    for ch in expr:
        if ch == "'":
            in_quote = not in_quote
        if ch in "?:" and not in_quote:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def templates(expr: str) -> list[str]:
    """一个调用表达式 → 它可能命中的路径模板。

    按 + 分词再拼回去：字面量取引号里的内容，变量段出一个 *。
    先试过正则替换，越写越绕——`'/newbie/' + EMP + '/memory'` 里
    后半段的 '/memory' 总被吃掉，然后报一个假的"路径不存在"。
    """
    out = []
    for branch in split_ternary(expr):
        branch = branch.strip()
        if not branch or "'" not in branch:
            continue
        parts = []
        for tok in branch.split("+"):
            tok = tok.strip()
            m = re.fullmatch(r"'([^']*)'", tok)
            parts.append(m.group(1) if m else ("*" if tok else ""))
        path = "".join(parts)
        if not path.startswith("/"):
            continue
        path = ("/api" + path).split("?")[0]
        out.append(re.sub(r"/{2,}", "/", path).rstrip("/"))
    return out


def check_paths() -> list[str]:
    from ramp.api import app

    real = {re.sub(r"\{[^}]+\}", "*", r.path)
            for r in app.routes if getattr(r, "path", "").startswith("/api")}

    bad, n = [], 0
    for f in sorted(WEB.glob("*.html")) + sorted(WEB.glob("*.js")):
        src = f.read_text(encoding="utf-8")
        found: dict[str, list[str]] = {}
        for m in CALL.finditer(src):
            expr = m.group(1).strip()
            if "'" not in expr:      # api(p) —— _shared.js 里的包装函数本身
                continue
            found[expr] = templates(expr)
        for m in FETCH.finditer(src):
            lit = m.group(1)
            if lit == "/api":        # fetch('/api' + p, o) —— 同上
                continue
            found[lit] = [lit.split("?")[0].rstrip("/")]

        for expr, tmpls in found.items():
            for t in tmpls:
                n += 1
                if t in real:
                    continue
                if any(re.fullmatch(t.replace("*", "[^/]+"), r) for r in real):
                    continue
                bad.append(f"{f.name}: api({expr})  →  {t}  后端没有这个路径")

    print(f"  路径核对：{n} 个调用点 / 后端 {len(real)} 个接口"
          f" → {'全部对得上' if not bad else str(len(bad)) + ' 个对不上'}")
    return bad


# ------------------------------------------------------------------ 语法
def check_js() -> list[str]:
    """用 node --check 解析每个页面的内联 script。

    没装 node 就跳过——检查缺失要说出来，不能假装通过。
    """
    try:
        subprocess.run(["node", "--version"], capture_output=True, check=True)
    except Exception:
        print("  JS 语法：跳过（没找到 node）")
        return []

    bad, n = [], 0
    for f in sorted(WEB.glob("*.html")) + sorted(WEB.glob("*.js")):
        blocks = ([f.read_text(encoding="utf-8")] if f.suffix == ".js"
                  else SCRIPT.findall(f.read_text(encoding="utf-8")))
        for i, code in enumerate(blocks):
            if not code.strip():
                continue
            n += 1
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                             encoding="utf-8") as t:
                # 顶层 await 要包一层才是合法脚本
                t.write("(async function(){\n" + code + "\n})();")
                tmp = t.name
            r = subprocess.run(["node", "--check", tmp],
                               capture_output=True, text=True)
            Path(tmp).unlink(missing_ok=True)
            if r.returncode:
                err = [ln for ln in r.stderr.splitlines() if "Error" in ln]
                bad.append(f"{f.name} 第 {i + 1} 段 script: "
                           f"{err[0] if err else r.stderr.strip()[:90]}")

    print(f"  JS 语法：{n} 段脚本 → "
          f"{'全部能解析' if not bad else str(len(bad)) + ' 段解析失败'}")
    return bad


def main() -> None:
    print()
    print("前端自检")
    print("-" * 60)
    bad = check_paths() + check_js()
    print("-" * 60)
    if bad:
        for b in bad:
            print(f"  ! {b}")
        print()
        sys.exit(1)
    print("  通过")
    print()


if __name__ == "__main__":
    main()
