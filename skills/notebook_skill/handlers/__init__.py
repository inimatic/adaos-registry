try:
    from .main import handle  # noqa: F401
except ImportError:
    handle = None  # type: ignore[assignment]
