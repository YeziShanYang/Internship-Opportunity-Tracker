"""The two HTTP clients, and why there are two of them.

This is a credential boundary rather than a style choice. The GitHub client puts
`Authorization: Bearer <GH_PAT>` on every request it makes. While only GitHub was
contacted that was harmless; the moment a job board or a careers page shares the client,
the PAT is sent to boards-api.greenhouse.io, api.lever.co, api.ashbyhq.com and every
firm's marketing site. The web client carries the honest User-Agent and no credentials
at all.

Both live in `gather` because that is the layer that owns the network. `enrich` builds
a web client too and imports it from here, which the layering allows -- a stage may
import at or below its own level.
"""
from __future__ import annotations

import os
import sys

import httpx

from core import paths
from gather import urllib_transport


def github_token() -> str | None:
    """The PAT, or None with a note. Unauthenticated still works, 83x slower."""
    token = os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print(
            "note: no GH_PAT/GITHUB_TOKEN set; using unauthenticated GitHub API "
            "(60 requests/hour instead of 5,000)",
            file=sys.stderr,
        )
    return token


def build_github_client(token: str | None) -> httpx.Client:
    headers = {"User-Agent": paths.USER_AGENT, "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(
        headers=headers, timeout=paths.HTTP_TIMEOUT_SECONDS, follow_redirects=True
    )


def build_web_client() -> httpx.Client:
    """No credentials, ever. Spec section 11: identify ourselves, with a contact
    address, and do not pretend to be a browser -- see the Citadel finding.

    Workday hosts go through `urllib_transport`: since about 2026-11-14 Cloudflare
    challenges httpx on them and answers urllib, under the same User-Agent. The
    measurement and the line not to cross are in that module."""
    return httpx.Client(
        headers={"User-Agent": paths.USER_AGENT},
        timeout=paths.HTTP_TIMEOUT_SECONDS,
        follow_redirects=True,
        mounts={urllib_transport.WORKDAY_HOSTS: urllib_transport.UrllibTransport()},
    )
