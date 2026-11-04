---
name: logging-that-helps
description: Log the identifiers needed to find an event later, at the right level, and never secrets
when: logging, logger, log message, log level, add logs, print statements, debug output, observability, log file
---
# Logging that helps

A log line is for the person debugging at 3am who cannot attach a debugger.

- **Use the logging module, not print.** `log = logging.getLogger(__name__)` at the top of the module. Configure handlers once,
  in the entry point, never inside a library.
- **Include the identifiers** needed to find the event: request id, user id, order id, file name, attempt number. A line that says
  "failed" with no id is useless.
- **Pick the level honestly.** DEBUG for detail, INFO for normal milestones, WARNING for something odd but handled, ERROR for a
  failed operation. Do not log every loop iteration at INFO.
- **Never log secrets.** No passwords, tokens, API keys, session cookies, full card numbers or whole request bodies that may
  contain them. Mask or omit.
- **Log an exception once, with its traceback**: `log.exception("charge failed order=%s", order_id)` inside the except block.
  Logging it again at every layer makes five copies of one error.
- **Use lazy formatting**: `log.info("saved %s rows", n)`, not an f-string, so disabled levels cost nothing.
- **Say what happened, not that code ran.** "retrying upload attempt=2 status=503" beats "in upload function".
