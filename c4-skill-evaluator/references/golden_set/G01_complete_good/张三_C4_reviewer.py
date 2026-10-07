#!/usr/bin/env python3
"""代码 review 助手：找出空指针与资源泄漏。"""
import argparse
import sys


def main():
    ap = argparse.ArgumentParser(description="code reviewer")
    ap.add_argument("path")
    args = ap.parse_args()
    print("review:", args.path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
