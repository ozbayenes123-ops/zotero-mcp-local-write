import argparse

from zotero_mcp import mcp


def main():
    parser = argparse.ArgumentParser(description="Zotero Model Contect Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport to use",
    )
    args = parser.parse_args()

    mcp.run(args.transport)


if __name__ == "__main__":
    main()
