# Hardware upgrade, 2026-11-04: what changed, measured

The second RAM stick went in (now 2 x 16 GB DDR4-3200: SK Hynix HMAG78EXNSA051N + Timetec TIMETEC-S16G-3200). The new NVMe has
not been fitted yet. A 1 TB USB drive (E:) and a card reader (D:) sit on the USB-C hub that now also carries the Dell monitor as
DisplayPort (the laptop's HDMI port stopped detecting it). Measured with `AFTER_UPGRADE.bat` (reports in
`hub_data/after_upgrade_20261103_2046.md` and `hub_data/after_upgrade_20261104_0057.md`) and a cache-bypassing drive test.

## Models: before (1 stick) and after (2 sticks)

| | Before | After |
|---|---|---|
| Worker 4B, new code / edits | 60.6 / 311 tokens/s, starts in 6.6 s | 67.9 / 334, starts in 5.7 s |
| 14B (Q4_K_M), half in RAM | 2.3 tokens/s, starts in 19 s | **5.1** (2.2x), starts in 9.6 s |
| 35B mixture-of-experts (IQ3), experts in RAM | could not run (needed ~9.5 GB free RAM) | **15.4** new code / 19.8 edits, starts in 14 s |

The 14B's speed is set by the half of it read from RAM for every word, so it tracks RAM bandwidth: dual channel doubles the ceiling
(25.6 -> 51.2 GB/s in theory; a similar DDR4-3200 laptop measured 22.7 -> 41.6 GB/s,
https://tpongo-afk.github.io/posts/2026/01/13/how-does-flex-memory-mode-compare-to-dual-channel-mode/). 2.2x measured here is
consistent with that, so the sticks run in dual channel. The MoE reads only the few experts each word uses, so it is three times
the 14B's speed. Moving the monitor to the Intel graphics also freed ~0.85 GB of the NVIDIA card (1.05 GB -> 0.2 GB in use idle):
the 14B now gets 22 of 40 layers on the card instead of 21.

## Drives (sequential, 4 MB blocks, Windows cache bypassed)

| Drive | Write | Read | A 9 GB model would load in |
|---|---|---|---|
| Internal Crucial P3 NVMe | 1,970 MB/s | 1,630 MB/s | ~6 s |
| E: "YGS ProductCode", on the hub | 20 MB/s | 23 MB/s | ~6.5 min |
| D: card reader, on the hub | 8.7 MB/s | 18 MB/s | ~8 min |

**The hub's data side is USB 2.0.** Windows shows no "Generic SuperSpeed USB Hub" anywhere, and a USB 3 hub always appears as a
SuperSpeed hub plus a USB 2.0 hub (https://learn.microsoft.com/en-us/windows-hardware/drivers/usbcon/usb-faq--introductory-level);
its inner hub 05E3:0608 is a Genesys GL850G, USB 2.0 only. USB-C hubs that drive a monitor often give data only the USB 2.0 wires.

**E: is very likely a fake-capacity stick.** Three signs:
- its USB id 048D:1234 with the placeholder name "ProductCode" matches documented fake "2 TB" sticks (one corrupted data at 140 GB,
  https://forums.tomshardware.com/threads/cant-force-fake-2-tb-pendrive-to-reveal-its-total-capacity.3708974; another held 7.2 GB,
  https://www.myapheus.com/2020/07/should-you-buy-one-of-those-cheap-high-capacity-usb-flash-drives-probably-not/);
- it claims exactly 1,048,576,000,000 bytes = 1,000,000 MiB, a round number in binary units, where real flash has an irregular size
  (a real 1 TB drive is 1,000,204,886,016 bytes);
- 20-23 MB/s is a USB 2.0 flash stick, not an SSD.
GRC bought 12 cheap 1-2 TB sticks and all 12 were fake (https://www.grc.com/validrive.htm). On such a stick, files written past the
real capacity are lost silently. Until it is checked (GRC ValiDrive spot-checks 576 places across the claimed space in minutes and
puts back what was there, https://www.grc.com/validrive/faq.htm), put nothing on it that matters.

## What this means for Architect

- Models stay on the internal NVMe. A model loaded from a USB drive is also memory-mapped from it, so a drive that drops (monitor or
  hub unplugged) would crash the server (https://learn.microsoft.com/en-us/windows/win32/memory/reading-and-writing-from-a-file-view).
- E: and D: are not useful to the app. A reputable USB 3.2 SSD plugged straight into the laptop, or the internal NVMe when it comes,
  is the real extra space.
- The heavy model for the planner, architect and evaluator should now be chosen by measured pass rate per minute, MoE against
  14B. Speed alone (15.4 vs 5.1 tokens/s) does not settle it: the MoE file is a 3-bit quant.
