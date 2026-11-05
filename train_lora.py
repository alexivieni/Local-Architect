"""QLoRA fine-tune of a small dense model on the examples `python architect.py dataset` exports.

*** The training part is UNTESTED HERE: the machine this was written on has no GPU free for it. Read it before you run it. ***
*** The curation part (what goes in, in what order) is pure Python and is proven by arch_selftest_tune2.py. ***

    python train_lora.py --check                       # are the packages / GPU there? (no download)
    python train_lora.py --dry-run                     # what it WOULD train on, in what order, and why things were left out (no GPU)
    python train_lora.py                               # train on <data dir>/datasets/tier-worker.sft.jsonl
    python train_lora.py --min-quality 0.6 --curriculum --weight repeat
    python train_lora.py --base unsloth/Qwen3-4B-Instruct-2507 --epochs 2
    python train_lora.py --gguf                        # also write a GGUF + Modelfile for `ollama create`
    python train_lora.py --export-only                 # GGUF + Modelfile from an adapter trained earlier (no training)

What goes in (`curate`):
  * only samples whose code checks passed -- that is all the dataset export writes;
  * never a sample a cloud model made or taught (meta.by_claude, a transport of "claude", a teacher named claude). The export already
    drops those; this is the second lock on the same door;
  * `--min-quality`: samples with a quality score below it are left out. Files from an older export have no score: they are kept
    and counted as "unscored";
  * `--weight repeat`: a high-quality sample (>= 0.8) is shown twice per epoch. The trainer has no per-sample weight, so repetition is it;
  * `--curriculum`: easy -> medium -> hard (meta.difficulty.level or a level-* tag), best quality first inside a level, and the trainer
    keeps that order instead of shuffling.
The chosen samples are written to <out>/train_set.jsonl (messages only) and that is what is trained on, so the set is inspectable.

Hardware: a 4B model in 4-bit with LoRA, batch 1, 2048-token examples is meant for ~6 GB VRAM
(community QLoRA reports: ~4 GB for 3B, 6-10 GB for 7-8B). Close other GPU programs and stop the model servers first
(`python architect.py server stop`, `ollama stop <model>`). If you hit out-of-memory: lower --max-seq, then --rank.

Needs (install once, internet): pip install unsloth datasets trl  (Unsloth pulls torch with CUDA).
On Windows, Unsloth works best through WSL2.

Expect: this teaches the model YOUR project's conventions and the agent's reply format. It does not add general intelligence.
Prove it with `eval --compare baseline` and switch over only with `promote-model`; keep the old model.
After a real run, <out>/train_stats.json holds the measured training speed, which `arch_autotune.tune_plan` uses for its time estimate.
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("ARCHITECT_DATA_DIR") or os.path.join(HERE, "hub_data")
LEVELS = {"easy": 0, "medium": 1, "hard": 2}
HIGH_QUALITY = 0.8
MIN_SAMPLES = 50
CHARS_PER_TOKEN = 3.5          # a rough English/code average; only used for estimates


def check():
    ok = True
    for mod in ("torch", "unsloth", "datasets", "trl"):
        try:
            __import__(mod)
            print(f"  ok       {mod}")
        except Exception as e:
            ok = False
            print(f"  MISSING  {mod}   ({type(e).__name__})")
    try:
        import torch
        if torch.cuda.is_available():
            p = torch.cuda.get_device_properties(0)
            print(f"  GPU      {p.name}, {p.total_memory / 2**30:.1f} GB")
        else:
            ok = False
            print("  GPU      none visible to torch (training would be far too slow on CPU)")
    except Exception as e:                  # torch present but broken (driver, CUDA): say so instead of reporting a clean check
        ok = False
        print(f"  GPU      could not be checked ({type(e).__name__}: {e})")
    return 0 if ok else 1


# ------------------------------------------------------------------ curation (no GPU, no third-party imports)

def row_model(row):
    meta = row.get("meta") or {}
    return meta.get("for_model") or meta.get("model") or None


def pick_model(rows, model=None):
    """An adapter fits ONE base. -> (rows for that model, error or None). With `model`, keep only its rows; without it, refuse a file
    whose rows come from more than one model (rows with no model recorded are counted as their own 'unknown' model)."""
    models = sorted({str(row_model(r) or "unknown") for r in rows})
    if model:
        kept = [r for r in rows if row_model(r) == model]
        return kept, (None if kept else f"no samples for model {model!r} (the file has: {', '.join(models) or 'none'})")
    if len(models) > 1:
        return rows, ("this file mixes models (" + ", ".join(models) + "); pass --model <the worker you will put the adapter on> "
                      "or use its own tier-<tier>.<model>.sft.jsonl")
    return rows, None


def base_error(model, base):
    """Refuse a --base that does not fit the served model (arch_dataset.base_matches). Unknown model -> no refusal, a warning upstream."""
    if not model:
        return None
    try:
        sys.path.insert(0, HERE)
        import arch_dataset
    except Exception:
        return None
    ok = arch_dataset.base_matches(model, base)
    if ok is False:
        return f"--base {base} does not match {model} (expected {arch_dataset.base_for(model)}); an adapter only fits its own base"
    return None


def made_by_claude(row):
    """True for a sample a cloud model wrote or taught. Looks at every place an export may record it."""
    meta = row.get("meta") or {}
    for d in (row, meta):
        if d.get("by_claude") or str(d.get("transport") or "").lower() == "claude" or str(d.get("source") or "").lower() == "claude":
            return True
    for k in ("taught_by", "model", "chosen_model"):
        if "claude" in str(meta.get(k) or "").lower():
            return True
    return False


def level_of(row):
    meta = row.get("meta") or {}
    lv = (meta.get("difficulty") or {}).get("level") if isinstance(meta.get("difficulty"), dict) else None
    if not lv:
        lv = next((t[6:] for t in (meta.get("tags") or []) if isinstance(t, str) and t.startswith("level-")), None)
    return lv if lv in LEVELS else None


def quality_of(row):
    q = (row.get("meta") or {}).get("quality")
    return float(q) if isinstance(q, (int, float)) else None


def read_jsonl(path):
    rows, broken = [], 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                broken += 1
    return rows, broken


def curate(rows, min_quality=0.0, curriculum=False, weight="none"):
    """-> {"rows": [kept samples, in training order], "dropped": {reason: n}, "unscored": n, "repeated": n, "by_level": {...}, "tokens": n}.
    Pure: the same input always gives the same output."""
    dropped = {"claude": 0, "low_quality": 0, "no_reply": 0}
    kept, unscored = [], 0
    for i, r in enumerate(rows):
        msgs = r.get("messages") or []
        if not any(m.get("role") == "assistant" and (m.get("content") or "").strip() for m in msgs):
            dropped["no_reply"] += 1
            continue
        if made_by_claude(r):
            dropped["claude"] += 1
            continue
        q = quality_of(r)
        if q is None:
            unscored += 1
        elif q < min_quality:
            dropped["low_quality"] += 1
            continue
        kept.append((i, r))
    if curriculum:          # easy first; unknown level after hard; inside a level the best first; then the file's own order
        kept.sort(key=lambda ir: (LEVELS.get(level_of(ir[1]), 3), -(quality_of(ir[1]) or 0.0), ir[0]))
    out, repeated = [], 0
    for _i, r in kept:
        out.append(r)
        if weight == "repeat" and (quality_of(r) or 0.0) >= HIGH_QUALITY:
            if curriculum:
                out.append(r)              # beside itself, so the curriculum order holds
            repeated += 1
    if weight == "repeat" and not curriculum:
        out.extend(r for _i, r in kept if (quality_of(r) or 0.0) >= HIGH_QUALITY)
    by_level = {}
    for r in out:
        k = level_of(r) or "unknown"
        by_level[k] = by_level.get(k, 0) + 1
    chars = sum(len(m.get("content") or "") for r in out for m in (r.get("messages") or []))
    return {"rows": out, "dropped": dropped, "unscored": unscored, "repeated": repeated, "by_level": by_level,
            "tokens": int(chars / CHARS_PER_TOKEN), "unique": len(kept)}


def describe(cur, path, broken=0, epochs=2.0, stats=None):
    """The dry run in plain words. -> list of lines."""
    lines = [f"From {path}:"]
    lines.append(f"  {cur['unique']} sample(s) would be trained on" +
                 (f", {cur['repeated']} of them shown twice (quality >= {HIGH_QUALITY})" if cur["repeated"] else "") +
                 f": {len(cur['rows'])} rows per epoch, about {cur['tokens']} tokens (estimate, {CHARS_PER_TOKEN} characters per token).")
    if cur["by_level"]:
        lines.append("  by difficulty: " + ", ".join(f"{k} {n}" for k, n in sorted(cur["by_level"].items(), key=lambda kv: LEVELS.get(kv[0], 3))))
    why = {"claude": "made or taught by a cloud model (never trained on)", "low_quality": "below the quality floor",
           "no_reply": "no assistant reply"}
    for k, n in cur["dropped"].items():
        if n:
            lines.append(f"  left out {n}: {why[k]}")
    if broken:
        lines.append(f"  left out {broken}: lines that are not valid JSON")
    if cur["unscored"]:
        lines.append(f"  {cur['unscored']} sample(s) carry no quality score (an older export): kept. Re-run `python architect.py dataset` to score them.")
    tps = (stats or {}).get("tok_per_s")
    if tps:
        mins = cur["tokens"] * epochs / tps / 60
        lines.append(f"  time: about {mins:.0f} min for {epochs:g} epoch(s) at {tps:.0f} tokens/s (measured by the last training run here).")
    if cur["unique"] < MIN_SAMPLES:
        lines.append(f"  A real run would refuse: fewer than {MIN_SAMPLES} samples is not worth training on yet.")
    for r in cur["rows"][:3]:
        user = next((m.get("content") or "" for m in r.get("messages") or [] if m.get("role") == "user"), "")
        meta = r.get("meta") or {}
        lines.append(f"    first: [{level_of(r) or '?'} q={quality_of(r) if quality_of(r) is not None else '-'}] "
                     f"{meta.get('flow') or '?'}/{meta.get('role') or '?'}: {' '.join(user.split())[:80]}")
    return lines


def load_stats(out_dir):
    try:
        with open(os.path.join(out_dir, "train_stats.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_train_set(rows, out_dir):
    """Only the messages go in: the meta dicts differ from row to row and would trip the dataset loader's schema."""
    os.makedirs(out_dir, exist_ok=True)
    p = os.path.join(out_dir, "train_set.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({"messages": r["messages"]}, ensure_ascii=False) + "\n")
    return p


# ------------------------------------------------------------------ GPU part

def _write_modelfile(out_dir, name="my-worker"):
    gguf_dir = os.path.join(out_dir, "gguf")
    files = [f for f in os.listdir(gguf_dir) if f.endswith(".gguf")] if os.path.isdir(gguf_dir) else []
    if not files:
        print("No .gguf file was written; see the messages above.")
        return 1
    mf = os.path.join(out_dir, "Modelfile")
    with open(mf, "w", encoding="utf-8") as f:
        f.write(f"FROM ./gguf/{sorted(files)[0]}\nPARAMETER temperature 0.2\nPARAMETER num_ctx 8192\n")
    print(f"Then:  cd {out_dir}  &&  ollama create {name} -f Modelfile")
    return 0


def export_only(a):
    adapter = os.path.join(a.out, "adapter")
    if not os.path.isdir(adapter):
        print(f"No adapter at {adapter}. Train first:  python train_lora.py")
        return 1
    if check() != 0:
        return 1
    from unsloth import FastLanguageModel
    model, tok = FastLanguageModel.from_pretrained(adapter, max_seq_length=a.max_seq, load_in_4bit=True)
    model.save_pretrained_gguf(os.path.join(a.out, "gguf"), tok, quantization_method="q4_k_m")
    return _write_modelfile(a.out, a.name)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=os.path.join(DATA_DIR, "datasets", "tier-worker.sft.jsonl"))
    ap.add_argument("--base", default="unsloth/Qwen3-4B-Instruct-2507")
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "lora_out"))
    ap.add_argument("--name", default="my-worker", help="the model name the Modelfile hint uses")
    ap.add_argument("--epochs", type=float, default=2.0)
    ap.add_argument("--max-seq", type=int, default=2048)
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--min-quality", type=float, default=0.0, help="leave out samples whose quality score is below this (0-1)")
    ap.add_argument("--curriculum", action="store_true", help="easy -> hard order, kept through training (no shuffling)")
    ap.add_argument("--weight", choices=["none", "repeat"], default="none", help="repeat = show quality >= 0.8 samples twice")
    ap.add_argument("--dry-run", action="store_true", help="print what would be trained on; no GPU, no packages needed")
    ap.add_argument("--gguf", action="store_true", help="export a q4_k_m GGUF and write an Ollama Modelfile")
    ap.add_argument("--export-only", action="store_true", help="GGUF + Modelfile from <out>/adapter; no training")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--model", default=None, help="train only on this served model's samples (the worker the adapter is for)")
    ap.add_argument("--force-base", action="store_true", help="train even when --base does not match --model")
    a = ap.parse_args(argv)
    if a.check:
        return check()
    if a.export_only:
        return export_only(a)
    if not os.path.exists(a.data):
        print(f"No training file at {a.data}. Run:  python architect.py dataset")
        return 1
    rows, broken = read_jsonl(a.data)
    rows, err = pick_model(rows, a.model)
    if err:
        print("Refused: " + err)
        return 2
    mdl = a.model or (str(row_model(rows[0])) if rows and row_model(rows[0]) else None)
    berr = base_error(mdl, a.base)
    if berr and not a.force_base:
        print("Refused: " + berr)
        return 2
    cur = curate(rows, a.min_quality, a.curriculum, a.weight)
    if a.dry_run:
        print("\n".join(describe(cur, a.data, broken, a.epochs, load_stats(a.out))))
        print("(dry run: nothing was trained, nothing was written)")
        return 0
    if cur["unique"] < MIN_SAMPLES:
        print(f"Only {cur['unique']} usable examples. That is too few to be worth training on; collect more approved runs first.")
        return 1
    if check() != 0:
        print("\nFix the items above first (see the top of this file).")
        return 1
    train_path = write_train_set(cur["rows"], a.out)

    from unsloth import FastLanguageModel
    from datasets import load_dataset
    from trl import SFTConfig, SFTTrainer

    model, tok = FastLanguageModel.from_pretrained(a.base, max_seq_length=a.max_seq, load_in_4bit=True)
    model = FastLanguageModel.get_peft_model(
        model, r=a.rank, lora_alpha=a.rank * 2, lora_dropout=0.0,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
        use_gradient_checkpointing="unsloth")
    ds = load_dataset("json", data_files=train_path, split="train")
    ds = ds.map(lambda r: {"text": tok.apply_chat_template(r["messages"], tokenize=False)})
    trainer_cls = SFTTrainer
    if a.curriculum:                       # the default sampler shuffles, which would undo the easy -> hard order
        from torch.utils.data import SequentialSampler

        class InOrder(SFTTrainer):
            def _get_train_sampler(self, *args, **kwargs):
                return SequentialSampler(self.train_dataset)
        trainer_cls = InOrder
    trainer = trainer_cls(model=model, tokenizer=tok, train_dataset=ds, args=SFTConfig(
        dataset_text_field="text", max_seq_length=a.max_seq, per_device_train_batch_size=1,
        gradient_accumulation_steps=8, num_train_epochs=a.epochs, learning_rate=a.lr,
        warmup_ratio=0.05, lr_scheduler_type="cosine", logging_steps=5, output_dir=a.out,
        optim="adamw_8bit", seed=3407, report_to="none"))
    t0 = time.time()
    trainer.train()
    secs = time.time() - t0
    model.save_pretrained(os.path.join(a.out, "adapter"))
    tok.save_pretrained(os.path.join(a.out, "adapter"))
    print("Adapter saved to", os.path.join(a.out, "adapter"))
    with open(os.path.join(a.out, "train_stats.json"), "w", encoding="utf-8") as f:     # measured here: the next plan's time estimate
        json.dump({"ts": time.time(), "base": a.base, "samples": len(cur["rows"]), "tokens": cur["tokens"], "epochs": a.epochs,
                   "seconds": round(secs, 1), "tok_per_s": round(cur["tokens"] * a.epochs / max(secs, 1.0), 1),
                   "max_seq": a.max_seq, "rank": a.rank, "min_quality": a.min_quality, "curriculum": a.curriculum}, f, indent=1)
    if a.gguf:
        model.save_pretrained_gguf(os.path.join(a.out, "gguf"), tok, quantization_method="q4_k_m")
        return _write_modelfile(a.out, a.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
