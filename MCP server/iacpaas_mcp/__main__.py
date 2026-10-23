from .config import settings
from .server import mcp

if __name__ == "__main__":
    if settings.transport == "http":
        mcp.run(transport="http", host=settings.host, port=settings.port)
    else:
        mcp.run()
