import zotero_mcp


def test_package_exports():
    assert hasattr(zotero_mcp, "__version__") or hasattr(zotero_mcp, "client")


def test_submodules_import():
    import zotero_mcp.client  # noqa: F401
    import zotero_mcp.writes  # noqa: F401
    import zotero_mcp.cli  # noqa: F401
