"""xiaohongshu-matrices-cli: Xiaohongshu CLI via reverse-engineered API."""

try:
    from importlib.metadata import version

    __version__ = version("xiaohongshu-matrices-cli")
except Exception:
    __version__ = "0.0.0"
