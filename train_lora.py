"""QLoRA fine-tune of a small dense model on the examples `python architect.py dataset` exports.

*** UNTESTED HERE: the machine this was written on has no GPU. Read it before you run it. ***

    python train_lora.py --check                       # are the packages / GPU there? (no download)
    python train_lora.py                               # train on hub_data/datasets/tier-worker.sft.jsonl
    python train_lora.py --base unsloth/Qwen3-4B-Instruct-2507 --epochs 2
    python train_lora.py --gguf                        # also write a GGUF + Modelfile for `ollama create`

Hardware: a 4B model in 4-bit with LoRA, batch 1, 2048-token examples is meant for ~6 GB VRAM
(community QLoRA reports: ~4 GB for 3B, 6-10 GB for 7-8B). Close other GPU programs and stop Ollama's
loaded model first (`ollama stop <model>`). If you hit out-of-memory: lower --max-seq, then --rank.

Needs (install once, internet): pip install unsloth datasets trl  (Unsloth pulls torch with CUDA).
On Windows, Unsloth works best through WSL2; if installation fights you, Google Colab's free T4 can run
this same script on the exported tier-worker.sft.jsonl, and you bring the GGUF home.

Expect: this teaches the model YOUR project's conventions and the agent's reply format. It does not add
general intelligence. Always compare before and after with the hub's Scoreboard, and keep the old model.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


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


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=os.path.join(HERE, "hub_data", "datasets", "tier-worker.sft.jsonl"))
    ap.add_argument("--base", default="unsloth/Qwen3-4B-Instruct-2507")
    ap.add_argument("--out", default=os.path.join(HERE, "hub_data", "lora_out"))
    ap.add_argument("--epochs", type=float, default=2.0)
    ap.add_argument("--max-seq", type=int, default=2048)
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--gguf", action="store_true", help="export a q4_k_m GGUF and write an Ollama Modelfile")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    if a.check:
        return check()
    if not os.path.exists(a.data):
        print(f"No training file at {a.data}. Run:  python architect.py dataset")
        return 1
    n = sum(1 for _ in open(a.data, encoding="utf-8"))
    if n < 50:
        print(f"Only {n} examples. That is too few to be worth training on; collect more approved runs first.")
        return 1
    if check() != 0:
        print("\nFix the items above first (see the top of this file).")
        return 1

    from unsloth import FastLanguageModel
    from datasets import load_dataset
    from trl import SFTConfig, SFTTrainer

    model, tok = FastLanguageModel.from_pretrained(a.base, max_seq_length=a.max_seq, load_in_4bit=True)
    model = FastLanguageModel.get_peft_model(
        model, r=a.rank, lora_alpha=a.rank * 2, lora_dropout=0.0,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
        use_gradient_checkpointing="unsloth")
    ds = load_dataset("json", data_files=a.data, split="train")
    ds = ds.map(lambda r: {"text": tok.apply_chat_template(r["messages"], tokenize=False)})
    trainer = SFTTrainer(model=model, tokenizer=tok, train_dataset=ds, args=SFTConfig(
        dataset_text_field="text", max_seq_length=a.max_seq, per_device_train_batch_size=1,
        gradient_accumulation_steps=8, num_train_epochs=a.epochs, learning_rate=a.lr,
        warmup_ratio=0.05, lr_scheduler_type="cosine", logging_steps=5, output_dir=a.out,
        optim="adamw_8bit", seed=3407, report_to="none"))
    trainer.train()
    model.save_pretrained(os.path.join(a.out, "adapter"))
    tok.save_pretrained(os.path.join(a.out, "adapter"))
    print("Adapter saved to", os.path.join(a.out, "adapter"))
    if a.gguf:
        model.save_pretrained_gguf(os.path.join(a.out, "gguf"), tok, quantization_method="q4_k_m")
        gguf_dir = os.path.join(a.out, "gguf")
        files = [f for f in os.listdir(gguf_dir) if f.endswith(".gguf")] if os.path.isdir(gguf_dir) else []
        if files:
            mf = os.path.join(a.out, "Modelfile")
            with open(mf, "w", encoding="utf-8") as f:
                f.write(f"FROM ./gguf/{files[0]}\nPARAMETER temperature 0.2\nPARAMETER num_ctx 8192\n")
            print(f"Then:  cd {a.out}  &&  ollama create my-dev-4b -f Modelfile")
    return 0


if __name__ == "__main__":
    sys.exit(main())
