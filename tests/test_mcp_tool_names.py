from types import SimpleNamespace

from harness.mcp.tools import canonical_tool_name


def test_canonical_tool_name_is_domain_scoped_and_safe():
    server = SimpleNamespace(name="GitHub Production")

    assert canonical_tool_name(server, "search") == "GitHub_Production__search"
