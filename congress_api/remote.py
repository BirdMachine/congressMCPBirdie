"""Fail-closed remote entry point; secrets stay in runtime environment.

Bearer headers work with MCP clients supporting them. ChatGPT's no-auth
connector can use the capability URL /connect/<random token>/mcp. Access
logs are disabled to avoid recording that URL. Prefer an OAuth proxy when
an organization's policies require identity-bound credentials.
"""
import os
import secrets

from starlette.responses import JSONResponse


class ProtectedMCP:
    def __init__(self, app, token: str):
        if len(token) < 32 or not token.isascii() or not all(c.isalnum() or c in "-_+/=" for c in token):
            raise ValueError(
                "MCP_ACCESS_TOKEN must be at least 32 ASCII characters using the base64 or base64url alphabet"
            )
        # Render generateValue uses standard base64, including +, / and =.
        # These are valid path characters; authenticate the entire exact path.
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope["path"] == "/healthz" and scope["method"] == "GET":
            return await JSONResponse({"status": "ok"})(scope, receive, send)
        path = scope["path"]
        headers = dict(scope.get("headers", []))
        bearer = headers.get(b"authorization", b"").decode("latin-1")
        capability = f"/connect/{self.token}/mcp"
        header_ok = secrets.compare_digest(bearer, f"Bearer {self.token}")
        path_ok = secrets.compare_digest(path, capability)
        if not ((header_ok and path in {"/mcp", "/mcp/"}) or path_ok):
            return await JSONResponse({"error": "unauthorized"}, status_code=401,
                                      headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"})(
                                          scope, receive, send)
        forwarded = dict(scope, path="/mcp", raw_path=b"/mcp")
        return await self.app(forwarded, receive, send)


def create_app():
    # Reuse upstream dotenv handling for local deployments, before fail-closed checks.
    from .core import api_config  # noqa: F401
    token = os.environ.get("MCP_ACCESS_TOKEN", "")
    if not os.environ.get("CONGRESS_API_KEY"):
        raise ValueError("CONGRESS_API_KEY must be configured server-side")
    from mcp.server.transport_security import TransportSecuritySettings
    from .main import server
    public_host = os.getenv("MCP_PUBLIC_HOST") or os.getenv("RENDER_EXTERNAL_HOSTNAME") or "localhost"
    if not public_host.isascii() or not all(c.isalnum() or c in ".-" for c in public_host):
        raise ValueError("MCP_PUBLIC_HOST must be a hostname without a scheme or path")
    security = TransportSecuritySettings(
        allowed_hosts=[public_host, f"{public_host}:*", "localhost", "localhost:*", "127.0.0.1:*"],
        allowed_origins=[f"https://{public_host}", "https://chatgpt.com", "http://localhost:*"],
    )
    return ProtectedMCP(server.streamable_http_app(stateless_http=True, transport_security=security), token)


def main():
    import uvicorn
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8000")), access_log=False)


if __name__ == "__main__":
    main()
