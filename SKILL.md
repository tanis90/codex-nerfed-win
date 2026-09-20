---
name: codex-nerfed-win
description: 检测用户的 Codex 是否被静默降智/偷换模型（Windows CLI 版）。用户问"codex 是不是降智了""模型被换了吗""检测一下 nerf/降级"时使用。零成本被动扫描本地 Codex 日志 + 可选的随机数指纹主动探测。不用于评测模型或排查其他工具。
---

# codex-nerfed-win — Codex 降智检测（Windows）

本目录下的 `nerfed_win.py` 是唯一需要执行的脚本；先解析本 SKILL.md 所在目录的绝对路径 `<dir>`，
所有命令形如 `python <dir>/nerfed_win.py <子命令>`。数据只读本机 `~/.codex`，探测记录写
`~/.codex/nerfed-win/`，除探测本身的推理外无任何网络请求。需要 Python 3.9+（仅标准库）和已登录的 Codex。

## 检测流程（按此顺序）

1. **环境自检**（不花额度）：`python <dir>/nerfed_win.py doctor`
   有 FAIL 项就如实报告，不要继续探测。
2. **被动扫描**（不花额度，首选）：`python <dir>/nerfed_win.py scan --hours 0`
   读的是 Codex 自己记录的每轮模型/推理档/上下文窗口，任何"没经过你操作就变了"的都会被标出：
   - `!! hard`：模型被静默更换、推理档被调低、隐藏内部模型跑了一轮、上下文窗口缩水
   - `? soft`：变更走了会话设置（"是你自己改的吗？"）
   - `+ good`：换到了更好或更新的模型
   大陆网络无需代理；此命令完全离线读本地文件。
3. **指纹探测**（可选，消耗约 2-3 次短回答额度）：先 `sessions` 拿会话 id，再
   - `python <dir>/nerfed_win.py probe --thread <id前8位>`（探测该会话实际拿到的是什么模型）
   - 或 `probe --fresh --model <slug>`（探测此刻新会话能拿到什么）
   探测会把会话临时 fork 3 次（ephemeral，不落盘、不进原会话上下文），让模型生成约 300 个
   "随机"数，与 ModelTrace 校准指纹库比对。**会消耗用户账户额度，运行前先告知用户成本，
   用户明确要测再跑。一次就够，不要为"更好看的结果"重复跑。**
   需要代理时脚本会自动探测本地 Clash 端口（7897 等）；403 时用 `--proxy http://127.0.0.1:7897`。
4. **持续监控**（用户想抓"前几句正常、后来变笨"的现行时）：
   `python <dir>/nerfed_win.py watch`（前台挂终端；默认 hard 事件触发——平时零成本扫日志，
   发现 hard 级变更才自动指纹取证）。常用变体：`--probe-now`（启动先来一次基线）、
   `--probe-on turns:20`（每 20 轮探一次）、`--probe-on off`（纯免费监听）。
   判定时间线记录在 `~/.codex/nerfed-win/timeline.jsonl`。单次探测结果可能是瞬时的
   （路由抖动时 MATCH/MISMATCH 会交替），看趋势以时间线为准，不要单点下结论。

## 结果判读（原样转达，不要自行加戏）

- `MATCH 未降智` — 所选模型就在回答
- `MISMATCH` — 指纹 confidently 指向别的模型（区分 downgrade 疑似降智 / upgrade 疑似偷偷升级 / lateral 同档换脑）
- `SUSPICIOUS` — 指纹有偏向但置信度不够，维持观察，等下次探测
- `DOWNGRADED` — Codex 自己的日志实锤（无需指纹）
- `UNLISTED` — 所选模型还不在指纹库里，不是结论
- `INVALID` — 没采到有效样本（拒绝回答/截断），可以重试一次

判定门与上游一致：MISMATCH 需要 p(top) ≥ 80%、p(所选) ≤ 20%、融合分差 ≥ 0.5σ、至少 2 个有效回答。

## 规则

- 脚本的判定是唯一判定；不要自己生成"随机数"代替探测，不要拿别的模型/API 代答，不要与判定争辩。
- 不要修改 `~/.codex/nerfed-win/` 下的任何文件。
- 探测结果和建议如实转达给用户后停止；除非用户要求，不要主动展开"修复降智"以外的操作。
