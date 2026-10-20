"""Workday paging: a board that moves under the crawl is re-read, not refused."""
from __future__ import annotations

import json
import pathlib
import sys
import unittest

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from gather import ats  # noqa: E402

BASE = "https://example.wd5.myworkdayjobs.com/wday/cxs/example/external"


def board(*passes: int) -> tuple[httpx.Client, list[int]]:
    """A board stating `passes[i]` postings on pass i while serving one fewer, except
    on the last pass, which serves all it states. Returns the client and a log of the
    offsets requested."""
    offsets: list[int] = []
    state = {"pass": -1}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        offset = body["offset"]
        if offset == 0:
            state["pass"] += 1
        offsets.append(offset)
        stated = passes[min(state["pass"], len(passes) - 1)]
        served = stated if state["pass"] >= len(passes) - 1 else stated - 1
        jobs = [{"externalPath": f"/job/{i}", "title": f"Intern {i}"}
                for i in range(offset, min(offset + ats.WORKDAY_PAGE, served))]
        return httpx.Response(200, json={"total": stated if offset == 0 else 0,
                                         "jobPostings": jobs})

    return httpx.Client(transport=httpx.MockTransport(handler)), offsets


class WorkdayPagingTests(unittest.TestCase):
    def test_a_short_pass_is_reread_once_and_then_accepted(self):
        client, offsets = board(46, 45)
        fetched = ats._fetch_workday(client, BASE, lambda job: False)
        self.assertEqual(fetched.listed, 45)
        self.assertEqual(offsets.count(0), 2)
        self.assertEqual(fetched.attempt.requests, len(offsets))

    def test_a_board_short_twice_running_is_refused(self):
        client, _ = board(46, 46, 46)
        with self.assertRaisesRegex(RuntimeError, "could be paged"):
            ats._fetch_workday(client, BASE, lambda job: False)

    def test_a_complete_pass_is_not_reread(self):
        client, offsets = board(45)
        ats._fetch_workday(client, BASE, lambda job: False)
        self.assertEqual(offsets.count(0), 1)


if __name__ == "__main__":
    unittest.main()
