"""A throttled source is asked again at the end of the run, and HEALTH prints a failure
shared by many sources as one line.

Both added 2026-11-16, after every Workday board failed together on 2026-11-14 (bodies
that were not JSON) and 2026-11-16 (HTTP 429), and read normally on 2026-11-15.
"""
from __future__ import annotations

import unittest
from unittest import mock

import httpx

from core import models
from deliver import health
from gather import ats, collect

WORKDAY = {"source_id": "acme-workday", "method": "workday",
           "url": "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/Campus"}
PAGE = {"source_id": "acme-page", "method": "page_text", "url": "https://acme.example/jobs"}

ERROR_429 = ("HTTPStatusError: Client error '429 Too Many Requests' for url "
             "'https://{slug}.wd1.myworkdayjobs.com/wday/cxs/{slug}/Campus/jobs'\n"
             "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/429")


def _attempt(source_id: str, ok: bool, status: int = 0, error: str = "") -> models.FetchAttempt:
    return models.FetchAttempt(source_id=source_id, ok=ok, status=status, error=error)


class RetryTests(unittest.TestCase):
    def setUp(self):
        for target in ("gather.collect.time.sleep", "gather.collect.artifacts.write_body"):
            patcher = mock.patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_only_come_back_later_is_worth_a_retry(self):
        self.assertEqual(collect.retry_reason(WORKDAY, _attempt("a", False, 429)), "HTTP 429")
        self.assertEqual(collect.retry_reason(WORKDAY, _attempt("a", False, 503)), "HTTP 503")
        self.assertEqual(collect.retry_reason(WORKDAY, _attempt(
            "a", False, error="JSONDecodeError: Expecting value: line 1 column 1 (char 0)")),
            "a response that was not JSON")
        self.assertEqual(collect.retry_reason(WORKDAY, _attempt("a", False, 403)), "",
                         "a refusal is an answer")
        self.assertEqual(collect.retry_reason(WORKDAY, _attempt("a", False, 404)), "")
        self.assertEqual(collect.retry_reason(PAGE, _attempt(
            "a", False, error="JSONDecodeError: x")), "", "a page is not a JSON API")
        self.assertEqual(collect.retry_reason(WORKDAY, models.FetchAttempt(
            source_id="a", ok=False, status=429, quarantined=True)), "")

    def test_a_throttled_source_that_reads_on_the_second_try_is_replaced_and_marked(self):
        attempts = [_attempt("acme-workday", False, 429, "429"), _attempt("acme-page", True)]
        with mock.patch.object(collect, "fetch_one",
                               return_value=(_attempt("acme-workday", True, 200), b"{}")) as fetch:
            collect.retry_refused([WORKDAY, PAGE], attempts, {}, lambda job: True)
        self.assertEqual(fetch.call_count, 1, "only the throttled source is asked again")
        self.assertTrue(attempts[0].ok)
        self.assertEqual(attempts[0].meta["retried"], "HTTP 429")
        self.assertTrue(attempts[1].ok)
        self.assertNotIn("retried", attempts[1].meta)

    def test_the_second_pass_gives_up_once_the_throttle_has_plainly_not_lifted(self):
        sources = [dict(WORKDAY, source_id=f"b{i}") for i in range(5)]
        attempts = [_attempt(f"b{i}", False, 429) for i in range(5)]
        with mock.patch.object(collect, "fetch_one",
                               side_effect=lambda s, *_: (_attempt(s["source_id"], False, 429), b"")
                               ) as fetch:
            collect.retry_refused(sources, attempts, {}, lambda job: True)
        self.assertEqual(fetch.call_count, collect.RETRY_GIVE_UP_AFTER)
        self.assertEqual(attempts[0].meta["retried"], "HTTP 429")
        self.assertNotIn("retried", attempts[4].meta, "never asked again, so not marked")

    def test_an_ats_failure_records_its_http_status(self):
        def refuse(request):
            return httpx.Response(429, json={})
        with httpx.Client(transport=httpx.MockTransport(refuse)) as client:
            fetched = ats.fetch(WORKDAY, client, wants_detail=lambda job: True)
        self.assertFalse(fetched.attempt.ok)
        self.assertEqual(fetched.attempt.status, 429)
        self.assertEqual(collect.retry_reason(WORKDAY, fetched.attempt), "HTTP 429")


class SharedFailureTests(unittest.TestCase):
    def _failing(self, n: int, count: str = "1", **extra):
        results, sources = [], {}
        for i in range(n):
            sid = f"firm{i}-workday"
            results.append(models.SourceMetrics(
                source_id=sid, ok=False, error=ERROR_429.format(slug=f"firm{i}"), extra=dict(extra)))
            sources[sid] = {"source_id": sid, "consecutive_failures": count,
                            "last_success": "2026-11-15T14:56:07+00:00"}
        return results, sources

    def test_one_cause_across_many_sources_prints_one_line_and_keeps_the_count(self):
        results, sources = self._failing(26)
        results.append(models.SourceMetrics(source_id="other", ok=False, error="ReadTimeout: x"))
        sources["other"] = {"consecutive_failures": "1", "last_success": "2026-11-15"}
        lines, escalated = health.source_lines(results, sources)
        self.assertIn("27 FAILING", lines[0], "grouping changes the wording, never the count")
        grouped = [line for line in lines if "same error" in line]
        self.assertEqual(len(grouped), 1)
        self.assertIn("26 sources failed with the same error, HTTP 429 Too Many Requests", grouped[0])
        self.assertIn("1 consecutive failure each, last success 2026-11-15", grouped[0])
        self.assertIn("firm0-workday", grouped[0])
        self.assertFalse(any(line.startswith("⚠ firm") for line in lines))
        self.assertTrue(any(line.startswith("⚠ other:") for line in lines))
        self.assertEqual(escalated, [])

    def test_a_few_failures_still_print_one_line_each(self):
        results, sources = self._failing(health.GROUP_FAILURES_AT - 1)
        lines, _ = health.source_lines(results, sources)
        self.assertFalse(any("same error" in line for line in lines))
        self.assertEqual(sum(line.startswith("⚠ firm") for line in lines),
                         health.GROUP_FAILURES_AT - 1)

    def test_a_blind_source_is_never_folded_into_a_group(self):
        results, sources = self._failing(5, count="3")
        lines, escalated = health.source_lines(results, sources)
        self.assertFalse(any("same error" in line for line in lines))
        self.assertEqual(len(escalated), 5, "each blind source keeps its own table row")

    def test_a_retry_is_reported_either_way(self):
        results, sources = self._failing(4, retried="HTTP 429")
        results.append(models.SourceMetrics(source_id="ok-workday", ok=True,
                                            extra={"retried": "HTTP 429"}))
        lines, _ = health.source_lines(results, sources)
        self.assertTrue(any("asked again at the end of the run and refused again" in line
                            for line in lines))
        self.assertIn("· 1 source(s) were refused with HTTP 429 on the first pass and read on "
                      "a second try at the end of the run: ok-workday.", lines)


if __name__ == "__main__":
    unittest.main()
