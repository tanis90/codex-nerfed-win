# codex-nerfed-win

English · [简体中文](README.md)

You pick a model in Codex. This tool checks whether that model is actually the one answering.

Same name, different brain happens for real: on my machine, selecting gpt-6-astra at xhigh effort
fingerprinted as gpt-5.6-luna, while the local log still said astra. The log records what was
*requested*; the fingerprint measures what showed up.

## What it does

`scan`: passive scan, free. Codex writes the model, reasoning effort, and context window of every
turn into local logs. scan reads those files and reports anything that changed without you changing
it: a swapped model, a lowered effort, an internal model running a turn, a shrunken window. Works
offline.

`probe`: fingerprint probe, costs 2-3 short messages per run. It forks the target session
temporarily (never written to disk, never enters your thread) and asks for ~300 random numbers.
Every model picks numbers with a fixed bias; comparing against the fingerprint bank tells you who
actually answered. The probe identifies itself as the session's own client (Codex Desktop etc.) so
the server can't route it differently.

`watch`: continuous monitoring. Incrementally scans the active session in the background, probes
only when a hard-severity finding appears, otherwise costs nothing. Verdicts go into
`timeline.jsonl`, so "fine at first, dumber later" becomes a timeline instead of a feeling.

`doctor` / `sessions` / `report` / `explain --method`: environment check, session list, summary
report, and the math behind the fingerprint.

## Install

Windows + Python 3.9+ (stdlib only) + a signed-in Codex (desktop or CLI).

```bash
git clone https://github.com/tanis90/codex-nerfed-win.git
cd codex-nerfed-win
python nerfed_win.py doctor
```

## Usage

```bash
python nerfed_win.py scan --hours 0                # scan all history, free
python nerfed_win.py probe --thread <first 8 of id>      # probe one session
python nerfed_win.py probe --fresh --model gpt-6-astra --effort medium   # what a new session gets now
python nerfed_win.py report                        # summary
```

watch variants:

```bash
python nerfed_win.py watch                                # probe only on hard findings
python nerfed_win.py watch --probe-on turns:20            # probe every 20 turns
python nerfed_win.py watch --probe-on off                 # free-only monitoring
python nerfed_win.py watch --probe-now                    # one baseline probe at startup
# cost control: --queries 2 (default) --cooldown 300 --max-probes 20
```

A single probe is an instantaneous snapshot. Under flapping routing, back-to-back probes can return
MATCH then MISMATCH. Read the timeline, not the single data point.

### As an Agent Skill

Copy the whole repository folder into your agent's skills directory (e.g. `~/.agents/skills/`) and
say "check whether my Codex got nerfed". It follows `SKILL.md`: free scan first, probe only with
your consent.

## Verdicts

| verdict | meaning |
| --- | --- |
| MATCH | the model you selected is the one answering |
| MISMATCH | confident mismatch: downgrade / secret upgrade / lateral brain swap |
| SUSPICIOUS | the fingerprint leans elsewhere, not confidently; wait for the next probe |
| DOWNGRADED | Codex's own records show a silent switch; no fingerprint needed |
| UNLISTED | your model is not in the bank; can only report who it resembles |
| INVALID | no usable sample; retry once |

The MISMATCH gate matches upstream: p(top) ≥ 80%, p(selected) ≤ 20%, a fused score margin ≥ 0.5σ,
at least 2 valid answers.

## What was adapted for Windows

- npm-installed codex is a `.CMD` shim and must launch through `cmd.exe /c`; `--stdio` is not passed
  (older CLI builds reject the flag; stdio is the default transport anyway)
- prefers the newer codex binary bundled with the desktop app, falls back to PATH
- the probe auto-detects local proxies (Clash-family 7897/7890 …) for restricted networks;
  `--proxy` overrides. Passive scanning is fully offline
- Codex's internal threads (codex-auto-review and friends) are excluded by default; `--all` includes them

## Limits

- The fingerprint answers "who is answering now"; it cannot identify who answered a past session —
  that data was never recorded locally
- The bank is a closed set (13 models); a model outside it maps to its nearest look-alike
- Probes cost quota: one run equals 2-3 short messages

## Privacy

Reads `~/.codex` only; records go to `~/.codex/nerfed-win/`. Apart from the probe's own inference
there are no network requests at all: no telemetry, no update checks.

## Credits

This project stands on three:

- [kiyoakii/is-gpt-nerfed](https://github.com/kiyoakii/is-gpt-nerfed) (MIT): the origin. The
  detection framework, the verdict gate, the scanner semantics, and the vendored
  `codex_appserver.py` / `modeltrace_core.py` all come from it. Upstream is a macOS app whose
  development notes say "the Python is portable" and welcome Windows support; this repository is
  that port.
- [xqy2006/ModelTrace](https://github.com/xqy2006/ModelTrace) (MIT): the fingerprint bank and
  scoring core; the `bank/` directory comes straight from it (100% accuracy with three answers in
  cross-validation).
- [hanlinwenyuan/hlwy-ai-checker](https://github.com/hanlinwenyuan/hlwy-ai-checker): the original
  random-number fingerprint idea (credited via is-gpt-nerfed).

If this is useful to you, star those three first, then this one.

## License

MIT, see `LICENSE`. Vendored files keep their upstream licenses: `bank/LICENSE-ModelTrace.txt` and
the notices in the headers of `codex_appserver.py` / `modeltrace_core.py`.
