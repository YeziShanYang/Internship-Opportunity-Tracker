"""Level 7: rendering, the HEALTH strings, urgency, the Issue POST and the email.

Every string the owner reads is composed here, and nothing else composes one. That is
the rule the digest kept breaking: `classify.screen_line()` and `classify.usage_line()`
emitted markdown from a stage forbidden to emit markdown, *and* read module globals to
do it -- which is concretely why `run.py render` could not run on its own. Both are
functions over data here now.

Along with `gather` and `enrich`, one of only three packages permitted to import
`httpx`. That is not an exception grudgingly made: `deliver.issue` POSTs the issue and
asks GitHub whether today's has already been opened, and the second of those is half of
the exactly-once guarantee. `deliver.mail` POSTs the untrimmed digest to an email API.
"""
