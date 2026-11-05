"""Copy everything worth learning from to the archive drive (Settings > storage.archive, e.g. the 32 GB SD card), in one clear layout:

  <drive>\\Architect\\learning\\
      conversations\\   Claude Code session transcripts (agents + the lead session), scrubbed of secrets
      agents\\          every agent's event log, brief and final report; ledger.jsonl (cost per agent)
      decisions\\       reasoning_corpus.jsonl (plans, decisions with reasons, observations, reports), LESSONS.md, METHOD.md
      knowledge\\       logbook.db (searchable), playbooks.md, skills\\, docs\\ (synthesis, reviews, plans)
      runs\\            the app's own run archives (already written by storage.archive)
      README.txt       what each folder is for and what may be used for training (local verified work only)

    python tools/sync_learning.py            # copy what changed
    python tools/sync_learning.py --dry-run  # say what it would copy
Only copies (never deletes on either side). Secrets are scrubbed on the way (arch_redact)."""
import glob
import os
import shutil
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)
import arch_core as core  # noqa: E402

DRY = "--dry-run" in sys.argv
LEAD = os.path.join(os.path.expanduser("~"), ".claude", "projects", "C--Users-alexi-Downloads-architect-v12")


def main():
    cfg = core.load_config()
    drive = ((cfg.get("storage") or {}).get("archive") or "").strip()
    if not drive or not os.path.isdir(drive):
        print("No archive drive set or present (Settings > storage.archive)."); return 1
    base = os.path.join(drive, "Architect", "learning")
    data = core.data_dir()
    try:
        import arch_redact
        scrub = arch_redact.scrub
    except Exception:
        print("arch_redact missing: nothing copied (a secret must never land unscrubbed)."); return 1
    plan = []
    for pat, sub in ((os.path.join(data, "claude_code", "projects", "**", "*.jsonl"), "conversations/agents"),
                     (os.path.join(LEAD, "*.jsonl"), "conversations/lead"),
                     (os.path.join(data, "learning", "agent_logs", "*.log"), "agents/logs"),
                     (os.path.join(data, "learning", "briefs", "*"), "agents/briefs"),
                     (os.path.join(data, "learning", "ledger.jsonl"), "agents"),
                     (os.path.join(data, "learning", "reasoning_corpus.jsonl"), "decisions"),
                     (os.path.join(data, "learning", "LESSONS.md"), "decisions"),
                     (os.path.join(data, "learning", "METHOD.md"), "decisions"),
                     (os.path.join(data, "logbook.db"), "knowledge"),
                     (os.path.join(data, "memory", "*.md"), "knowledge/memory"),
                     (os.path.join(APP, "skills", "*.md"), "knowledge/skills"),
                     (os.path.join(APP, "docs", "*.md"), "knowledge/docs")):
        for src in glob.glob(pat, recursive=True):
            if os.path.isfile(src):
                dst = os.path.join(base, sub, os.path.basename(src))
                if not os.path.exists(dst) or os.path.getsize(dst) != os.path.getsize(src) or os.path.getmtime(src) > os.path.getmtime(dst):
                    plan.append((src, dst))
    size = sum(os.path.getsize(s) for s, _ in plan)
    print(f"{len(plan)} file(s), {size / 1e6:.1f} MB -> {base}")
    if DRY:
        return 0
    for src, dst in plan:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if src.endswith((".db",)):
            shutil.copy2(src, dst)
        else:
            text = open(src, "rb").read().decode("utf-8", errors="surrogateescape")
            open(dst, "wb").write(scrub(text).encode("utf-8", errors="surrogateescape"))
    with open(os.path.join(base, "README.txt"), "w", encoding="utf-8") as f:
        f.write(__doc__ + "\nTraining rule: only the app's own verified LOCAL work trains model weights. Claude-written content "
                "(conversations, agent logs, decisions) is for analysis, playbooks, retrieval and harness design.\n")
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
