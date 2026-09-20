# codex-nerfed-win

English · [简体中文](README.md)

You pick a model in Codex. This tool tells you whether that model is actually the one answering.

If you've ever felt "the first few replies are fine, then it gets dumber as the session goes on" —
the `watch` mode turns that feeling into a timestamped MATCH→MISMATCH timeline.

## Credits

This project is a port of, and heavily built on, the work below. The detection design and code
quality are theirs:

- **[kiyoakii/is-gpt-nerfed](https://github.com/kiyoakii/is-gpt-nerfed)** (MIT) — the origin project.
  The detection framework, the verdict gate (p≥80% / ≤20% / 0.5σ / ≥2 answers), the full semantics of
  the passive scanner, and the vendored `codex_appserver.py` / `modeltrace_core.py` modules all come
  from it. Upstream is a macOS menu bar app whose development notes say "the Python is portable" and
  welcome Windows/Linux support — this repository is that Windows port.
- **[xqy2006/ModelTrace](https://github.com/xqy2006/ModelTrace)** (MIT) — the fingerprint bank and
  scoring core. `bank/unified_bank.json` and the scoring algorithm come straight from it
  (100% accuracy with three answers in cross-validation); license at `bank/LICENSE-ModelTrace.txt`.
- **[hanlinwenyuan/hlwy-ai-checker](https://github.com/hanlinwenyuan/hlwy-ai-checker)** — the original
  random-number fingerprint idea (credited via is-gpt-nerfed).

If this tool is useful to you, star the three projects above first. Starring this repo is welcome too,
but star them first.

## The two detection paths

| path | command | cost | how it works |
| --- | --- | --- | --- |
| passive scan | `scan` | zero tokens | Codex records the model / reasoning effort / context window of every turn in local rollout logs; the scanner flags everything that changed without you changing it |
| fingerprint probe | `probe` | ~2-3 short answers | forks the target session ephemerally (never written to disk, never pollutes your thread), asks for ~300 "random" numbers; every model picks them with a characteristic bias, compared against the calibrated bank |

Passive-scan severities: `hard` (silent model swap, lowered reasoning effort, hidden internal model
running a turn, shrunken context window), `soft` (the change went through thread settings — "was that
you?"), `good` (moved to a better model), `info` (logged only).

## Install

Requires: Windows + Python 3.9+ (stdlib only) + a signed-in Codex (desktop app or CLI).

```bash
git clone https://github.com/tanis90/codex-nerfed-win.git
cd codex-nerfed-win
python nerfed_win.py doctor    # environment self-check (free)
```

## Usage

```bash
python nerfed_win.py doctor                        # environment self-check (free)
python nerfed_win.py sessions                      # recent sessions + model/effort
python nerfed_win.py scan --hours 0                # passive scan of all history (free)
python nerfed_win.py probe --thread <first 8 of id>      # fingerprint-probe one session (~3 short answers)
python nerfed_win.py probe --fresh --model gpt-6-astra --effort medium   # what a new session gets right now
python nerfed_win.py report                        # summary report
python nerfed_win.py explain --method              # the math behind the fingerprint
```

### watch: catch "it gets dumber mid-session" in the act

The passive scan covers changes visible in the logs (model name / effort / window); the fingerprint
probe covers "same name, different brain". watch chains the two into a timeline: it incrementally
scans the active session's log for free and probes automatically per your policy; the verdict
sequence lands in `~/.codex/nerfed-win/timeline.jsonl`.

```bash
python nerfed_win.py watch                                # default: probe only on hard findings (event-triggered, free while quiet)
python nerfed_win.py watch --probe-now                    # one baseline probe at startup
python nerfed_win.py watch --probe-on turns:20            # probe every 20 turns (watch a long session)
python nerfed_win.py watch --probe-on minutes:30          # probe every 30 minutes of activity
python nerfed_win.py watch --probe-on off                 # free-only monitoring, never probes
python nerfed_win.py watch --thread <id> --interval 5     # pin one session, poll every 5 s
# cost control: --queries 2 (default) --cooldown 300 --max-probes 20
```

Attaching to an old session replays its history as a baseline without triggering probes; Ctrl+C
persists the scan cursor and the next run resumes where it left off.
A single probe is an instantaneous snapshot — under flapping routing, back-to-back probes can return
MATCH then MISMATCH. Read the timeline, not the single data point.

### As an Agent Skill (zcode / kimi code, etc.)

Copy the whole repository folder into your agent's skills directory (e.g. `~/.agents/skills/`).
Say "check whether my Codex got nerfed" and the agent follows `SKILL.md`: free scan first, fingerprint
probe only with your consent, verdict relayed verbatim.

## Verdicts

| verdict | meaning |
| --- | --- |
| MATCH | the model you selected is the one answering |
| MISMATCH | confident mismatch (downgrade / secret upgrade / lateral brain swap) |
| SUSPICIOUS | the fingerprint leans elsewhere, not confidently enough; stands until the next probe |
| DOWNGRADED | Codex's own records show a silent switch; no fingerprint needed |
| UNLISTED | your model is not in the bank; can only say who it resembles |
| INVALID | no usable sample; you may retry once |

The MISMATCH gate matches upstream: p(top) ≥ 80% and p(selected) ≤ 20% and a fused score margin
≥ 0.5σ and at least 2 valid answers.

## Windows adaptations

The changes relative to upstream concentrate on:

- probe process launch: npm-installed `codex` is a `.CMD` shim and must go through `cmd.exe /c`; do not pass `--stdio` (stdio is the default transport, and older CLI builds reject the flag)
- binary selection: prefer the newer codex bundled with the desktop app (`AppData\Local\OpenAI\Codex\bin\*\codex.exe`), fall back to PATH
- restricted networks: the probe auto-detects local proxy ports (Clash-family 7897/7890 …); `--proxy` overrides. Passive scanning is fully offline
- client impersonation: the probe reads the session's real originator (e.g. Codex Desktop) and identifies itself the same way, avoiding client-based-routing false positives (same approach as upstream)
- internal-thread filtering: Codex's own `subagent` / `guardian_review` threads (e.g. codex-auto-review) are excluded by default; `--all` includes them

## Limits (same as upstream)

- "The model you selected" is the model Codex requested. If the server swaps the weights and keeps the
  name, only the fingerprint or a context-window change can show it.
- The bank is a closed set (13 models); a model outside it maps to its nearest look-alike.
- The fingerprint answers "who is answering now"; it cannot retroactively identify who answered a
  past session — local logs record only the request side.
- Probes consume account quota: one probe = 2-3 short answers of ~300 numbers each.

## Privacy

Reads `~/.codex` only; probe records go to `~/.codex/nerfed-win/`; apart from the probe's own
inference there are **no network requests at all** (no telemetry, no update checks). Probe forks are
ephemeral: never written to disk, never injected into your thread.

## License

MIT — see `LICENSE`. Vendored files keep their upstream licenses: `bank/LICENSE-ModelTrace.txt`
(ModelTrace), and the upstream notices in the headers of `codex_appserver.py` / `modeltrace_core.py`.
