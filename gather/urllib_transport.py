"""An httpx transport that sends each request through the standard library's urllib.

Mounted on the web client for Workday hosts only (`*.myworkdayjobs.com`). From about
2026-11-14, Cloudflare in front of Workday began answering httpx with HTTP 429 and a
"Security Check: Enable JavaScript and cookies" page. Measured from the Actions runner
on 2026-11-19, with the same `paths.USER_AGENT`, the same headers and the first page of
each board one second apart:

    client    boards answering 200    posting details answering 200
    httpx     8 of 27                 0 of 6
    urllib    27 of 27                6 of 6
    curl      27 of 27                --

So the check is not a rate limit, and the end-of-run retry in `gather.collect` could
never clear it. It does not refuse automated clients as such either: urllib and curl
both announce themselves exactly as httpx does and are answered. Nothing about the
client is misrepresented here. The User-Agent is still the tracker's own, and no browser
headers or cookies are added. If Workday starts refusing urllib as well, that is a
refusal, and the boards go to `method=manual` like Citadel rather than being worked
around further.

Behaviour the callers depend on is kept:
- An HTTP error status comes back as a response, so `raise_for_status()` and the
  429/503 retry logic read it as before.
- A redirect comes back as a response too, so httpx follows it and `response.url` is
  the final URL. `process.redirect` reads that URL.
- A network failure raises `httpx.ConnectError` or `httpx.ReadTimeout`, so
  `except httpx.HTTPError` still catches it.
"""
from __future__ import annotations

import socket
import urllib.error
import urllib.request

import httpx

from core import paths

# Workday's hosts. Matches every tenant (`cibc.wd3.myworkdayjobs.com`) and not the bare
# domain, which serves no boards.
WORKDAY_HOSTS = "all://*.myworkdayjobs.com"

# Headers urllib sets itself. Sending httpx's values for these instead (keep-alive,
# gzip) gives up the request shape that was measured to pass.
_SET_BY_URLLIB = {"host", "connection", "accept-encoding", "content-length"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Return a redirect as a response, so that httpx follows it and records it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class UrllibTransport(httpx.BaseTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        headers = {name: value for name, value in request.headers.items()
                   if name.lower() not in _SET_BY_URLLIB}
        body = request.read() or None
        timeout = (request.extensions.get("timeout") or {}).get("read")
        outgoing = urllib.request.Request(
            str(request.url), data=body, headers=headers, method=request.method)
        try:
            response = _OPENER.open(
                outgoing, timeout=timeout or paths.HTTP_TIMEOUT_SECONDS)
        except urllib.error.HTTPError as error:
            response = error  # an error status is still a response
        except (TimeoutError, socket.timeout) as exc:
            raise httpx.ReadTimeout(str(exc), request=request) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise httpx.ReadTimeout(str(exc.reason), request=request) from exc
            raise httpx.ConnectError(str(exc.reason), request=request) from exc
        except OSError as exc:
            raise httpx.ConnectError(str(exc), request=request) from exc

        with response:
            content = response.read()
            status = getattr(response, "status", None) or response.code
            # The body is already de-chunked, so a Transfer-Encoding header would be
            # untrue. Content-Encoding is kept, and httpx decodes the body by it.
            response_headers = [(name, value) for name, value in response.headers.items()
                                if name.lower() != "transfer-encoding"]
        return httpx.Response(
            status, headers=response_headers, content=content, request=request)
