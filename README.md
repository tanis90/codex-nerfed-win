# codex-nerfed-win

[English](README.en.md) · 简体中文

你在 Codex 里选了一个模型。这套工具告诉你**实际回答的到底是不是它**。

如果你遇到过"前几句话好好的，越聊越像降智"——`watch` 模式会把这种体感变成带时间戳的
MATCH→MISMATCH 曲线。

## 致敬 Credits

本项目是从下面这些工作移植、搬运、组合而来，检测思路与代码质量全靠他们：

- **[kiyoakii/is-gpt-nerfed](https://github.com/kiyoakii/is-gpt-nerfed)**（MIT）— 源头项目。检测框架、
  判定门（p≥80%/≤20%/0.5σ/≥2 采样）、被动扫描器的全部判定语义、`codex_appserver.py` 与
  `modeltrace_core.py` 两个模块均来自它。上游是 macOS 菜单栏应用，作者在开发文档中说明
  "Python 核心是可移植的，欢迎 Windows/Linux 支持"——本仓库就是这个 Windows 移植。
- **[xqy2006/ModelTrace](https://github.com/xqy2006/ModelTrace)**（MIT）— 指纹库与打分核心。
  `bank/unified_bank.json` 与打分算法直接来自它（交叉验证三回答准确率 100%），许可见
  `bank/LICENSE-ModelTrace.txt`。
- **[hanlinwenyuan/hlwy-ai-checker](https://github.com/hanlinwenyuan/hlwy-ai-checker)** — 随机数指纹的
  最初灵感（经 is-gpt-nerfed 致谢传递）。

如果这个工具对你有用，请去给上面三个项目点 star；给本仓库点 star 也可以，但先给他们。

## 两条检测路径

| 路径 | 命令 | 成本 | 原理 |
| --- | --- | --- | --- |
| 被动扫描 | `scan` | 零 token | Codex 每轮都把所用模型/推理档/上下文窗口写进本地 rollout 日志，扫描并标出一切"未经你操作就变了"的记录 |
| 指纹探测 | `probe` | 约 2-3 次短回答 | 把目标会话临时 fork（ephemeral，不落盘、不污染原会话），让模型生成约 300 个"随机"数；模型选随机数有特征性偏好，与校准指纹库比对 |

被动扫描分级：`hard`（模型被静默换、推理档被调低、隐藏内部模型跑了一轮、上下文窗口缩水）、
`soft`（变更走了会话设置——"是你改的吗？"）、`good`（换到了更好的模型）、`info`（仅记录）。

## 安装

要求：Windows + Python 3.9+（仅标准库）+ 已登录的 Codex（桌面版或 CLI 均可）。

```bash
git clone https://github.com/tanis90/codex-nerfed-win.git
cd codex-nerfed-win
python nerfed_win.py doctor    # 环境自检（不花额度）
```

## 使用

```bash
python nerfed_win.py doctor                        # 环境自检（不花额度）
python nerfed_win.py sessions                      # 最近会话 + 模型/推理档
python nerfed_win.py scan --hours 0                # 被动扫描全部历史（不花额度）
python nerfed_win.py probe --thread <id前8位>      # 指纹探测某会话（花约 3 次短回答额度）
python nerfed_win.py probe --fresh --model gpt-6-astra --effort medium   # 探测此刻新会话
python nerfed_win.py report                        # 汇总报告
python nerfed_win.py explain --method              # 指纹打分的数学方法
```

### watch：抓"聊着聊着变笨"的现行

被动扫描管日志里看得见的变更（模型名/推理档/窗口），指纹探测管"名字没变、脑子换了"。
watch 把两者串成时间线：平时零成本增量扫当前活跃会话的日志，按策略自动指纹取证，
判定序列记在 `~/.codex/nerfed-win/timeline.jsonl`。

```bash
python nerfed_win.py watch                                # 默认：发现 hard 级变更才探测（事件触发，平时零成本）
python nerfed_win.py watch --probe-now                    # 启动先做一次基线探测
python nerfed_win.py watch --probe-on turns:20            # 每 20 轮探一次（盯长会话降智）
python nerfed_win.py watch --probe-on minutes:30          # 每 30 分钟活动量探一次
python nerfed_win.py watch --probe-on off                 # 纯免费监听，永不探测
python nerfed_win.py watch --thread <id> --interval 5     # 固定盯某个会话，5 秒轮询
# 成本控制：--queries 2（默认）--cooldown 300 --max-probes 20
```

首次附着老会话只回放历史基线、不触发探测；Ctrl+C 退出落盘游标，重启续扫。
单次探测是瞬时快照——路由抖动时连续两次探测可能一次 MATCH 一次 MISMATCH，看趋势请用时间线。

### 作为 Agent Skill（zcode / kimi code 等）

把整个仓库目录复制进 agent 的 skills 目录（如 `~/.agents/skills/`），用户对 agent 说
"检测一下我的 Codex 是不是降智了"，agent 会按 `SKILL.md` 的流程执行：先零成本扫描、
征得同意再跑指纹探测、原样转达判定。

## 判定结果

| 判定 | 含义 |
| --- | --- |
| MATCH | 所选模型就在回答 |
| MISMATCH | 高置信错配（downgrade 降智 / upgrade 偷偷升级 / lateral 同档换脑） |
| SUSPICIOUS | 指纹有偏向但不够自信，维持观察 |
| DOWNGRADED | Codex 自己的日志实锤（无需指纹） |
| UNLISTED | 所选模型不在指纹库，只能说像谁 |
| INVALID | 没采到有效样本，可重试一次 |

MISMATCH 门槛与上游一致：p(top) ≥ 80% 且 p(所选) ≤ 20% 且融合分差 ≥ 0.5σ 且 ≥ 2 个有效回答。

## Windows 适配说明

相对上游的改动集中在：

- 探测进程启动：npm 装的 `codex` 是 `.CMD` shim，需经 `cmd.exe /c`；不传 `--stdio`（stdio 本来就是默认传输，旧版 CLI 反而不认这个参数）
- 二进制选择：优先用桌面版自带的新版 codex（`AppData\Local\OpenAI\Codex\bin\*\codex.exe`），回退 PATH
- 大陆网络：探测自动探测本地代理端口（Clash 系 7897/7890 等），`--proxy` 可显式指定；被动扫描完全离线
- 客户端伪装：探测时读会话真实 originator（如 Codex Desktop）并伪装一致，避免服务端按客户端路由造成假阳性（上游同款做法）
- 内部线程过滤：默认排除 Codex 自己的 `subagent`/`guardian_review` 线程（如 codex-auto-review），`--all` 可含

## 局限（同上游）

- "所选模型"指 Codex 请求的模型；若服务端换权重但保留模型名，只有指纹或上下文窗口变化能发现。
- 指纹库是闭集（13 个模型），库外模型会映射到最相近的。
- 指纹只能回答"现在是谁"，无法回溯历史会话当时是谁——本地日志只记请求侧。
- 探测消耗账户额度；一次探测 = 2-3 个约 300 数字的短回答。

## 隐私

只读本机 `~/.codex`；探测记录写 `~/.codex/nerfed-win/`；除探测本身的推理外**无任何网络请求**
（无遥测、无更新检查）。探测临时 fork 不落盘、不进原会话上下文。

## 许可

MIT（见 `LICENSE`）。 vendored 文件各自的许可保留：`bank/LICENSE-ModelTrace.txt`（ModelTrace），
`codex_appserver.py` / `modeltrace_core.py` 文件头的上游声明。
