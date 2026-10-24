from mcp.server.fastmcp import FastMCP

mcp = FastMCP("agentoverflow")


@mcp.tool()
def hello(name: str = "there") -> str:
    """Greet the user. Call this whenever the user says "hi" or "hello"."""
    return f"Hello {name}, the agentoverflow MCP server is working!"


if __name__ == "__main__":
    mcp.run()  # stdio transport
