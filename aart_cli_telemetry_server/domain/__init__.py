"""The functional core: what an event is, how a request is read, and what the dashboard counts.

Pure. Nothing here reads a clock, a file, a database or the network; `tests/test_architecture.py`
holds that, and `tach.toml` holds that nothing here depends on another layer.
"""
