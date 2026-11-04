# What limits local model work on this laptop (audit of 2026-11-03)

HP Victus 15-fa2xxx: Intel i5-13420H (4 performance + 4 efficiency cores, 12 threads), RTX 4050 Laptop 6 GB, 16 GB DDR4-3200 in
one slot, Crucial P3 1 TB NVMe (QLC, no DRAM, 75% full). Measured read-only by five probes; the claims that mattered most were
re-checked by a second agent and by hand. "Measured" means seen on this machine; "general" means known behaviour, not measured here.

## Fixed in the app today

| What | Why it mattered (measured) | Now |
|---|---|---|
| 20 of 23 roles named a model that is no longer installed (`richardyoung/qwen2.5-coder-14b-instruct-abliterated:latest`) | Every coding, review and routing call skipped the GPU worker, went to Ollama and failed with "model not found". The benchmarks never saw it because they clear the roles. | Those 20 roles use the tiers (router on the CPU, worker on the GPU). Backup: `architect_config.json.bak-20261103_174745-before-roles-fix`. |
| Speculative decoding was never on | This llama.cpp build speculates only when asked (`--spec-type`, default none). | The worker uses the n-gram lookup: edits went from 56 to 306-343 tokens/s. |
| Every model server asked for 11 CPU threads | 4 performance cores; CPU generation is limited by memory bandwidth, so extra threads on E-cores and hyperthreads slow it. | One thread per performance core (4; the router gets 2), read from Windows itself. |
| Prompt cache in RAM unlimited | llama-server keeps up to 8 GB of saved prompts per server; the worker log showed it evicting 76-172 MB entries. | Capped by machine size: 1 GB per server on 16 GB, 2 GB on 32 GB; none for the router. |
| RAM check assumed Windows + apps use 6 GB | 9.3 GB was in use with no model loaded. | Checks against what is really free, and adds up the models kept loaded. |
| Ollama models could stay on the card when llama-server started | Ollama keeps models loaded for 30 minutes; llama-server sizes its layers for an empty card. | Models outside your setup are unloaded from Ollama before a model starts on the card. |
| Ollama ran as a child of a terminal | When that terminal closed, Ollama stopped and every call to it failed. | `architect.py start` launches Ollama as its own background process when it is not running. |
| An empty role borrowed a related role's model before using its tier | With the planner on the 14B, the router and four other roles borrowed it: every task was routed by a model running at 2.5 tokens/s. | An empty role uses its own tier first (router on the CPU, the rest on the GPU worker). |
| Ollama's copy of a model could stay on the card while llama-server loaded the same one | Two copies of one model on a 6 GB card: a failed start or a crawl. | Ollama's copy is unloaded first; "exclusive GPU = off" is respected for other models. |
| The scripted benchmark started the real model servers | 3.7 GB of graphics memory held idle afterwards. | Servers start only when a run uses their model. |

## Your settings: proposals (nothing below was changed)

Ordered by effect. Every step is reversible.

1. **Power adapter (the crash).** Measured: Windows logged the charger dropping out at 14:08:56 and coming back at 14:10:06,
   drawing about 89 W from the battery in between. Windows' last record is the charger coming back at 14:10:06; the machine then hung
   with no blue screen and no memory error until it was powered off at 14:23. That points at power delivery under load, not at RAM.
   Check the adapter's rating against the laptop's (usually 150-200 W for an RTX 4050 Victus), reseat the barrel plug, look for a worn
   connector, and do not run heavy jobs on a loose cable. Keep OMEN Gaming Hub on Balanced until it is checked.
2. **The second RAM stick.** One stick means single-channel memory: about half the bandwidth (general: ~25.6 vs ~51.2 GB/s). Every
   model layer that runs from RAM (the router, the heavy model's offloaded part, a mixture-of-experts model's experts) runs about
   twice as fast in dual channel. Match the installed one: 16 GB DDR4-3200 SODIMM (installed: SK Hynix HMAG78EXNSA051N).
3. **Docker Desktop and WSL2.** Not running now, but `.wslconfig` lets the VM take 6 GB of the 16 GB and 4 CPU threads, and it was very
   likely running near the crash. Before heavy runs: quit Docker from the tray, then `wsl --shutdown`. In `C:\Users\alexi\.wslconfig`:
   `memory=4GB`, `processors=3`, and `autoMemoryReclaim=dropCache` (8 GB is fine after the RAM upgrade). In Docker Desktop >
   Settings > AI, turn off GPU-backed inference (Docker Model Runner is a third llama.cpp that could take the same 6 GB).
4. **The three roles on the 14B (planner, architect, evaluator).** It cannot fit the card beside the resident worker, so it runs
   mostly from RAM at about 2.5 tokens/s: a plan takes minutes. Either set those roles to the tiers too (fast, smaller model), or
   wait for the next harness change (stop the worker while the 14B runs), or use the mixture-of-experts model you already have once
   the RAM is in.
5. **Ollama settings.** The Ollama app has "Expose Ollama to the network" ON: if the tray app starts the server, anyone on your network
   can load models onto your GPU. Turn it off (Ollama > Settings). `setx OLLAMA_KEEP_ALIVE 5m` (now 30m) and
   `setx OLLAMA_CONTEXT_LENGTH 8192` (now 16384), then restart Ollama.
6. **NVIDIA memory overflow.** By default the driver lets CUDA spill into system RAM when the card is full (several times slower and it
   takes RAM). NVIDIA Control Panel > Manage 3D settings > Program Settings: add `bin\llama\llama-server.exe` and `ollama.exe`, set
   "CUDA - Sysmem Fallback Policy" to "Prefer No Sysmem Fallback". The app then sees a clear error and adapts instead of crawling.
7. **The external monitor.** The Dell (2560x1440 at 119 Hz) is on HDMI, which this laptop wires to the NVIDIA card: about 0.7 GB of
   graphics memory and the card never idles. If the USB-C port carries video from the Intel GPU, use it; otherwise Win+P > "PC screen
   only" during heavy runs.
8. **Windows Defender.** Real-time scanning is on and the exclusions need admin to read. In an admin PowerShell, check
   `Get-MpPreference | Select ExclusionPath,ExclusionProcess`; if empty, exclude `C:\Users\alexi\.ollama\models`, the project folder,
   and `llama-server.exe` (model loads and test runs touch many files).
9. **Apps holding RAM.** The Claude app (about 2.8 GB) and Edge (about 1.7 GB) are half of the 9.3 GB in use. Close Edge during heavy
   runs, and turn off Edge's Startup boost and "Continue running background apps". Startup apps worth disabling: BitTorrent Web,
   Docker Desktop, Battle.net, Riot Client (Task Manager > Startup apps).
10. **The new 1 TB drive.** Make it the boot drive and pick a TLC drive with a DRAM cache (the P3 is QLC without DRAM: fine for reading
    models, slow for long writes when full). Keep the P3 for the models and point `OLLAMA_MODELS` at it. A Dev Drive for the project
    makes test runs faster (Defender scans it lightly).
11. **Disk space.** Docker's virtual disk is 102 GB after 5 days and Ubuntu's 34 GB; neither shrinks on its own (`docker system df`,
    `docker builder prune`, `docker image prune -a`). Temp holds 2 GB of leftover WSL swap files and about 2,900 old test folders.

## Healthy (no change needed)

Page file system-managed and barely used; no hardware memory errors; the WSL VM and Docker are stopped; Memory Integrity off; Windows
Search off; OneDrive not syncing the project; CPU turbo working (about 4.4 GHz); PCIe x8 as designed; NVIDIA driver 617.42 with CUDA 13.4
runs the bundled CUDA 12 llama-server; the GPU's power limit moves up to 50 W with Dynamic Boost; the laptop screen runs on the Intel
GPU (hybrid mode, correct); Ollama listens on this computer only and keeps one model at a time; nothing spills into shared memory now.

## Done in the app since (measured)

- **The worker steps aside for a big model, only when that helps** (`server.make_room`, auto). Measured on this laptop: the dense 14B
  writes 2.45 tokens a second beside the worker, 1.9 with the worker stopped (Ollama put 39% of it on the card), and 2.3-2.7 on our own
  heavy server with 21 of 40 layers on a full card. The half that stays in RAM sets the speed on one memory channel, so the worker is
  only moved for a model that then fits on the card whole (a 7B planner beside the 4B worker, say). Moving it costs ~25 s.
- The desktop's graphics memory is read live; a rolling log of GPU, RAM, temperature and power is kept in `hub_data/live.log`.
- Shared Python bytecode between test processes was measured slower (1.69 s first, 0.71 s next, against 0.41 s with none), so it was
  not adopted.
- Live check with your own roles: 3 of 3 bug fixes passed (20-34 s each on the 4B worker); a build with the 14B planner passed in 379 s.

## Still to do

- Re-measure the 14B and the MoE after the second RAM stick (dual channel should roughly double the RAM half's speed), then pick the
  planner/architect/evaluator model from those numbers (AFTER_UPGRADE.bat).
- Clean up test folders in Temp.
