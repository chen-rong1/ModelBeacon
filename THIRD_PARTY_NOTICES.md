# 第三方组件与参考数据

本文件说明随 Model Beacon 分发的第三方部分，不为本项目自有源码指定许可或虚构维护者身份。

## CPython 与构建引导程序

发布版内置 CPython 3.12 运行时及标准库。完整许可和标准库相关声明见 [Python-LICENSE.txt](packaging/licenses/Python-LICENSE.txt)，随应用保存在 `Contents/Resources/Legal/licenses/`。

读取与采样程序由 PyInstaller 打包。PyInstaller 引导程序适用其分发例外，不改变本项目源码、Python 运行时或参考数据各自的许可。PyInstaller 及其他构建依赖仅用于开发和构建，不要求最终用户安装。

## 数字评分实现与参考数据

`fingerprint_reference.py` 中的 nuisance-Hellinger／ordered-block 数字特征与评分实现，及统一参考库 `assets/fingerprint-gpt-bank.json` 中原有 8 个 GPT 型号的数据，使用 ModelTrace 的 MIT 授权部分。版权为 **Copyright (c) 2026 xqy2006**，完整版权、许可及免责声明保存在 [ModelTrace-LICENSE.txt](packaging/licenses/ModelTrace-LICENSE.txt)，并随应用放在 `Contents/Resources/Legal/licenses/`。

来源为 [ModelTrace 固定版本](https://github.com/xqy2006/ModelTrace/tree/6cd4a8ff0f3f4e49d6d6969dd2fbadfa1569fde2)的 `data/gpt_bank.json`、`data/gpt_reference.jsonl` 和 `codex-plugin/modeltrace-guard/scripts/fingerprint-core.mjs`。原始参考库文件 SHA-256：

```text
94229c6601429c916011b1ce45ac1a87d6ad0d6914368038d0d069effe7201a1
```

随包统一参考库包含 9 个型号、291 份响应：原有 8 型号各 36 份，新增 `gpt-6.1-sol` 的 3 份真实采集响应。新增响应于 2026-10-28 通过明确配置的 Codex 临时分支采集，采集配置为 `gpt-6.1-sol / openai / ultra`，共 900 个数字；独立检查输出未参与拟合。所有型号使用同一评分入口，来源及样本信息记录在 `fingerprints.json`，资源不含原聊天。

当前统一参考库 SHA-256：

```text
77983ea31d9ae0ecf9f2e58001d5f975c3ea2ae7a85a43989ee4294e3b9429a5
```

## 本机 Codex

数字采样调用用户已经安装的 Codex 可执行文件及其原生账号连接；安装包不捆绑 Codex 程序、账号凭据或访问令牌。Codex 的使用与许可适用其自身条款。
