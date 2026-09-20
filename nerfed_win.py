#!/usr/bin/env python3
"""nerfed-win — is-gpt-nerfed 的 Windows CLI 移植：检测 Codex 是否被静默降智。

两条检测路径，与上游 kiyoakii/is-gpt-nerfed 语义一致：

  passive  scan   读本地 rollout JSONL（零 token 成本）：模型被换、推理档被调低、
                  隐藏内部模型跑了一轮、上下文窗口缩水，全部来自 Codex 自己的记录。
  active   probe  通过 `codex app-server` 把目标会话临时 fork 3 次（ephemeral，不落盘、
                  不进原会话上下文），每个 fork 让模型生成约 300 个"随机"数，用 ModelTrace
                  校准指纹库比对，判断实际回答的和所选的是不是同一个模型。

命令：
  nerfed_win.py doctor                       环境自检（不花额度）
  nerfed_win.py sessions [--last 20]         最近会话 + 模型/推理档
  nerfed_win.py scan [--hours 48] [--all]    被动扫描（不花额度）
  nerfed_win.py probe --thread <id>          指纹探测指定会话（花 3 次短回答额度）
  nerfed_win.py probe --fresh --model <slug> 全新临时会话探测（同上）
  nerfed_win.py report                       汇总：会话 + 扫描发现 + 探测结论
  nerfed_win.py explain [--method]           方法说明

数据只读本机 ~/.codex；探测记录写 ~/.codex/nerfed-win/。除探测本身的推理外无任何网络请求。

上游致谢（均为 MIT）：
  kiyoakii/is-gpt-nerfed — 检测框架、判定门、passive scanner 设计
  xqy2006/ModelTrace — 指纹库 bank/unified_bank.json、打分核心 modeltrace_core.py
Python 3.9+，仅标准库。
"""
from __future__ import annotations

import argparse
import contextlib
import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
BANK_PATH = os.path.join(HERE, "bank", "unified_bank.json")

sys.path.insert(0, HERE)
import codex_appserver as appserver  # noqa: E402
import modeltrace_core as core       # noqa: E402

VERSION = "0.1.0"
CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
NERFED_HOME = os.path.join(CODEX_HOME, "nerfed-win")
PROBES_DIR = os.path.join(NERFED_HOME, "probes")

MARGIN_SIGMA = 0.5
MISMATCH_CONFIDENCE = 0.8

EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
SIZE_TAGS = (("nano", 0), ("mini", 1), ("spark", 1), ("lite", 1), ("small", 1), ("flash", 1),
             ("reserve", 1), ("auto-review", 1))

SEVERITY_TAG = {"hard": "!!", "good": "+ ", "soft": "? ", "info": "  "}
SEVERITY_COLOR = {"hard": "\x1b[31m", "good": "\x1b[32m", "soft": "\x1b[33m", "info": "\x1b[90m"}
RESET = "\x1b[0m"
COLOR = sys.stdout.isatty()


# --------------------------------------------------------------------------- console helpers


def c(text: str, code: str) -> str:
    return f"{code}{text}{RESET}" if COLOR else text


def out(msg: str = "") -> None:
    print(msg)


def iso(ts_ms: int | None = None) -> str:
    if ts_ms:
        return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")


def read_json(path: str, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def ensure_dirs() -> None:
    os.makedirs(PROBES_DIR, exist_ok=True)


def log_event(kind: str, **fields) -> None:
    ensure_dirs()
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, **fields}
    with open(os.path.join(NERFED_HOME, "log.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- model catalog
# 以下与上游 nerfed 语义一致：模型优劣不是硬编码名单，而是按可信度排序的信号推断。


def load_catalog() -> dict:
    d = read_json(os.path.join(CODEX_HOME, "models_cache.json"), {}) or {}
    models = d.get("models") if isinstance(d, dict) else None
    if isinstance(models, dict):
        models = list(models.values())
    out_catalog = {}
    for m in models or []:
        if isinstance(m, dict) and m.get("slug"):
            out_catalog[m["slug"]] = {
                "display_name": m.get("display_name"), "visibility": m.get("visibility"),
                "context_window": m.get("context_window"), "priority": m.get("priority"),
                "upgrade": m.get("upgrade"),
                "supported_reasoning_levels": [(lvl.get("effort") if isinstance(lvl, dict) else lvl)
                                               for lvl in (m.get("supported_reasoning_levels") or [])]}
    return out_catalog


def hidden_models(cat: dict | None = None) -> frozenset:
    cat = load_catalog() if cat is None else cat
    return frozenset(s.lower() for s, m in cat.items() if (m.get("visibility") or "").lower() == "hide")


def normalize_model(slug) -> str:
    s = str(slug or "").strip().lower()
    return s.split("/", 1)[1] if "/" in s else s


def model_version(slug: str) -> tuple[int, int] | None:
    m = re.search(r"(?:^|-)(\d+)(?:[.\-](\d{1,2}))?(?=$|-)", slug)
    return (int(m.group(1)), int(m.group(2) or 0)) if m else None


def model_size(slug: str) -> int:
    size = 2
    for tag, val in SIZE_TAGS:
        if tag in slug:
            size = min(size, val)
    if re.search(r"(^|-)(pro|ultra)($|-)", slug):
        size = 3
    return size


def effort_idx(e) -> int | None:
    try:
        return EFFORTS.index(str(e or "").lower())
    except ValueError:
        return None


def model_profile(slug: str | None, cat: dict) -> dict:
    s = normalize_model(slug)
    entry = cat.get(s) or {}
    levels = [effort_idx(x) for x in entry.get("supported_reasoning_levels") or []]
    levels = [x for x in levels if x is not None]
    return {"slug": s, "version": model_version(s), "size": model_size(s),
            "hidden": (entry.get("visibility") or "").lower() == "hide",
            "priority": entry.get("priority"), "max_effort": max(levels) if levels else None,
            "ctx": entry.get("context_window"),
            "upgrade": normalize_model(entry.get("upgrade")) if entry.get("upgrade") else None}


def compare_models(old: str | None, new: str | None, cat: dict | None = None) -> dict:
    cat = load_catalog() if cat is None else cat
    a, b = model_profile(old, cat), model_profile(new, cat)

    def done(direction: str, confidence: str, reason: str) -> dict:
        return {"direction": direction, "confidence": confidence, "reason": reason}

    if a["slug"] == b["slug"]:
        return done("lateral", "high", "same model")
    if a["upgrade"] and a["upgrade"] == b["slug"]:
        return done("upgrade", "high", f"目录标注 {b['slug']} 是 {a['slug']} 的后继模型")
    if b["upgrade"] and b["upgrade"] == a["slug"]:
        return done("downgrade", "high", f"目录标注 {a['slug']} 是 {b['slug']} 的后继模型")
    if b["hidden"] and not a["hidden"]:
        return done("downgrade", "high", f"{b['slug']} 是 Codex 不对外列出的内部模型")
    if a["hidden"] and not b["hidden"]:
        return done("upgrade", "medium", f"从内部模型 {a['slug']} 回到了公开模型")
    if a["version"] and b["version"] and a["version"] != b["version"]:
        newer = b["version"] > a["version"]
        fmt = lambda v: f"{v[0]}.{v[1]}" if v[1] else str(v[0])
        conf = "high" if a["size"] == b["size"] else "medium"
        return done("upgrade" if newer else "downgrade", conf,
                    f"{'更新' if newer else '更旧'}的世代 ({fmt(b['version'])} vs {fmt(a['version'])})")
    if a["size"] != b["size"]:
        larger = b["size"] > a["size"]
        return done("upgrade" if larger else "downgrade", "high",
                    f"{'更大' if larger else '更小'}的型号档位")
    if a["priority"] is not None and b["priority"] is not None and a["priority"] != b["priority"]:
        above = b["priority"] < a["priority"]
        return done("upgrade" if above else "downgrade", "medium",
                    f"目录把 {b['slug']} 排在 {a['slug']} {'上' if above else '下'}方")
    if a["max_effort"] is not None and b["max_effort"] is not None and a["max_effort"] != b["max_effort"]:
        higher = b["max_effort"] > a["max_effort"]
        return done("upgrade" if higher else "downgrade", "low",
                    f"支持{'更高' if higher else '更低'}的推理档位上限")
    if a["ctx"] and b["ctx"] and a["ctx"] != b["ctx"]:
        larger = b["ctx"] > a["ctx"]
        return done("upgrade" if larger else "downgrade", "low",
                    f"{'更大' if larger else '更小'}的上下文窗口")
    return done("lateral", "low", "同世代同档位，目录未排序")


def classify_change(old: str | None, new: str | None, cat: dict | None = None) -> str:
    return compare_models(old, new, cat)["direction"]


# --------------------------------------------------------------------------- rollout scanner
# 与上游 scan_rollout 同一套判定。scan 状态可持久化（cursor 增量游标 + seen 去重键），
# watch 模式靠它只解析每个 rollout 新追加的部分。


def new_scan_state() -> dict:
    return {"cursor": 0, "model": None, "effort": None, "ctx": None, "tier": None, "applied": None,
            "turns": 0, "originator": None, "cli_version": None, "models_seen": {}, "efforts_seen": {},
            "seen": []}


def scan_rollout_step(path: str, scan: dict, hidden=frozenset(), cap_bytes: int = 64 << 20) -> list[dict]:
    """Incrementally scan a rollout JSONL from scan["cursor"]. Mutates `scan`; returns NEW evidence."""
    ev: list[dict] = []
    seen_keys = set(scan.get("seen") or [])

    def add(kind: str, severity: str, detail: str, ts=None, turn=None, **extra):
        key = f"{kind}|{detail}|{ts}"
        if key in seen_keys:
            return
        seen_keys.add(key)
        ev.append({"ts": ts or "", "kind": kind, "severity": severity, "detail": detail,
                   "turn_id": turn, **extra})

    try:
        size = os.path.getsize(path)
    except OSError:
        return ev
    cur = int(scan.get("cursor") or 0)
    if cur > size:  # 文件被截断/轮转，从头再扫
        scan.clear()
        scan.update(new_scan_state())
        cur = 0
    if cur >= size:
        return ev
    with open(path, "rb") as f:
        f.seek(cur)
        data = f.read(min(size - cur, cap_bytes))
    end = data.rfind(b"\n")
    if end < 0:
        return ev
    for raw in data[:end].split(b"\n"):
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except Exception:
            continue
        rtype, p, ts = rec.get("type"), rec.get("payload") or {}, rec.get("timestamp")
        if rtype == "session_meta":
            scan["originator"], scan["cli_version"] = p.get("originator"), p.get("cli_version")
        elif rtype == "turn_context":
            model = p.get("model")
            settings = (p.get("collaboration_mode") or {}).get("settings") or {}
            effort = settings.get("reasoning_effort") or p.get("reasoning_effort") or p.get("effort")
            turn = p.get("turn_id")
            scan["turns"] += 1
            applied = scan.get("applied") or {}
            if model:
                scan["models_seen"][model] = scan["models_seen"].get(model, 0) + 1
                if model.lower() in hidden:
                    add("hidden_model", "hard", f"隐藏/内部模型 {model} 跑了一轮", ts, turn)
                if scan["model"] is None:
                    scan["model"] = model
                elif model != scan["model"]:
                    cmp = compare_models(scan["model"], model)
                    change = cmp["direction"]
                    good = change == "upgrade" and cmp["confidence"] in ("high", "medium")
                    if applied.get("model") == model:
                        add("applied_model_change", "soft" if change == "downgrade" else ("good" if good else "info"),
                            f"会话设置把模型 {scan['model']} → {model}（{change}）", ts, turn, why=cmp["reason"])
                    else:
                        add("silent_model_change", "hard" if change == "downgrade" else ("good" if good else "soft"),
                            f"模型 {scan['model']} → {model}，无设置变更（{change}）", ts, turn, why=cmp["reason"])
                    scan["model"] = model
            if effort:
                scan["efforts_seen"][effort] = scan["efforts_seen"].get(effort, 0) + 1
                if scan["effort"] is None:
                    scan["effort"] = effort
                elif effort != scan["effort"]:
                    oi, ni = effort_idx(scan["effort"]), effort_idx(effort)
                    lower = oi is not None and ni is not None and ni < oi
                    if applied.get("effort") == effort:
                        add("applied_effort_change", "soft" if lower else "info",
                            f"会话设置把推理档 {scan['effort']} → {effort}", ts, turn)
                    else:
                        add("silent_effort_change", "hard" if lower else "soft",
                            f"推理档 {scan['effort']} → {effort}，无设置变更", ts, turn)
                    scan["effort"] = effort
        elif rtype == "event_msg":
            et = p.get("type")
            if et == "thread_settings_applied":
                s = p.get("thread_settings") or {}
                scan["applied"] = {"model": s.get("model"), "effort": s.get("reasoning_effort"),
                                   "tier": s.get("service_tier"), "ts": ts}
                tier = s.get("service_tier")
                if tier:
                    if scan["tier"] is None:
                        scan["tier"] = tier
                    elif tier != scan["tier"]:
                        add("service_tier_change", "info", f"服务档 {scan['tier']} → {tier}", ts)
                        scan["tier"] = tier
            elif et == "token_count":
                ctx = (p.get("info") or {}).get("model_context_window")
                if isinstance(ctx, int) and ctx > 0:
                    if scan["ctx"] is None:
                        scan["ctx"] = ctx
                    elif ctx != scan["ctx"]:
                        add("context_window_change", "hard" if ctx < scan["ctx"] else "info",
                            f"上下文窗口 {scan['ctx']} → {ctx}", ts)
                        scan["ctx"] = ctx
    scan["cursor"] = cur + end + 1
    scan["seen"] = list(seen_keys)
    return ev


def scan_rollout(path: str, hidden=frozenset(), cap_bytes: int = 256 << 20) -> tuple[dict, list[dict]]:
    """Full scan of one rollout JSONL. Returns (state, evidence records)."""
    scan = new_scan_state()
    ev = scan_rollout_step(path, scan, hidden, cap_bytes=cap_bytes)
    return scan, ev


# --------------------------------------------------------------------------- sessions (sqlite)


def state_db() -> str | None:
    cands = sorted(glob.glob(os.path.join(CODEX_HOME, "state_*.sqlite")))
    return cands[-1] if cands else None


THREAD_COLS = "id, rollout_path, cwd, model, reasoning_effort, updated_at_ms, title, name, archived, thread_source, cli_version"


def db_threads(limit: int = 100, include_archived: bool = False) -> list[dict]:
    db = state_db()
    if not db:
        return []
    # subagent / guardian_review 是 Codex 自己的内部线程（含 codex-auto-review），不算用户会话
    where = ("" if include_archived else
             "WHERE archived = 0 AND (thread_source IS NULL OR thread_source NOT IN ('subagent', 'guardian_review'))")
    sql = f"SELECT {THREAD_COLS} FROM threads {where} ORDER BY updated_at_ms DESC LIMIT ?"
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(sql, (limit,)).fetchall()]
        finally:
            con.close()
    except Exception as e:
        out(c(f"读取 Codex 数据库失败: {e}", SEVERITY_COLOR["hard"]))
        return []


def probe_history() -> dict[str, dict]:
    """thread_id -> latest probe verdict, from our ledger."""
    index = os.path.join(NERFED_HOME, "probes.jsonl")
    latest: dict[str, dict] = {}
    for rec in _jsonl(index):
        tid = rec.get("thread") or {}
        key = tid.get("id") or "__fresh__"
        latest[key] = rec
    return latest


def _jsonl(path: str) -> list[dict]:
    recs = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        recs.append(json.loads(line))
                    except Exception:
                        pass
    except OSError:
        pass
    return recs


# --------------------------------------------------------------------------- verdict layer


def assess(expected: str | None, analysis: dict | None, hard_evidence: list[dict]) -> dict:
    """与上游同一条判定门：MISMATCH 需要 p(top) ≥ 0.8、p(declared) ≤ 0.2、
    融合 z 分差 ≥ 0.5σ、至少 2 个有效回答；否则 SUSPICIOUS。"""
    res = {"verdict": None, "direction": None, "p_top": None, "p_expected": None,
           "margin": None, "top": None}
    if hard_evidence:
        res.update(verdict="DOWNGRADED", direction="hard",
                   why=hard_evidence[0].get("detail"))
        return res
    if not analysis or not analysis.get("results"):
        res["verdict"] = "INVALID"
        return res
    top = analysis["results"][0]
    res.update(top=top["model"], p_top=top["probability"])
    exp = normalize_model(expected)
    if not exp:
        res["verdict"] = "UNKNOWN"
        return res
    exp_row = next((r for r in analysis["results"] if r["model"] == exp), None)
    if exp_row is None:
        res["verdict"] = "UNLISTED"
        return res
    res.update(p_expected=exp_row["probability"], margin=top["score"] - exp_row["score"])
    if top["model"] == exp:
        res.update(verdict="MATCH", confidence="high" if top["probability"] >= MISMATCH_CONFIDENCE else "low")
        return res
    eps = 1e-9
    confident = (top["probability"] + eps >= MISMATCH_CONFIDENCE
                 and exp_row["probability"] <= (1 - MISMATCH_CONFIDENCE) + eps
                 and res["margin"] + eps >= MARGIN_SIGMA)
    if analysis.get("used_outputs") is not None and int(analysis["used_outputs"]) < 2:
        confident = False
    res.update(verdict="MISMATCH" if confident else "SUSPICIOUS",
               direction=classify_change(exp, top["model"]))
    return res


VERDICT_CN = {"MATCH": "未降智", "MISMATCH": "指纹错配", "SUSPICIOUS": "可疑", "DOWNGRADED": "记录实锤降智",
              "UNLISTED": "模型不在指纹库", "INVALID": "无有效样本", "UNKNOWN": "未知"}


def verdict_line(a: dict, expected: str | None) -> str:
    v = a.get("verdict")
    if v == "MATCH":
        return c(f"MATCH 未降智 — 指纹指向 {a['top']}（{a['p_top']:.0%}），与所选模型一致", SEVERITY_COLOR["good"])
    if v == "MISMATCH":
        arrow = {"downgrade": "疑似降智", "upgrade": "疑似偷偷升级", "lateral": "疑似同档换脑"}[a.get("direction") or "lateral"]
        return c(f"MISMATCH {arrow} — 你选的是 {expected}，指纹说是 {a['top']}（{a['p_top']:.0%}，"
                 f"所选模型仅 {a['p_expected']:.0%}）", SEVERITY_COLOR["hard"])
    if v == "SUSPICIOUS":
        return c(f"SUSPICIOUS 可疑 — 指纹偏向 {a['top']}（{a['p_top']:.0%}）而非 {expected}（{a['p_expected']:.0%}），"
                 f"但置信度不够，维持观察", SEVERITY_COLOR["soft"])
    if v == "DOWNGRADED":
        return c(f"DOWNGRADED 记录实锤 — Codex 自己的日志: {a.get('why')}", SEVERITY_COLOR["hard"])
    if v == "UNLISTED":
        return f"UNLISTED — {expected} 不在指纹库里，只能说像 {a['top']}（{a['p_top']:.0%}），不算结论"
    return f"{v} {VERDICT_CN.get(v, '')}"


# --------------------------------------------------------------------------- commands


def find_codex_bin() -> str | None:
    p = os.environ.get("NERFED_WIN_CODEX_BIN")
    if p and os.path.exists(p):
        return p
    # 桌面版自带的 codex 比 npm PATH 上的新、协议与上游测试版本（0.154）更接近，优先使用
    desktop = []
    pattern = os.path.join(os.path.expanduser("~"), "AppData", "Local", "OpenAI", "Codex", "bin", "*", "codex.exe")
    for path in glob.glob(pattern):
        with contextlib.suppress(OSError):
            desktop.append((os.path.getmtime(path), path))
    if desktop:
        return max(desktop)[1]
    return shutil.which("codex")


def cmd_doctor(args) -> int:
    ok = True

    def check(cond: bool, label: str, detail: str = "") -> None:
        nonlocal ok
        mark = c("OK ", SEVERITY_COLOR["good"]) if cond else c("FAIL", SEVERITY_COLOR["hard"])
        if not cond:
            ok = False
        out(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))

    out("nerfed-win doctor")
    check(os.path.isdir(CODEX_HOME), f"CODEX_HOME {CODEX_HOME}", "Codex 未安装？")
    db = state_db()
    check(bool(db), "Codex 状态数据库", db or "未找到 state_*.sqlite")
    if db:
        check(bool(db_threads(1)), "threads 表可读")
    check(os.path.exists(os.path.join(CODEX_HOME, "models_cache.json")), "models_cache.json（模型目录，判定升降级用）")
    check(os.path.exists(BANK_PATH), "指纹库 bank/unified_bank.json")

    cb = find_codex_bin()
    check(bool(cb), "codex 可执行文件", cb or "PATH 上没有 codex")
    version = None
    if cb:
        try:
            prefix = ["cmd.exe", "/c"] if os.name == "nt" and cb.lower().endswith((".cmd", ".bat")) else []
            r = subprocess.run(prefix + [cb, "--version"], capture_output=True, text=True, timeout=30)
            version = (r.stdout or r.stderr).strip()
        except Exception as e:
            out(f"        codex --version 失败: {e}")
        check(version is not None, "codex --version", version or "")

        # app-server 握手（只 initialize，不跑推理，不花额度）
        try:
            app = appserver.AppServer(cb)
            try:
                app.initialize()
                check(True, "app-server JSON-RPC 握手", "initialize 成功（probe 可用）")
            finally:
                app.close()
        except Exception as e:
            check(False, "app-server JSON-RPC 握手", str(e))

    out()
    out("结论: " + ("环境齐全，scan 和 probe 都可用。" if ok else "有缺失项，见上。"))
    return 0 if ok else 1


def cmd_sessions(args) -> int:
    rows = db_threads(args.last)
    if not rows:
        out("没有找到会话（数据库为空或不可读）。")
        return 1
    hidden = hidden_models()
    hist = probe_history()
    out(f"最近 {len(rows)} 个会话（{iso(rows[-1]['updated_at_ms'])} 起）:")
    for r in rows:
        tid, model, effort = r["id"], r.get("model") or "?", r.get("reasoning_effort") or "-"
        flags = []
        if model and model.lower() in hidden:
            flags.append(c("隐藏模型", SEVERITY_COLOR["hard"]))
        pr = hist.get(tid)
        if pr:
            v = pr.get("assess", {}).get("verdict")
            flags.append(f"probe:{v}")
        title = (r.get("title") or r.get("name") or (r.get("cwd") or ""))[:42]
        out(f"  {iso(r['updated_at_ms'])}  {model:<18} {effort:<7} {tid[:8]}  {title}"
            + (f"  [{'/'.join(flags)}]" if flags else ""))
    out()
    out("用 `scan` 做零成本被动检测，`probe --thread <id>` 做指纹探测。")
    return 0


def _aggregate(ev: list[dict]) -> list[dict]:
    """同类发现合并成一行（相邻、同 kind 同 detail 只计一次，累加次数）。"""
    merged: list[dict] = []
    for e in ev:
        if merged and merged[-1]["kind"] == e["kind"] and merged[-1]["detail"] == e["detail"] \
                and merged[-1]["severity"] == e["severity"]:
            merged[-1]["count"] = merged[-1].get("count", 1) + 1
            continue
        merged.append(dict(e, count=1))
    return merged


def cmd_scan(args) -> int:
    rows = db_threads(args.limit, include_archived=args.all)
    cutoff_ms = (time.time() - args.hours * 3600) * 1000 if args.hours else 0
    if args.thread:
        rows = [r for r in rows if r["id"].startswith(args.thread)] or \
               [{"id": args.thread, "rollout_path": None, "model": None, "reasoning_effort": None,
                 "updated_at_ms": 0, "title": "", "cwd": ""}]
    rows = [r for r in rows if not cutoff_ms or (r.get("updated_at_ms") or 0) >= cutoff_ms]
    if not rows:
        out(f"近 {args.hours} 小时没有会话；用 --hours 0 扫描全部。")
        return 0
    cat = load_catalog()
    hidden = hidden_models(cat)
    total_hard = 0
    scanned = 0
    findings_all = []
    for r in rows:
        rp = r.get("rollout_path")
        if not rp or not os.path.exists(rp):
            continue
        scanned += 1
        scan_state, ev = scan_rollout(rp, hidden)
        hard = [e for e in ev if e["severity"] == "hard"]
        total_hard += len(hard)
        findings_all.extend({"thread": r["id"], **e} for e in ev)
        if not args.json and ev:
            title = (r.get("title") or r.get("cwd") or r["id"])[:40]
            out(f"\n{iso(r['updated_at_ms'])}  {r['id'][:8]}  {title}")
            out(f"  模型轨迹: {' → '.join(scan_state['models_seen']) or '?'}   推理档: "
                f"{' → '.join(scan_state['efforts_seen']) or '?'}   轮数: {scan_state['turns']}")
            for e in _aggregate(ev):
                tag = SEVERITY_TAG.get(e["severity"], "  ")
                line = c(f"  {tag} [{e['ts'][:16] if e['ts'] else ''}] {e['detail']}", SEVERITY_COLOR.get(e["severity"], ""))
                if e.get("why"):
                    line += f"  ({e['why']})"
                if e.get("count", 1) > 1:
                    line += c(f"  ×{e['count']}", SEVERITY_COLOR.get(e["severity"], ""))
                out(line)
    if args.json:
        print(json.dumps({"scanned": scanned, "hard": total_hard, "findings": findings_all},
                         ensure_ascii=False, indent=1))
        return 0
    out()
    if scanned == 0:
        out("没有可扫描的 rollout 文件。")
        return 1
    if total_hard:
        out(c(f"结论: {scanned} 个会话中发现 {total_hard} 条 hard 级证据（Codex 自己的记录显示模型/档位/窗口被动过）。", SEVERITY_COLOR["hard"]))
        return 2
    out(c(f"结论: {scanned} 个会话未发现静默变更（hard 级）。soft/good/info 见上；无输出即无任何变更。", SEVERITY_COLOR["good"]))
    return 0


def ensure_proxy(explicit: str | None = None, no_proxy: bool = False) -> str | None:
    """大陆网络直连 chatgpt.com 会 403，探测进程需要代理。尊重已有环境变量；
    否则探测常见本地代理端口（Clash 系）。返回生效的代理（None = 未设置）。"""
    import socket
    if no_proxy:
        return None
    if explicit:
        os.environ["HTTPS_PROXY"] = os.environ["HTTP_PROXY"] = explicit
        return explicit
    for var in ("HTTPS_PROXY", "https_proxy"):
        if os.environ.get(var):
            return os.environ[var]
    for port in (7897, 7890, 7891, 2080, 10809):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                proxy = f"http://127.0.0.1:{port}"
                os.environ["HTTPS_PROXY"] = os.environ["HTTP_PROXY"] = proxy
                return proxy
        except OSError:
            continue
    return None


def _thread_originator(tid: str) -> str | None:
    """从 rollout 的 session_meta 读会话真实客户端（如 Codex Desktop），探测时伪装成它，
    避免服务端按客户端身份路由造成假阳性（上游同款做法）。"""
    row = next((r for r in db_threads(300) if r["id"] == tid), None)
    rp = (row or {}).get("rollout_path")
    if not rp or not os.path.exists(rp):
        return None
    try:
        with open(rp, "r", encoding="utf-8") as f:
            for line in f:
                rec = json.loads(line)
                if rec.get("type") == "session_meta":
                    return (rec.get("payload") or {}).get("originator")
                if rec.get("type") == "turn_context":
                    break
    except (OSError, ValueError):
        pass
    return None


def run_probe(thread_id: str | None = None, model: str | None = None, effort: str | None = None,
              fresh: bool = False, queries: int = 3, timeout: float = 300.0, trigger: str = "manual") -> dict:
    """执行一次指纹探测并写入台账。返回含 assess/analysis/record 的 dict；
    error 非 None 时探测未成功。cmd_probe 和 watch 共用此函数。"""
    cb = find_codex_bin()
    if not cb:
        return {"error": "找不到 codex 可执行文件；设 NERFED_WIN_CODEX_BIN 指定路径。"}
    ensure_proxy()
    try:
        if fresh:
            if not model:
                model, effort = config_toml_model()
            if not model:
                return {"error": "fresh 探测需要 model（--model 或 config.toml）。"}
            record = appserver.probe_fresh(cb, model, effort=effort, queries=queries, timeout_s=timeout)
        else:
            record = appserver.probe_thread(cb, thread_id, queries=queries, timeout_s=timeout,
                                            originator=_thread_originator(thread_id))
    except appserver.AppServerError as e:
        return {"error": str(e)}
    expected = record["thread"].get("model")
    outputs = [{"text": f.get("text"), "expected_count": f.get("count")} for f in record.get("forks", [])]
    analysis = None
    analysis_error = ""
    try:
        analysis = core.analyze_global_outputs(outputs, core.load_bank(BANK_PATH))
    except ValueError as e:
        analysis_error = str(e)
    a = assess(expected, analysis, [])
    a["verdict_cn"] = VERDICT_CN.get(a["verdict"], "")
    pid = appserver.new_probe_id()
    ensure_dirs()
    with open(os.path.join(PROBES_DIR, pid + ".json"), "w", encoding="utf-8") as f:
        json.dump({"id": pid, "ts": datetime.now(timezone.utc).isoformat(), "assess": a,
                   "analysis": analysis, "record": record, "trigger": trigger}, f, ensure_ascii=False, indent=1)
    with open(os.path.join(NERFED_HOME, "probes.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"id": pid, "thread": record["thread"], "assess": a, "trigger": trigger},
                           ensure_ascii=False) + "\n")
    log_event("probe_verdict", probe=pid, verdict=a["verdict"], expected=expected, trigger=trigger)
    return {"id": pid, "assess": a, "analysis": analysis, "record": record, "expected": expected,
            "analysis_error": analysis_error,
            "fork_errors": [f["error"] for f in record.get("forks", []) if f.get("error")]}


def cmd_probe(args) -> int:
    tid = None
    if not args.fresh:
        tid = args.thread
        if not tid:
            out("需要 --thread <id>（先跑 sessions 查看会话 id）。")
            return 1
        if len(tid) < 36:  # 8 位前缀等短 id → 从数据库解析成完整 UUID
            match = next((r for r in db_threads(300) if r["id"].startswith(tid)), None)
            if not match:
                out(f"没有找到以 {tid} 开头的会话。")
                return 1
            tid = match["id"]
        out(f"指纹探测: 会话 {tid}，{args.queries} 个 ephemeral fork…")
    else:
        out(f"指纹探测（全新临时会话）: {args.model or 'config.toml 默认'} effort={args.effort or 'default'}，{args.queries} 个采样…")
    r = run_probe(thread_id=tid, model=args.model, effort=args.effort, fresh=args.fresh,
                  queries=args.queries, timeout=args.timeout)
    if r.get("error"):
        out(c(f"探测失败: {r['error']}", SEVERITY_COLOR["hard"]))
        return 1
    for err in r.get("fork_errors") or []:
        out(c(f"  fork 失败: {err}", SEVERITY_COLOR["soft"]))
    a = r["assess"]
    out()
    out("  " + verdict_line(a, r.get("expected")))
    if r.get("analysis"):
        out(f"  有效样本 {r['analysis']['used_outputs']}；Top 候选: " +
            ", ".join(f"{x['model']} {x['probability']:.0%}" for x in r["analysis"]["results"][:3]))
    elif r.get("analysis_error"):
        out(c(f"  {r['analysis_error']}", SEVERITY_COLOR["soft"]))
    out(f"\n  记录: {os.path.join(PROBES_DIR, r['id'] + '.json')}")
    return 0 if a["verdict"] == "MATCH" else (2 if a["verdict"] == "MISMATCH" else 1)


def config_toml_model() -> tuple[str | None, str | None]:
    model = effort = None
    try:
        with open(os.path.join(CODEX_HOME, "config.toml"), "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s.startswith("["):
                    break
                m = re.match(r'^model\s*=\s*"([^"]+)"', s)
                if m:
                    model = m.group(1)
                m = re.match(r'^model_reasoning_effort\s*=\s*"([^"]+)"', s)
                if m:
                    effort = m.group(1)
    except OSError:
        pass
    return model, effort


WATCH_STATE_PATH = os.path.join(NERFED_HOME, "watch-state.json")


def tnow() -> str:
    return datetime.now().astimezone().strftime("%H:%M:%S")


def _timeline(event: str, **fields) -> None:
    ensure_dirs()
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
    with open(os.path.join(NERFED_HOME, "timeline.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _parse_probe_on(spec: str) -> tuple[str, int]:
    if spec == "hard":
        return "hard", 0
    if spec == "off":
        return "off", 0
    m = re.match(r"turns:(\d+)$", spec)
    if m:
        return "turns", int(m.group(1))
    m = re.match(r"minutes:(\d+)$", spec)
    if m:
        return "minutes", int(m.group(1))
    raise SystemExit(f"不认识的 --probe-on: {spec}（可选 hard / off / turns:N / minutes:N）")


def cmd_watch(args) -> int:
    mode, n = _parse_probe_on(args.probe_on)
    ensure_dirs()
    state = read_json(WATCH_STATE_PATH) or {}
    state.setdefault("threads", {})
    probes_done = 0
    last_probe_ts = 0.0
    current_tid = None
    catalog_ts = 0.0
    hidden = frozenset()

    out(f"nerfed-win watch — 轮询 {args.interval}s｜探测策略 {args.probe_on}｜每次 {args.queries} 采样｜"
        f"冷却 {args.cooldown}s｜上限 {args.max_probes} 次。Ctrl+C 退出。")
    out(f"台账: {NERFED_HOME}")

    def do_probe(tid: str | None, why: str, fresh_model: tuple[str, str] | None = None) -> None:
        nonlocal probes_done, last_probe_ts
        if probes_done >= args.max_probes:
            out(c(f"[{tnow()}] ⚠ 探测次数达上限（--max-probes {args.max_probes}），不再探测", SEVERITY_COLOR["soft"]))
            return
        if time.time() - last_probe_ts < args.cooldown and last_probe_ts:
            out(c(f"[{tnow()}] ⚠ 冷却中（{args.cooldown}s），跳过本次探测", SEVERITY_COLOR["soft"]))
            return
        probes_done += 1
        last_probe_ts = time.time()
        target = "fresh" if fresh_model else (tid or "")[:8]
        out(f"[{tnow()}] ⟩ 指纹探测 [{why}] target={target} …")
        r = run_probe(thread_id=tid, fresh=bool(fresh_model), model=fresh_model[0] if fresh_model else None,
                      effort=fresh_model[1] if fresh_model else None, queries=args.queries, trigger=why)
        if r.get("error"):
            out(c(f"[{tnow()}]   探测失败: {r['error']}", SEVERITY_COLOR["hard"]))
            _timeline("probe_error", thread=tid, trigger=why, error=r["error"])
            return
        a = r["assess"]
        out(f"[{tnow()}]   {verdict_line(a, r.get('expected'))}")
        _timeline("probe_verdict", thread=tid, probe=r["id"], verdict=a["verdict"], direction=a.get("direction"),
                  expected=r.get("expected"), top=a.get("top"))

    def pick_active(threads: list[dict]) -> dict | None:
        if args.thread:
            for r in threads:
                if r["id"].startswith(args.thread):
                    args.thread = r["id"]  # 展开前缀为完整 id
                    return r
            return None
        cutoff = time.time() * 1000 - args.active_window * 60 * 1000
        for r in threads:
            if r.get("rollout_path") and os.path.exists(r["rollout_path"]) \
                    and (r.get("updated_at_ms") or 0) >= cutoff:
                return r
        return None

    if args.probe_now:  # 启动基线：有活跃会话测会话，没有就测 fresh 基线
        row = pick_active(db_threads(50))
        if row:
            do_probe(row["id"], "baseline")
        else:
            m, e = config_toml_model()
            do_probe(None, "baseline-fresh", fresh_model=(m or "gpt-6-astra", e or "medium"))

    try:
        while True:
            if time.time() - catalog_ts > 60:
                hidden = hidden_models()
                catalog_ts = time.time()
            row = pick_active(db_threads(50))
            if not row:
                if current_tid != "idle":
                    out(f"[{tnow()}] 无活跃会话（{args.active_window} 分钟窗口内），待命…")
                    current_tid = "idle"
                time.sleep(args.interval)
                continue
            tid = row["id"]
            st = state["threads"].setdefault(tid, new_scan_state())
            st.setdefault("last_probe_turn", 0)
            st.setdefault("last_probe_ts", 0.0)
            if tid != current_tid:
                current_tid = tid
                title = (row.get("title") or row.get("cwd") or "")[:40]
                out(f"[{tnow()}] ▶ 跟踪会话 {tid[:8]} {title}（{row.get('model')}/{row.get('reasoning_effort')}，"
                    f"已扫 {st['turns']} 轮）")
                _timeline("session_start", thread=tid, title=title, model=row.get("model"),
                          effort=row.get("reasoning_effort"))
            first_step = st["turns"] == 0
            turns_before = st["turns"]
            ev = scan_rollout_step(row["rollout_path"], st, hidden)
            if first_step and st["turns"]:
                # 首次附着：历史回放只做记录，探测计时从现在开始算
                st["last_probe_turn"] = st["turns"]
                st["last_probe_ts"] = time.time()
            if st["turns"] != turns_before:
                out(f"[{tnow()}] · turn {st['turns']}（{st['model']}/{st['effort']}）")
                _timeline("turn", thread=tid, turns=st["turns"], model=st["model"], effort=st["effort"])
            for e in _aggregate(ev):
                suffix = (f"（{e['why']}）" if e.get("why") else "") + (f" ×{e['count']}" if e.get("count", 1) > 1 else "")
                out(c(f"[{tnow()}] {SEVERITY_TAG.get(e['severity'], '  ')} {e['detail']}{suffix}",
                      SEVERITY_COLOR.get(e["severity"], "")))
                _timeline("finding", thread=tid, kind=e["kind"], severity=e["severity"], detail=e["detail"])
            if first_step and ev:
                out(f"[{tnow()}]   （以上为该会话的历史基线，不触发探测）")
            if mode == "hard" and not first_step and any(e["severity"] == "hard" for e in ev):
                do_probe(tid, "hard-finding")
            elif mode == "turns" and st["turns"] - st["last_probe_turn"] >= n:
                st["last_probe_turn"] = st["turns"]
                do_probe(tid, f"turns:{n}")
            elif mode == "minutes" and st["turns"] and time.time() - st["last_probe_ts"] >= n * 60:
                st["last_probe_ts"] = time.time()
                do_probe(tid, f"minutes:{n}")
            with open(WATCH_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        out(f"\n[{tnow()}] 退出。本次探测 {probes_done} 次。时间线: {os.path.join(NERFED_HOME, 'timeline.jsonl')}")
        _timeline("watch_stop", probes=probes_done)
    return 0


def cmd_report(args) -> int:
    out(f"nerfed-win report  —  {iso()}")
    out()
    cmd_sessions(argparse.Namespace(last=args.last))
    out()
    out("被动扫描（零成本）:")
    rc = cmd_scan(argparse.Namespace(hours=args.hours, all=False, thread=None, limit=args.last, json=False))
    out()
    out("探测历史:")
    hist = probe_history()
    if not hist:
        out("  （还没有跑过 probe；`probe --thread <id>` 或 `probe --fresh --model <slug>`）")
    else:
        for key, rec in list(hist.items())[-10:]:
            tid = rec.get("thread") or {}
            a = rec.get("assess") or {}
            label = tid.get("id", "fresh")[:8] if tid.get("id") else "fresh"
            out(f"  {rec.get('ts', '')[:19]}  {label}  {a.get('verdict', '?')} ({a.get('verdict_cn', '')})"
                f"  expected={tid.get('model')}")
    return rc


def cmd_explain(args) -> int:
    if args.method:
        out(core.__doc__.strip() if core.__doc__ else "见 modeltrace_core.py")
        return 0
    out(__doc__.strip())
    return 0


# --------------------------------------------------------------------------- main


def enable_utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    enable_utf8_console()
    ap = argparse.ArgumentParser(prog="nerfed_win.py",
                                 description="检测 Codex 是否被静默降智（is-gpt-nerfed 的 Windows CLI 移植）")
    ap.add_argument("--version", action="version", version=f"nerfed-win {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="环境自检（不花额度）")
    sub.add_parser("explain", help="方法说明").add_argument("--method", action="store_true",
                                                            help="指纹打分的数学方法")

    s = sub.add_parser("sessions", help="最近会话列表")
    s.add_argument("--last", type=int, default=20)

    s = sub.add_parser("scan", help="被动扫描 rollout 日志（零成本）")
    s.add_argument("--hours", type=int, default=48, help="只扫最近 N 小时（0 = 全部）")
    s.add_argument("--all", action="store_true", help="含已归档会话")
    s.add_argument("--thread", help="只扫该会话（id 前缀即可）")
    s.add_argument("--limit", type=int, default=300)
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("probe", help="指纹探测（消耗 3 次短回答额度）")
    s.add_argument("--thread", help="目标会话 id（sessions 里 8 位前缀即可）")
    s.add_argument("--fresh", action="store_true", help="探测全新临时会话而非已有会话")
    s.add_argument("--model", help="--fresh 用的模型 slug（默认 config.toml）")
    s.add_argument("--effort", help="--fresh 用的推理档")
    s.add_argument("--queries", type=int, default=3, help="采样次数 1-3（默认 3，准确率最高）")
    s.add_argument("--timeout", type=float, default=300)
    s.add_argument("--proxy", help="推理走该代理（如 http://127.0.0.1:7897）；默认自动探测本地 Clash 端口")
    s.add_argument("--no-proxy", action="store_true", help="禁用代理自动探测")

    s = sub.add_parser("report", help="汇总报告")
    s.add_argument("--last", type=int, default=20)
    s.add_argument("--hours", type=int, default=48)

    s = sub.add_parser("watch", help="后台监控：免费扫日志，事件触发指纹取证")
    s.add_argument("--thread", help="固定监控该会话（默认自动跟最新活跃会话）")
    s.add_argument("--probe-on", default="hard",
                   help="hard=发现 hard 级变更才探测（默认）；turns:N=每 N 轮探测；minutes:N=每 N 分钟活动量；off=只扫不探")
    s.add_argument("--probe-now", action="store_true", help="启动时先做一次基线探测")
    s.add_argument("--queries", type=int, default=2, help="每次探测采样数 1-3（默认 2）")
    s.add_argument("--cooldown", type=int, default=300, help="两次探测最小间隔秒数")
    s.add_argument("--max-probes", type=int, default=20, help="本次运行最多探测次数")
    s.add_argument("--interval", type=int, default=10, help="轮询间隔秒")
    s.add_argument("--active-window", type=int, default=30, help="会话算活跃的窗口分钟数")

    args = ap.parse_args()
    handlers = {"doctor": cmd_doctor, "sessions": cmd_sessions, "scan": cmd_scan,
                "probe": cmd_probe, "report": cmd_report, "explain": cmd_explain, "watch": cmd_watch}
    try:
        return handlers[args.cmd](args)
    except KeyboardInterrupt:
        return 130
    except appserver.AppServerError as e:
        out(c(f"app-server 错误: {e}", SEVERITY_COLOR["hard"]))
        return 1


if __name__ == "__main__":
    sys.exit(main())
