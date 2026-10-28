<img src="assets/AppIcon.png" width="80" height="80" alt="模型灯塔图标">

# Model Beacon · 模型灯塔

轻量的 macOS 桌面数字归因工具，支持 Codex 会话配置、数字指纹参考、手动采样和自动后台检测。会话继续聊天时，工具可冻结独立快照并在临时分支采样；原聊天不接收数字题。

**[下载最新版](../../releases/latest) · [全部版本](../../releases) · [维护者发布指南](RELEASING.md)**

首次上传源码后，成功运行发布工作流，下载页才会有安装包。GitHub 自动生成的 “Source code” 压缩包是源码。

## 普通用户：下载与安装

当前支持 **macOS / Apple Silicon（M 系列芯片，arm64）**，提供 DMG 和应用 ZIP。最低部署目标为 **macOS 13.0**，本机验证环境为 Apple Silicon / macOS 27；未逐一实测其他 macOS 版本，不提供 Intel、Windows 或 Linux 安装包。检查范围见 [VALIDATION.md](VALIDATION.md)。

| Release 文件 | 安装方法 |
| --- | --- |
| `model-beacon-版本-macos-arm64.dmg` | 打开后将 `Model Beacon.app` 拖到 `Applications` |
| `model-beacon-版本-macos-arm64.zip` | 解压，将 `Model Beacon.app` 移到“应用程序” |
| `SHA256SUMS.txt` / `*-manifest.json` | 校验下载文件，查看版本、架构和签名信息 |

从“应用程序”打开 **Model Beacon（模型灯塔）**。应用自带 Python 运行时、图标和参考数据，无需安装 Python、Node.js、Xcode、拉取源码或填写 API Key。采样使用本机已安装、已登录且支持冻结分支接口的 Codex 运行程序；应用不读取账号凭据，也不替换服务地址。

默认构建使用 **ad-hoc 临时签名，未经过 Apple Developer ID 签名与公证**。首次打开被系统阻止时，确认下载来源后，可在“系统设置 → 隐私与安全性”中允许打开。维护者可按[发布指南](RELEASING.md)配置正式签名；每个版本的实际状态以发布清单为准。

## 日常使用

- **单击圆球**立即展开信息卡。点击卡片空白处保持展开，只有右上角“—”收起。
- **信息卡**显示会话、当前配置模型和数字归因的返回模型；有进度或提示时，上次检测时间放在同一行右侧，只有时间时靠左显示。
- **执行进度**区分“初检中”和“复测 1/3”；每批从初检开始，运行中只显示当次进度，历史结果明确标为上次记录。
- **归因记录**保留七项：记录名称、时间、有效样本、尝试次数、当前配置模型、返回模型和原因。
- **右键圆球或信息卡**打开系统菜单，提供手动归因、自动归因、新会话采样、最近记录、主题和会话选择。自动归因是全局开关，前面有 ✓ 表示开启，再点击即可关闭。
- **取消当前归因**会立即停止本工具的采样请求，状态从“正在取消”转为“已取消”，原聊天继续运行。
- **拖动与吸边**：拖动圆球或卡片空白处移动，靠近边缘松手吸附；展开和收起保留圆球位置。明暗主题保留金属边缘、高光和外部阴影。
- **按名称选择会话**后，工具用唯一 ID 绑定并保存，重启恢复。未绑定时跟随最近活跃会话；点击归因或开启自动归因，会绑定当前显示的会话。日志活跃度不等同于前台聊天。

打开卡片、移动鼠标和打开菜单不发送模型请求。默认不开启自动归因；只有明确点击采样，或已开启的自动检测达到条件，才会发送数字题。任务可取消，重复点击不会创建多个任务。

本地元数据只用于会话名称、配置、轮次、上下文段和工具计数。默认读取 `~/.codex/sessions`，可用 `CODEX_HOME` 指定目录；已有文件每秒检查增量，新会话通常在下一次目录扫描（约 15 秒）发现。

## 数字归因怎样运行

已有会话先通过 Codex 原生接口冻结独立快照，再从快照创建临时采样分支。**原会话回复和执行工具时也能后台采样，不等待整轮结束。** 采样沿用原会话的原生参数，核对目标、模型、提供方和工作目录；只向临时分支发送数字题。工具清理自己创建的快照，不修改源会话文件。

已有会话 ID 但尚无聊天记录时，会读取该会话自己的配置，无需先发送打招呼消息。没有会话记录，或希望单独检查某个配置时，右键选择“新会话采样…”，在原生对话框中选择模型和提供方；结果标注独立采样范围。

**常规检查先采 1 份，匹配即结束；候选与所选型号不一致时立即提醒，再固定使用同一快照完成 3 份独立复测，总计最多 4 次 `turn/start`。** 复测分别判断，全部 3 份均不匹配才升级为持续差异；其中一次匹配仍完成本批复测，并保留最初的差异记录。没有适用型号参考时显示参考候选，不额外启动这批复测。

采样额外消耗账号模型额度，Codex 原生运行程序可能自行重试底层网络请求。失败和无效输出显示具体原因；不伪造数字、不补齐答案，也不把相似分数当作准确率。

统一参考库覆盖 **9 个型号**：`gpt-5.4`、`gpt-5.5`、`gpt-5.6-sol / terra / luna`、`gpt-6-astra / sol / luna`、`gpt-6.1-sol`。各型号共用数字特征提取、候选评分和复测流程。

结果显示为接近参考、指纹有差异或参考候选，并保留配置、时间和采样范围。数字指纹用于辅助比较。

只手动归因一次、自动关闭时，不会继续采样。源会话继续新一轮、修改配置或切换上下文段后，最近结果标记为上次归因或上一配置的归因；重启恢复的结果标为历史。同配置下继续聊天不影响冻结采样。模型、提供方、原生参数或上下文段变化时，旧批次停止后续请求，已发送请求收尾；开启自动归因时，再按新配置检查。结果始终属于采样时的配置和轮次。

### 自动检测规则

自动默认关闭，开关在应用内全局保存。任何时候只监测当前绑定的一个会话；切换会话后仍保持开启，并先对新会话安排一次后台初检。快速切走再切回、关闭后重新开启时，新首检会等待旧任务退出后执行。各会话的工具完成进度、随机检查点和历史结果仍相互独立。没有可读会话或日志读取失败时，界面保留真实开关状态，已开启的监测仍可关闭。

| 条件 | 行为 |
| --- | --- |
| 首次开启或恢复已启用的监测 | 安排一次后台检查 |
| 模型、原生会话参数或上下文段变化 | 安排新配置的检查 |
| 累计随机 16～32 次正常工作工具完成记录 | 达到检查点后后台采样，当前聊天继续运行 |
| 普通用户消息或轮次变化 | 更新会话范围；有待执行任务时拾取，不按每轮固定发题 |
| 空闲或单纯经过一段时间 | 不产生新的采样任务 |
| 关闭自动开关 | 取消自动任务尚未发送的样本，已发请求收尾；手动任务不受影响 |

这里的工具进度指连续监测期间新出现的正常工具完成记录：有调用 ID 时按 ID 去重，没有 ID 时每条完成记录各计一次。超长工具输出采用有界流式读取，只提取结构化类型和调用 ID，不因正文超过 256 KiB 漏计，也不保留正文。它不是会话历史总调用数。首次接管前、应用退出后或切走期间的历史记录不追补，未产生完成记录的中断调用不计入，采样分支的消息和工具也不计入正常工作。纯文字聊天在首次检查后，若没有工具完成记录及配置变化，不会每轮自动采样。切换监测对象可取消旧会话尚未发送的样本；已发请求收尾，结果留在原会话记录中。每次检测结束（包括失败或用户取消），当前周期清零并重新选择下一检查点；旧任务不会清除后来安排的新首检。

## 本地数据与运行设置

`~/Library/Application Support/Model Beacon/` 保存会话绑定、全局自动开关和有限归因历史，最多保留 128 个会话、每会话 32 个结果、256 条任务事件。记录必要配置、候选、时间、结果及原因，不保存凭据或原始数字答案。退役的历史文件不再读写，也不自动删除用户已有文件。

| 环境变量 | 用途 |
| --- | --- |
| `CODEX_HOME` | 指定 Codex 本地数据目录 |
| `MODEL_BEACON_CODEX_PATH` | 指定已安装 Codex 可执行文件的绝对路径；正常自动发现成功时无需设置 |
| `MODEL_BEACON_REFERENCE` | 指定显式导入的 `fingerprints.json` 绝对路径；默认使用随包参考 |
| `MODEL_BEACON_SESSION` | 指定启动时绑定的会话 ID |

从终端启动时传入环境变量；已运行的应用需退出重启后采用新设置。普通用户正常使用无需配置。

## 开发者：源码运行与构建

在 Apple Silicon macOS 上开发，需要 Python 3.12 和 Xcode Command Line Tools。

```bash
python3.12 -m venv .venv-build
source .venv-build/bin/activate
python -m pip install -r packaging/requirements-build.txt
python -B -m unittest discover -s tests -v
python start.py
```

启动器按需构建开发版。开发版需要本机 Python，可用 `PASSIVE_MONITOR_PYTHON` 指定解释器；发布版自带运行时。从 Codex 聊天内启动时可通过 `CODEX_THREAD_ID` 绑定，也可运行 `python start.py --session 会话ID`。启动器通过 `modelbeacon://bind?session=...` 向已运行应用交付绑定请求。

正式构建、打包及校验：

```bash
python build_macos.py --release --arch arm64
python scripts/package_macos.py --version 1.2.3 --arch arm64
(cd dist/packages && shasum -a 256 -c SHA256SUMS.txt)
```

版本集中在 [project.json](project.json)，产物位于 `dist/packages/`。构建校验品牌、架构、最低系统版本、运行时、图标、型号规则、参考数据及法律说明，构建依赖固定在 [requirements-build.txt](packaging/requirements-build.txt)。

### 导入维护者参考库

导入脚本处理已有完整统计参考库，不发模型请求。导入前应确认采集来源和适用配置，SHA-256 用于文件完整性检查。以下目标目录必须尚不存在：

```bash
python scripts/import_fingerprint_reference.py \
  --source assets/fingerprint-gpt-bank.json \
  --sha256 77983ea31d9ae0ecf9f2e58001d5f975c3ea2ae7a85a43989ee4294e3b9429a5 \
  --source-label "随包 GPT 参考" \
  --output .build/imported-reference
MODEL_BEACON_REFERENCE="$PWD/.build/imported-reference/fingerprints.json" python start.py
```

新型号参考使用真实采集数据，并通过同一导入和评分入口验证。采集来源、样本数和配置记录在参考清单中。

### 控制和离线检查

读取器入口仍为 `passive_monitor.py`，只读取采样所需会话元数据。控制输入为逐行 JSON：`bind`、`follow_recent`、`attribute`、`set_auto`、`sample_new`、`cancel_attribution`。归因操作携带当前显示的会话 ID，独立采样携带模型、提供方及后台原生参数，取消操作携带任务 ID。`attribute`、`sample_new` 或开启 `set_auto` 可发起真实模型请求；自动测试使用注入适配器和隔离目录。

原生应用可用 `MODEL_BEACON_SESSIONS_DIR` 和 `MODEL_BEACON_STATE_FILE` 指定合成日志与隔离状态位置。离线预览支持 `--preview-json /absolute/path/to/state.json --theme graphite --expanded --snapshot /absolute/path/to/preview.png`，亮色使用 `--theme silver`；预览不启动读取器或采样。

## 发布与许可

上传源码后，推送与 `project.json` 一致的版本标签，例如 `v1.2.3`，或手动运行 **macOS release**，自动构建并上传安装包到本仓库的 GitHub Releases。操作步骤见 [RELEASING.md](RELEASING.md)。

本项目尚未指定自有源码许可，也未填写维护者或版权身份。随包运行组件及直接复用的算法、参考数据所需声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
