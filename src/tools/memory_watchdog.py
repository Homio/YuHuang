"""YuHuang Backend — 硬内存限制守护进程

作为独立子进程运行，监控后端主进程的 RSS 内存占用。
当超过限制时，立即 SIGKILL 主进程并发送桌面通知。

这是一个"防呆"机制：即使后端内部有未知内存泄漏，
守护进程也会在泄漏到限制值之前截停，防止 OOM 杀死其他进程。

设计原则:
  - 零依赖：只使用 Python 标准库 + /proc
  - 低开销：每 2 秒轮询一次 VmRSS，无网络/无磁盘 I/O
  - 独立可靠：不依赖后端任何内部状态，完全独立运行
"""
import os
import sys
import time
import subprocess
import argparse


def get_vmrss_kb(pid: int) -> int:
    """读取 /proc/<pid>/status 中的 VmRSS（单位 KB）"""
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    # "VmRSS:   123456 kB"
                    return int(line.split()[1])
    except (IOError, OSError, ValueError, IndexError):
        pass
    return -1


def send_notification(
    title: str,
    body: str,
    urgency: str = "critical",
    expire: int = 0,
):
    """发送桌面通知（使用 notify-send，静默失败）"""
    try:
        subprocess.run(
            ["notify-send", f"-u{urgency}", f"-t{expire}", title, body],
            timeout=5,
            capture_output=True,
        )
    except Exception:
        pass  # 通知失败不影响守护进程功能


def watchdog(pid: int, limit_mb: int, restart_cmd: list[str] | None = None):
    """守护进程主循环

    Args:
        pid: 要监控的进程 ID（后端主进程）
        limit_mb: 内存限制（MB），超过此值触发行动
        restart_cmd: 可选的重启命令（列表形式），杀死后执行
    """
    limit_kb = limit_mb * 1024
    poll_interval = 2.0  # 秒
    peak_rss_kb = 0

    # ENOENT 持续计数：判断是暂时不可读还是进程已死
    enoent_count = 0

    while True:
        time.sleep(poll_interval)
        rss_kb = get_vmrss_kb(pid)

        if rss_kb < 0:
            enoent_count += 1
            # 连续 5 次 /proc/pid/status 不可读 → 认为进程已结束
            if enoent_count >= 5:
                sys.exit(0)
            continue
        enoent_count = 0

        if rss_kb > peak_rss_kb:
            peak_rss_kb = rss_kb

        if rss_kb <= limit_kb:
            continue

        # ═══ 内存超限！═══
        rss_mb = rss_kb // 1024

        # 1) 尝试发送桌面通知（SIGKILL 之前）
        send_notification(
            title="⚠️ YuHuang 内存超限 — 正在强制重启",
            body=(
                f"后端进程已使用 {rss_mb}MB 内存\n"
                f"超过限制 {limit_mb}MB\n\n"
                f"已自动重启，如有疑问请提交 Issue"
            ),
            urgency="critical",
        )

        # 休眠 1 秒让通知有机会送达
        time.sleep(1.0)

        # 2) 发送 SIGKILL
        try:
            os.kill(pid, 9)
        except OSError:
            pass

        # 3) 执行重启命令
        if restart_cmd:
            try:
                subprocess.Popen(
                    restart_cmd,
                    start_new_session=True,
                )
            except Exception:
                pass

        # 4) 提交 issue 弹窗
        send_notification(
            title="🔍 YuHuang 内存超限 — 是否提交 Issue？",
            body=(
                f"后端因内存超限 ({rss_mb}MB > {limit_mb}MB) "
                f"被守护进程强制重启。\n"
                f"点击通知打开 GitHub Issue 页面。"
            ),
            urgency="normal",
            expire=15000,  # 15 秒后自动消失
        )

        sys.exit(0)


def main():
    parser = argparse.ArgumentParser(
        description="YuHuang Memory Watchdog — 硬内存限制守护进程"
    )
    parser.add_argument(
        "--pid",
        type=int,
        required=True,
        help="要监控的进程 PID",
    )
    parser.add_argument(
        "--limit-mb",
        type=int,
        default=5120,
        help="内存限制（MB），默认 1024（1GB）",
    )
    parser.add_argument(
        "--restart-cmd",
        type=str,
        default="",
        help='杀死后执行的重启命令（如: "yuhuang-backend --memory-limit 1024"）',
    )

    args = parser.parse_args()
    restart_cmd = (
        args.restart_cmd.split() if args.restart_cmd.strip() else None
    )

    watchdog(
        pid=args.pid,
        limit_mb=args.limit_mb,
        restart_cmd=restart_cmd,
    )


if __name__ == "__main__":
    main()
