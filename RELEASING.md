# 发布 Model Beacon

当前版本为 `project.json` 中的 `1.2.3`。支持的发布目标为 macOS / Apple Silicon arm64，生成 DMG、应用 ZIP、构建清单和 SHA-256 校验文件。源码仓库不应提交 `.venv-build/`、`.build/`、`dist/` 或任何凭据。

## 首次上传后

1. 在 GitHub 创建自己的空仓库，建议名称 `model-beacon`。将整理后的源码推送到默认分支，保留 `.github/workflows/release.yml`。仓库简介可填写：**Codex 会话数字指纹归因，支持冻结快照后台采样与轻量桌面悬浮工具。**
2. 打开仓库的 **Actions** 页面。如果 GitHub 提示工作流尚未启用，先启用。仓库或组织策略需允许 Actions 使用 GitHub 托管的 macOS runner，并允许发布 job 获得 `contents: write` 权限。工作流使用 GitHub 自动提供的 `GITHUB_TOKEN`，无需创建个人令牌。
3. 选择以下任一方式发布。

**方式一：推送版本标签**

确认当前提交已包含要发布的改动，且 `project.json` 为 `1.2.3`：

```bash
git tag v1.2.3
git push origin v1.2.3
```

**方式二：网页手动运行**

进入 **Actions → macOS release → Run workflow**，选择已推送代码的分支；版本可留空，或填写与 `project.json` 相同的 `1.2.3`。首次手动运行时，工作流会在这次实际构建的提交创建 `v1.2.3` 标签。已有标签若指向其他提交，工作流会停止，不会悄悄替换标签。

4. 等待测试、构建、打包和发布全部成功，然后打开该仓库的 **Releases**。应看到以下四个附件：

```text
model-beacon-1.2.3-macos-arm64.dmg
model-beacon-1.2.3-macos-arm64.zip
model-beacon-1.2.3-macos-arm64-manifest.json
SHA256SUMS.txt
```

README 的下载入口使用 GitHub 同仓相对链接，会随仓库地址自动解析，无需替换用户名。第一次 Release 成功前，最新版下载页没有安装包。Actions 的构建产物也会保留一份，正式下载入口以 Releases 为准。

若无法使用 GitHub macOS runner，可在 Apple Silicon Mac 本地按 README 构建、打包和校验，再在 GitHub 的 **Releases → Draft a new release** 中选择同版本标签，将 `dist/packages/` 中上述四个文件一起上传。普通用户下载 DMG 或 ZIP，无需构建。

## 后续版本

修改 `project.json` 的版本，提交并推送代码，再推送完全一致的 `v版本` 标签。打包命令中的 `--version` 也要相同。不复用已发布版本标签；需要改变已发布内容时递增版本。

## 可选的 Developer ID 签名与 Apple 公证

默认流程不需要维护者凭据，使用 ad-hoc 临时签名，Release 说明和清单会标注“未公证”。签名与公证流程已配置，但尚未使用真实凭据验证。若要启用，在 **Settings → Secrets and variables → Actions** 配置：

| Secret | 内容 |
| --- | --- |
| `MACOS_CERTIFICATE_P12_BASE64` | 含私钥的 Developer ID Application 证书 `.p12`，Base64 编码 |
| `MACOS_CERTIFICATE_PASSWORD` | `.p12` 导出密码 |
| `MACOS_CODESIGN_IDENTITY` | 证书完整签名身份，例如 Developer ID Application 的实际名称 |
| `APPLE_NOTARY_KEY_P8` | App Store Connect API `.p8` 私钥完整内容 |
| `APPLE_NOTARY_KEY_ID` | 对应的 Key ID |
| `APPLE_NOTARY_ISSUER_ID` | 对应的 Issuer ID |

前三项一起启用正式签名；后三项连同前三项一起启用公证。部分配置缺失会直接失败，不会自动退回未公证发布。临时钥匙串和私钥在 job 结束时清理；请勿将这些内容提交到源码。

公证会将签名后的应用和 DMG 提交给 Apple，等待接受结果并装订票据；只有全部成功，清单才记录 `notarized: true`。发布后仍需在另一台 Mac 验证下载、安装与启动。

## 发布前检查

- 运行 README 中的测试、release 构建、打包和校验命令；确认 ZIP 解压与 DMG 内均为 `Model Beacon.app`。
- 检查版本、图标、型号配置和 Release 附件名称；普通用户启动不应依赖源码目录或系统 Python。
- 检查单击信息卡、减号收起、顶部手动按钮和右键原生菜单；自动归因及主题选中时显示原生勾选。主卡展示当前配置模型与返回模型，记录菜单仅保留七项，执行进度区分初检与复测。
- 核对统一 9 型号参考库及其资源路径、哈希和必要许可；安装包只包含一份参考库，所有型号使用同一评分入口。
- 在明确授权的目标会话验证并发快照采样：原会话运行时仍可归因，源会话不发送 `turn/start`；核对模型、提供方、原生参数和冻结轮次，检查取消、仅清理自有快照、进程收尾和单任务隔离。
- 检查新一批初检不继承历史复测状态；手动取消后显示“正在取消／已取消”，立即停止本工具自有采样进程，不等待采样超时，也不影响源会话。
- 检查首次匹配只发 1 份；初始候选差异立即提醒，再在同一快照完成 3 份独立复测，最多 4 次 `turn/start`。全部复测均不匹配才升级为持续差异，其中出现匹配也保留初始差异记录；缺少适用参考时不追加复测。请求上限不等于底层 HTTP 次数保证。记录实际结果到 VALIDATION.md，离线测试不算真实模型准确率验证。
- 当前源码未指定项目许可。若计划以开源许可授权他人使用、修改和分发，发布前由维护者决定许可及版权身份；随包 Python 运行时、直接复用的参考数据和评分实现所需版权及许可随安装包保留，详见 THIRD_PARTY_NOTICES.md。
