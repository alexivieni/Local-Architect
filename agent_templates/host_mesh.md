---
name: host_mesh
title: Other machines the person added, measured and ranked
purpose: Adds opt-in benchmarking, health checks with timeouts, ranking by measured speed and a fallback to local when a host is down, for hosts the person added; no network discovery.
why: Several machines can share the work only if the harness knows which ones answer and how fast, measured, never scanned for. Network failure modes and fallbacks need careful judgment.
# local_ok: no
# tier: heavy
mode: acceptEdits
model:
budget_usd: 3
files: arch_hosts.py, arch_selftest_mesh.py
tests: python arch_selftest_mesh.py
footer: common
source: brief_mesh.md
---
You are the multi-machine agent for "{app}". YOUR FILES: arch_hosts.py, NEW arch_selftest_mesh.py.
Read CLAUDE.md (hosts are machines the person added; loopback-only defaults; never scan networks) and arch_hosts.py.
Add bench_host(name) -> measured latency and tokens/s for a tiny prompt on that host's model (opt-in, only hosts the person added);
health(name) with timeouts; rank_hosts(role) by measured speed and health; a route preference so a role runs on the fastest healthy
host, falling back to local when a host is down (reuse the existing down_hosts logic); a plain-words status() for the hub.
Tests with a fake HTTP server on 127.0.0.1.
