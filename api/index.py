#!/usr/bin/env python3
"""
api/index.py — Vercel serverless entry point for the Foosball Tracker.

Vercel's Python runtime imports this file and instantiates a class named
`handler` that subclasses BaseHTTPRequestHandler; it calls do_GET / do_POST
per request (the request line, headers, and body are provided exactly like the
stdlib server). We adapt each request into the shared webapp.handle(...) via
webapp.serve_via_bhrh — identical code path to the local LAN server.

Storage backend is chosen by env vars (store.get_store): GitHubStore when
GITHUB_TOKEN + GITHUB_REPO are set (the normal Vercel config), else LocalStore.

ASSUMPTION (documented in HOSTING.md): the current Vercel Python runtime uses
the `handler(BaseHTTPRequestHandler)` contract. If a deployment instead expects
a WSGI/ASGI `app` callable, wrap webapp.handle in a tiny WSGI adapter — the
transport-agnostic core does not change.
"""

import os
import sys
from http.server import BaseHTTPRequestHandler

# api/ is a subdirectory; make the top-level modules (core/store/webapp)
# importable regardless of Vercel's working directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webapp                    # noqa: E402
from store import get_store      # noqa: E402

# One store per warm lambda instance. Stateless across cold starts by design
# (GitHub is the source of truth; trial state lives in the signed cookie).
STORE = get_store()


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        webapp.serve_via_bhrh(self, "GET", STORE)

    def do_POST(self):
        webapp.serve_via_bhrh(self, "POST", STORE)

    def log_message(self, *args):
        pass  # keep serverless logs clean; never log request/token details
