import os
import sys

# Add root directory to sys.path so app and main can be imported
root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from main import app as fastapi_app


class VercelASGIAdapter:
    """
    Vercel Serverless ASGI Adapter.
    Handles Vercel path rewrites cleanly without requiring any changes to main.py.
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = scope.get("path", "")
            query = scope.get("query_string", b"").decode("utf-8")

            # 1. Recover original path from __path__ query param (set by vercel.json)
            if "__path__=" in query:
                for param in query.split("&"):
                    if param.startswith("__path__="):
                        val = param.split("=", 1)[1]
                        scope["path"] = ("/" + val.lstrip("/")).split("?")[0]
                        break
            # 2. Recover from Vercel forward headers if rewritten to /api/index.py
            elif path in ("/api/index.py", "/api/index", "/api/index/"):
                for header_name, header_val in scope.get("headers", []):
                    if header_name.lower() in (b"x-forwarded-uri", b"x-matched-path", b"x-original-url"):
                        orig = header_val.decode("utf-8")
                        scope["path"] = ("/" + orig.lstrip("/")).split("?")[0]
                        break

            # 3. Route /openapi.json alias to /api/openapi.json if requested
            if scope.get("path") == "/openapi.json":
                scope["path"] = "/api/openapi.json"
            elif scope.get("path") in ("/api/docs", "/api/docs/"):
                scope["path"] = "/docs"

        await self.app(scope, receive, send)


app = VercelASGIAdapter(fastapi_app)
