"""config: a Dockerfile (or CI yaml) with hardening findings; the same scanner re-run must report none at medium or above."""
import _bank as B

FLOW = "config"
DEFAULT_N = 6
GOOD_DOCKER = ("FROM python:3.12-slim@sha256:0000000000000000000000000000000000000000000000000000000000000000\n"
               "WORKDIR /app\nCOPY . /app\nRUN pip install --no-cache-dir -r requirements.txt\nRUN useradd -m app\nUSER app\n"
               "HEALTHCHECK CMD python -c \"import socket; socket.create_connection(('127.0.0.1', 8000), 2)\"\nCMD [\"python\", \"app.py\"]\n")
CASES = [
    ("unpinned-root", "FROM python\nWORKDIR /app\nCOPY . /app\nRUN pip install -r requirements.txt\nCMD [\"python\", \"app.py\"]\n"),
    ("no-healthcheck", "FROM python:3.12-slim\nWORKDIR /app\nCOPY . /app\nRUN useradd -m app\nUSER app\nCMD [\"python\", \"app.py\"]\n"),
    ("curl-pipe-sh", "FROM python:3.12-slim\nRUN curl -sSL https://example.com/install.sh | sh\nWORKDIR /app\nCOPY . /app\nRUN useradd -m app\nUSER app\nHEALTHCHECK CMD true\nCMD [\"python\", \"app.py\"]\n"),
    ("baked-secret", "FROM python:3.12-slim\nENV PASSWORD=abcdef0123456789abcdef\nWORKDIR /app\nCOPY . /app\nRUN useradd -m app\nUSER app\nHEALTHCHECK CMD true\nCMD [\"python\", \"app.py\"]\n"),
    ("root-and-no-health", "FROM python:3.12\nWORKDIR /app\nCOPY . /app\nCMD [\"python\", \"app.py\"]\n"),
    ("latest-tag-root", "FROM python:latest\nWORKDIR /app\nCOPY . /app\nCMD [\"python\", \"app.py\"]\n"),
]


def generate(n, seed=0):
    rng = B.rng_for(seed, "config")
    out = []
    for i, (name, bad) in enumerate(B.pick(rng, CASES, n)):
        out.append({"id": f"config-{name}-{i:02d}", "tags": ["config"], "goal": f"Harden the Dockerfile ({name}): fix every hardening finding the scanner reports.",
                    "fixture": {"files": {"Dockerfile": bad, "app.py": "print('hi')\n", "requirements.txt": ""}},
                    "budget": {"calls": 12, "tokens": 40000, "seconds": 600},
                    "judge": [{"type": "scanner_zero", "kind": "config", "severity": "medium", "files": ["Dockerfile"]}, {"type": "file_contains", "path": "Dockerfile", "needles": ["USER ", "HEALTHCHECK"]}],
                    "solution": {"files": {"Dockerfile": GOOD_DOCKER}}, "wrong": {"files": {"Dockerfile": bad}}})
    return out
