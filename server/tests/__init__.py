"""Test package for the Invigismart Pro backend.

The suite drives the real server over HTTP and needs no third-party packages.
Either of these runs the same tests:

    python3 server/tests/test_api.py
    python3 -m unittest discover -s server/tests

This file exists so the second form works: unittest only treats a start
directory as a place to import tests from when it is a package.
"""
