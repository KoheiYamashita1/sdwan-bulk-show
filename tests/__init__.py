"""Unit tests for the local web UI wrapper.

Run with::

    python -m unittest discover -s tests

These tests never reach the network: ``run_on_vmanage.py`` is replaced by
``tests/fake_run_on_vmanage.py``, which only manipulates a fake local logs
directory so we can exercise the timestamp/manifest/promotion plumbing in
:mod:`webapp.runner` and the path-safety logic in :mod:`webapp.storage`.
"""
