"""Retrieval benchmark: keyword-only (TF-IDF + FTS5, the real arch_index.hybrid) vs keyword + embedding (arch_embed) over
evals/retrieval/questions.json. Reports recall@1/@3/@8 and MRR, overall and by question type, with Wilson 95% intervals.

  py -3.12 tools/retrieval_bench.py --label keyword                 # keyword-only (CPU, no model)
  py -3.12 tools/retrieval_bench.py --label fake --fake             # + deterministic fake embedder (plumbing, no network)
  py -3.12 tools/retrieval_bench.py --label bge --embed bge-m3:latest   # + a real Ollama embedding model (keep_alive 0)
  py -3.12 tools/retrieval_bench.py --check                         # only validate the ground truth

Writes hub_data/evals/retrieval_<label>.json and .md. The index files live in a temp folder: nothing is written into the repo's .arch/.
"""
import argparse
import copy
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import arch_core          # noqa: E402
import arch_embed         # noqa: E402
import arch_fts           # noqa: E402
import arch_index         # noqa: E402
import arch_templates_catalog  # noqa: E402

QFILE = os.path.join(ROOT, "evals", "retrieval", "questions.json")
OWN = ("tools/retrieval_bench.py", "arch_selftest_retrieval.py", "arch_embed.py")
KS = (1, 3, 8)


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(max(0.0, c - h), 3), round(min(1.0, c + h), 3))


def load(path=QFILE):
    with open(path, encoding="utf-8") as f:
        return json.load(f)["questions"]


def cfg_default():
    return copy.deepcopy(arch_core.DEFAULTS)


def corpus_files(root, cfg, name):
    files = arch_core.list_files(root, cfg)
    if name == "architect":                     # the repo's own modules; selftests and this benchmark would echo the questions
        files = [f for f in files if "/" not in f and f.endswith(".py") and not f.startswith("arch_selftest") and f not in OWN]
    return files


def materialise(names, tmp):
    """-> {corpus: root}. Templates are rendered into tmp; 'architect' is this repo."""
    out = {}
    for n in sorted(set(names)):
        if n == "architect":
            out[n] = ROOT
        elif n.startswith("tpl:"):
            d = os.path.join(tmp, n[4:])
            arch_templates_catalog.render(n[4:], d, {"name": "app"}, overwrite=True)
            out[n] = d
    return out


def check(questions, roots, cfg):
    """Ground truth by script: every expected file exists in its corpus and matches its evidence regex. -> [problem]."""
    bad, ids = [], set()
    for q in questions:
        if q["id"] in ids:
            bad.append(f"{q['id']}: duplicate id")
        ids.add(q["id"])
        if q.get("type") not in ("identifier", "paraphrase"):
            bad.append(f"{q['id']}: bad type")
        root = roots.get(q["corpus"])
        if not root:
            bad.append(f"{q['id']}: unknown corpus {q['corpus']}")
            continue
        files = set(corpus_files(root, cfg, q["corpus"]))
        for e in q["expect"]:
            p = os.path.join(root, e)
            if e not in files or not os.path.isfile(p):
                bad.append(f"{q['id']}: {e} not in corpus")
                continue
            with open(p, encoding="utf-8", errors="replace") as f:
                if not re.search(q["evidence"], f.read()):
                    bad.append(f"{q['id']}: evidence /{q['evidence']}/ not in {e}")
        if q["type"] == "paraphrase":           # the identifier itself must not be in the question
            for ident in re.findall(r"[A-Za-z_]{4,}", q["evidence"]):
                if ident.lower() in ("def", "class") or ident in q.get("allow_words", ()):
                    continue
                if re.search(r"\b" + re.escape(ident.lower()) + r"\b", q["q"].lower()):
                    bad.append(f"{q['id']}: paraphrase repeats identifier {ident}")
    return bad


class Corpus:
    def __init__(self, name, root, cfg, tmp, embedder_factory=None):
        self.name, self.root, self.cfg = name, root, cfg
        t0 = time.time()
        self.files = corpus_files(root, cfg, name)
        self.chunks = arch_index.chunks_for(root, cfg, self.files, arch_core.read_text)
        self.index = arch_fts.Index(root)
        self.index.path = os.path.join(tmp, "idx_" + re.sub(r"\W", "_", name) + ".db")
        self.refresh = self.index.refresh(self.chunks, kind="code")
        self.index_secs = round(time.time() - t0, 2)
        self.embedder = embedder_factory(name) if embedder_factory else None
        self.embed_secs = None
        if self.embedder:
            t1 = time.time()
            ok = self.embedder.embed([c["text"] for c in self.chunks if c.get("kind", "code") == "code"])
            self.embed_secs = round(time.time() - t1, 2)
            if ok is None:
                self.embedder = None

    def rank(self, q, dense_on):
        tf = arch_core.rank_files(self.root, self.cfg, q, self.files)
        hits = self.index.search(q[:4000], top=24)
        dense = self.embedder.search(q, self.chunks, top=24) if (dense_on and self.embedder) else None
        fused, _ = arch_index.hybrid(hits, tf, dense)
        return fused


def score(rows):
    n = len(rows)
    out = {"n": n}
    for k in KS:
        hit = sum(1 for r in rows if r["rank"] and r["rank"] <= k)
        out[f"r@{k}"] = round(hit / n, 3) if n else 0.0
        out[f"r@{k}_ci"] = wilson(hit, n)
    out["mrr"] = round(sum((1.0 / r["rank"]) if r["rank"] else 0.0 for r in rows) / n, 3) if n else 0.0
    return out


def evaluate(questions, corpora, dense_on):
    rows = []
    for q in questions:
        c = corpora[q["corpus"]]
        order = c.rank(q["q"], dense_on)
        rank = next((i + 1 for i, f in enumerate(order) if f in q["expect"]), None)
        rows.append({"id": q["id"], "type": q["type"], "corpus": q["corpus"], "rank": rank, "top3": order[:3]})
    by = {"all": score(rows)}
    for t in ("identifier", "paraphrase"):
        by[t] = score([r for r in rows if r["type"] == t])
    return {"scores": by, "rows": rows}


def ollama_note(model):
    """Size of the embedding model as Ollama reports it (what it occupies while a batch runs; keep_alive 0 frees it after)."""
    post = arch_embed.ollama_post(arch_embed.DEFAULT_HOST, timeout=5)
    if not post:
        return None
    try:
        import urllib.request
        with urllib.request.urlopen(arch_embed.DEFAULT_HOST + "/api/tags", timeout=5) as r:
            tags = json.loads(r.read().decode())
        for m in tags.get("models", []):
            if m.get("name") == model or m.get("model") == model:
                gb = round(m.get("size", 0) / 1e9, 2)
                return f"{model}: {gb} GB on disk; roughly that much VRAM (or RAM) only while a batch runs - keep_alive 0 unloads it after each call"
        return f"{model}: not installed in Ollama"
    except Exception as e:
        return f"Ollama not answering ({type(e).__name__})"


def markdown(res):
    lines = [f"# Retrieval benchmark: {res['label']}", "", f"{res['questions']} questions; corpora: {', '.join(res['corpora'])}", ""]
    lines.append("| ranker | slice | n | recall@1 | recall@3 | recall@8 | MRR |")
    lines.append("|---|---|---|---|---|---|---|")
    for rk, ev in res["runs"].items():
        for sl, s in ev["scores"].items():
            cell = lambda k: f"{s[f'r@{k}']:.2f} [{s[f'r@{k}_ci'][0]:.2f}-{s[f'r@{k}_ci'][1]:.2f}]"
            lines.append(f"| {rk} | {sl} | {s['n']} | {cell(1)} | {cell(3)} | {cell(8)} | {s['mrr']:.3f} |")
    lines += ["", "Index time (s): " + ", ".join(f"{k} {v['index_secs']}" + (f" (+embed {v['embed_secs']})" if v.get("embed_secs") is not None else "") for k, v in res["timing"].items())]
    if res.get("embed_note"):
        lines += ["", "Embedding: " + res["embed_note"]]
    misses = [r for r in res["runs"]["keyword"]["rows"] if not r["rank"] or r["rank"] > 3]
    if misses:
        lines += ["", "Keyword misses (not in top 3): " + ", ".join(f"{r['id']}({r['rank'] or '-'})" for r in misses)]
    return "\n".join(lines) + "\n"


def run(label, embed=None, fake=False, out_dir=None, questions=None, say=print):
    questions = questions or load()
    cfg = cfg_default()
    tmp = tempfile.mkdtemp(prefix="retrbench_")
    try:
        roots = materialise([q["corpus"] for q in questions], tmp)
        bad = check(questions, roots, cfg)
        if bad:
            raise SystemExit("ground truth invalid:\n  " + "\n  ".join(bad))
        factory, note = None, None
        if fake:
            post = arch_embed.fake_post()
            factory = lambda n: arch_embed.Embedder(tmp, "fake-embed", post=post, db_path=os.path.join(tmp, "embed.db"))
            note = "deterministic fake embedder (hashed words + trigrams), plumbing only"
        elif embed:
            cache = os.path.join(ROOT, "hub_data", "evals", "retrieval_embed.db")
            factory = lambda n: arch_embed.Embedder(tmp, embed, db_path=cache)
            note = ollama_note(embed)
        corpora = {n: Corpus(n, r, cfg, tmp, factory) for n, r in roots.items()}
        res = {"label": label, "when": time.strftime("%Y-%m-%d %H:%M:%S"), "questions": len(questions), "corpora": sorted(roots),
               "fts5": arch_fts.available(), "embed_model": "fake-embed" if fake else embed, "embed_note": note,
               "timing": {n: {"index_secs": c.index_secs, "embed_secs": c.embed_secs, "files": len(c.files), "chunks": len(c.chunks)} for n, c in corpora.items()},
               "runs": {"keyword": evaluate(questions, corpora, False)}}
        if factory:
            if all(c.embedder is None for c in corpora.values()):
                res["embed_note"] = (note or "") + " | embedding failed for every corpus: keyword-only"
            else:
                res["runs"]["keyword+embed"] = evaluate(questions, corpora, True)
        out_dir = out_dir or os.path.join(ROOT, "hub_data", "evals")
        os.makedirs(out_dir, exist_ok=True)
        base = os.path.join(out_dir, f"retrieval_{label}")
        with open(base + ".json", "w", encoding="utf-8") as f:
            json.dump(res, f, indent=1)
        md = markdown(res)
        with open(base + ".md", "w", encoding="utf-8") as f:
            f.write(md)
        say(md)
        say(f"wrote {base}.json and .md")
        return res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="keyword")
    ap.add_argument("--embed", default="", help="Ollama embedding model, e.g. bge-m3:latest")
    ap.add_argument("--fake", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    if a.check:
        tmp = tempfile.mkdtemp(prefix="retrcheck_")
        try:
            qs = load()
            bad = check(qs, materialise([q["corpus"] for q in qs], tmp), cfg_default())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        print("\n".join(bad) if bad else f"ground truth ok ({len(qs)} questions)")
        return 1 if bad else 0
    run(a.label, embed=a.embed or None, fake=a.fake, out_dir=a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
