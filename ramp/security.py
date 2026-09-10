"""Same-origin JSON writes, bounded bodies, login throttling and safe metadata logs."""
import logging
import time
import uuid
from collections import OrderedDict

from starlette.responses import JSONResponse

log = logging.getLogger("ramp.access")


class BoundaryMiddleware:
    def __init__(self, app):
        self.app = app
        self.attempts = OrderedDict()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        path, method = scope["path"], scope["method"]
        rid = uuid.uuid4().hex
        start = time.monotonic()
        async def reject(status, msg):
            await JSONResponse({"detail": msg, "request_id": rid}, status_code=status)(scope, receive, send)
        if path in ("/api/login", "/api/register") and method == "POST":
            key = (scope.get("client") or ("unknown",))[0]
            now = time.monotonic()
            count, until = self.attempts.get(key, (0, now+300))
            if now >= until:
                count, until = 0, now+300
            self.attempts[key] = (count+1, until)
            self.attempts.move_to_end(key)
            if len(self.attempts) > 10000:
                self.attempts.popitem(last=False)
            if count >= 30:
                return await reject(429, "登录或注册过于频繁，请5分钟后再试")
        body = None
        if path.startswith("/api/") and method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = headers.get(b"origin", b"").decode()
            expected = scope.get("scheme", "http") + "://" + headers.get(b"host", b"").decode()
            if headers.get(b"sec-fetch-site") == b"cross-site" or (origin and origin != expected):
                return await reject(403, "拒绝跨站写入，请从本系统页面操作")
            if method != "DELETE" and not headers.get(b"content-type", b"").lower().startswith(b"application/json"):
                return await reject(415, "写入请求必须使用 JSON")
            body = bytearray()
            while True:
                part = await receive()
                if part["type"] == "http.disconnect":
                    return
                body.extend(part.get("body", b""))
                if len(body) > 131072:
                    return await reject(413, "请求超过128 KB，请缩小内容")
                if not part.get("more_body"):
                    break
        sent_body = False
        async def buffered_receive():
            nonlocal sent_body
            if body is not None and not sent_body:
                sent_body = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()
        async def response_send(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).extend([(b"x-request-id", rid.encode()),
                    (b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"same-origin")])
                if path.startswith("/api/"):
                    message["headers"].append((b"cache-control", b"no-store"))
                # Deliberately exclude query, body, cookie and model contents.
                log.info("request=%s method=%s status=%s duration_ms=%d", rid, method,
                    message["status"], int((time.monotonic()-start)*1000))
            await send(message)
        await self.app(scope, buffered_receive, response_send)
