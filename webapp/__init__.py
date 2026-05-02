"""Local FastAPI web UI for sdwan-bulk-show.

This package wraps `run_on_vmanage.py` so a Mac-local user can drive the
existing CLI through a browser without exposing it over the network. See
``webapp.main`` for the FastAPI app and ``webapp.runner`` for the subprocess
wrapper.
"""

__all__ = ["main", "runner", "storage"]
