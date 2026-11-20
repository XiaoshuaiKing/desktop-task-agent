"""手动检查遗留任务。

    python _check.py          # 只扫描，不改库（dry-run）
    python _check.py --mark   # 扫描并标记为 INTERRUPTED

注意：只标记、不重跑。重跑可能让已经生效的外部动作再发生一次。
"""
import argparse

from core.recovery import format_report, mark_stale_interrupted


def main():
    parser = argparse.ArgumentParser(description="检查进程重启后遗留的 Agent 任务")
    parser.add_argument("--mark", action="store_true", help="真正写入标记（默认只扫描）")
    args = parser.parse_args()
    report = mark_stale_interrupted(dry_run=not args.mark)
    print(format_report(report))


if __name__ == "__main__":
    main()
