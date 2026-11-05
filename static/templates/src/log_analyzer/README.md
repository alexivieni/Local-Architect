# __NAME__

Turns web-server access logs into a short report.

    python -m __PKG__ samples/access.log
    python -m __PKG__ /var/log/nginx/access.log.1.gz --json --top 5
    python -m pytest -q

Accepted format: Common/Combined Log Format, optionally followed by a request time in seconds (nginx
`$request_time`). Lines that do not parse are counted, never guessed at.

## Layout

    __PKG__/parse.py    one regex, one dataclass, open plain or gzip files
    __PKG__/stats.py    status classes, top-N, nearest-rank percentiles, spikes
    __PKG__/__main__.py text / JSON output
