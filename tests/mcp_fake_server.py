"""A tiny stdio MCP server for tests/test_mcp_mount.py."""
from fastmcp import FastMCP

mcp = FastMCP("fake")


@mcp.tool(annotations={"readOnlyHint": True})
def lookup(query: str, limit: int = 3) -> str:
    """Look something up."""
    return f"found {query} x{limit}"


@mcp.tool()
def send_note(text: str) -> str:
    """Send a note somewhere."""
    return f"sent {text}"


@mcp.tool(annotations={"readOnlyHint": True})
def danger(x: int) -> str:
    """Read-only by hint, but excluded or overridden in tests."""
    return "d"


@mcp.tool(name="weird.name-with stuff", annotations={"readOnlyHint": True})
def weird() -> str:
    """Tool with an awkward name."""
    return "w"


if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)
