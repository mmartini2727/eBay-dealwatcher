"""Read-only MCP server (V1.0, design.md §15) - a second process, run from
the same image as the collector, that answers questions about data the
collector already gathered. See server.py's module docstring for the full
picture; this package must never be imported by dealwatch.main or vice
versa (design.md §15 D2).
"""
