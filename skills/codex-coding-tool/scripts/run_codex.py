#!/usr/bin/env python3
import sys
import os
import subprocess
import argparse

def main():
    parser = argparse.ArgumentParser(description="调用本地codex-cli执行代码相关操作")
    parser.add_argument("--workdir", "--cd", "-C", default=os.getcwd(), help="工作目录")
    parser.add_argument("prompt", nargs="+", help="执行的prompt内容")
    args = parser.parse_args()
    
    prompt = " ".join(args.prompt)
    cmd = [
        "codex", "exec",
        "--cd", args.workdir,
        "--skip-git-repo-check",
        prompt
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    sys.exit(result.returncode)

if __name__ == "__main__":
    main()
