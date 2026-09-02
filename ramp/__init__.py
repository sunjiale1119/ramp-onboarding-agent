"""爬坡 Ramp — 新人入职 30 天带教 Agent。"""

__version__ = "0.1.0"

# Windows 控制台默认 GBK，中文与 ✓ 之类的符号会直接抛 UnicodeEncodeError。
# 这个项目是 CLI 优先的，所以在包级别把标准流强制成 UTF-8。
import sys as _sys

for _stream in (_sys.stdout, _sys.stderr):
    try:
        if _stream and getattr(_stream, "encoding", "").lower() not in ("utf-8", "utf8"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 某些宿主环境的流不支持 reconfigure
        pass
