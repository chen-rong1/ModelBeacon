#!/usr/bin/env python3
"""Build if needed, then open the local macOS floating monitor."""
import platform
import os
import subprocess
import sys
import argparse
import re
from urllib.parse import urlencode

from build_macos import APP_PATH, needs_build, build


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="打开模型灯塔，并可绑定明确的 Codex 会话。")
    parser.add_argument("--session", help="绑定会话唯一 ID；运行中的应用也会接收本次绑定")
    args = parser.parse_args(argv)
    if platform.system() != "Darwin":
        print("模型灯塔的桌面窗口需要 macOS。", file=sys.stderr)
        return 1
    try:
        if needs_build():
            build()
        command = ["/usr/bin/open"]
        session = args.session or os.environ.get("MODEL_BEACON_SESSION") or os.environ.get("CODEX_THREAD_ID")
        if session and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", session):
            raise ValueError("会话 ID 格式无效。")
        if session:
            command.extend(["--env", "MODEL_BEACON_SESSION=" + session])
        for key in ("CODEX_HOME", "PASSIVE_MONITOR_PYTHON", "MODEL_BEACON_CODEX_PATH", "MODEL_BEACON_REFERENCE"):
            if os.environ.get(key):
                command.extend(["--env", key + "=" + os.environ[key]])
        if session:
            # URL delivery reaches an already-running application. Environment
            # variables alone are read only by a newly launched process.
            command.extend(["-a", str(APP_PATH), "modelbeacon://bind?" + urlencode({"session": session})])
        else:
            command.append(str(APP_PATH))
        subprocess.run(command, check=True)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print("启动失败：{}".format(error), file=sys.stderr)
        return 1
    print("已打开：{}".format(APP_PATH))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
