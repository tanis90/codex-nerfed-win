# codex-nerfed-win

[English](README.en.md) · 简体中文

你在 Codex 里选了一个模型，这个工具检查实际回答的是不是它。

名字没变、脑子换了的情况真实存在：我在自己机器上选 gpt-6-astra 的 xhigh 档，指纹测出 gpt-5.6-luna，本地日志里却仍然写着 astra。日志只记"请求了什么"，指纹测的是"实际来了谁"。

## 功能

`scan`：被动扫描，免费。Codex 每轮把模型、推理档、上下文窗口写进本地日志，scan 读这些文件，报出你没操作却变了的东西：模型被换、推理档被调低、内部模型跑了一轮、窗口缩水。断网也能跑。

`probe`：指纹探测，一次消耗 2-3 条短消息额度。把目标会话临时 fork 一份（不落盘、不进你会话），让模型生成约 300 个随机数。每个模型选数字有固定偏好，和指纹库比对就知道实际是谁在答。探测会伪装成会话自己的客户端身份（Codex Desktop 等），避免服务端按客户端区别对待。

`watch`：持续监控。后台增量扫当前活跃会话，默认发现 hard 级变更才探测，平时零成本。判定序列记在 `timeline.jsonl`，"前几句正常、后面变笨"这件事有时间线可查。

`doctor` / `sessions` / `report` / `explain --method`：环境自检、会话列表、汇总报告、指纹打分的数学原理。

## 安装

Windows + Python 3.9+（只用标准库）+ 已登录的 Codex（桌面版或 CLI）。

```bash
git clone https://github.com/tanis90/codex-nerfed-win.git
cd codex-nerfed-win
python nerfed_win.py doctor
```

## 用法

```bash
python nerfed_win.py scan --hours 0                # 扫全部历史，免费
python nerfed_win.py probe --thread <id前8位>      # 探测某个会话
python nerfed_win.py probe --fresh --model gpt-6-astra --effort medium   # 探测此刻新会话
python nerfed_win.py report                        # 汇总
```

watch 的几种用法：

```bash
python nerfed_win.py watch                                # 发现 hard 级变更才探测
python nerfed_win.py watch --probe-on turns:20            # 每 20 轮探一次
python nerfed_win.py watch --probe-on off                 # 纯免费监听
python nerfed_win.py watch --probe-now                    # 启动先探一次基线
# 控制成本：--queries 2（默认）--cooldown 300 --max-probes 20
```

单次探测是瞬时快照。路由抖动时连续两次探测可能一次 MATCH 一次 MISMATCH，看时间线，别看单点。

### 作为 Agent Skill

把整个仓库目录复制进 agent 的 skills 目录（如 `~/.agents/skills/`），然后对 agent 说"检测一下我的 Codex 是不是降智了"。它会按 `SKILL.md` 先跑免费扫描，探测先征得你同意。

## 判定

| 判定 | 含义 |
| --- | --- |
| MATCH | 所选模型就在回答 |
| MISMATCH | 高置信错配：downgrade 降智 / upgrade 偷偷升级 / lateral 同档换脑 |
| SUSPICIOUS | 指纹有偏向但置信度不够，等下次探测 |
| DOWNGRADED | Codex 自己的日志实锤，无需指纹 |
| UNLISTED | 所选模型不在指纹库，只能报"像谁" |
| INVALID | 没采到有效样本，可重试一次 |

MISMATCH 的门槛与上游一致：p(top) ≥ 80%、p(所选) ≤ 20%、融合分差 ≥ 0.5σ、至少 2 个有效回答。

## Windows 适配了什么

- npm 装的 codex 是 `.CMD` shim，要经 `cmd.exe /c` 启动；不传 `--stdio`（旧版 CLI 不认这个参数，stdio 本来就是默认传输）
- 优先用桌面版自带的新 codex 二进制，回退到 PATH
- 大陆网络下探测自动找本地代理（Clash 系 7897/7890 等），`--proxy` 可指定；被动扫描完全离线
- 默认排除 codex-auto-review 之类的内部线程，`--all` 可含

## 局限

- 指纹只回答"现在是谁"，回溯不了历史：当时的实际模型在本地没有留下可判定的数据
- 指纹库是闭集（13 个模型），库外模型只能报最接近的
- 探测消耗额度，一次等于 2-3 条短消息

## 隐私

只读 `~/.codex`，记录写 `~/.codex/nerfed-win/`。除探测本身的推理外没有任何网络请求：无遥测，无更新检查。

## 致谢

这个项目站在三个项目上：

- [kiyoakii/is-gpt-nerfed](https://github.com/kiyoakii/is-gpt-nerfed)（MIT）：源头。检测框架、判定门、扫描器语义、`codex_appserver.py` 和 `modeltrace_core.py` 两个模块都来自它。上游是 macOS 应用，作者在开发文档里写了"Python 核心可移植，欢迎 Windows 支持"，本仓库就是那份移植。
- [xqy2006/ModelTrace](https://github.com/xqy2006/ModelTrace)（MIT）：指纹库和打分核心，`bank/` 目录直接来自它，交叉验证三回答准确率 100%。
- [hanlinwenyuan/hlwy-ai-checker](https://github.com/hanlinwenyuan/hlwy-ai-checker)：随机数指纹的最初想法（经 is-gpt-nerfed 致谢传递）。

觉得有用的话，先给他们点 star，再给本仓库点。

## 许可

MIT，见 `LICENSE`。vendored 文件保留上游许可：`bank/LICENSE-ModelTrace.txt`，以及 `codex_appserver.py` / `modeltrace_core.py` 文件头的声明。
