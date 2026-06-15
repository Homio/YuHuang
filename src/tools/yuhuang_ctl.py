"""YuHuang control tool — mic toggle, service management, status query"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

YUHUANG_DIR = os.path.expanduser("~/.config/yuhuang")
LOG_FILE = os.path.join(YUHUANG_DIR, "backend.log")
LOG_MAX_SIZE = 2 * 1024 * 1024   # 2 MB
LOG_MAX_FILES = 5                 # keep backend.log + backend.log.1~4


def get_socket_path() -> str:
    config_path = os.path.join(YUHUANG_DIR, "config.yaml")
    if os.path.exists(config_path):
        try:
            import yaml
            with open(config_path) as f:
                config = yaml.safe_load(f)
                return config.get("backend", {}).get(
                    "socket_path", "/tmp/yuhuang-backend.sock")
        except Exception:
            pass
    return "/tmp/yuhuang-backend.sock"


def _rotate_logs():
    """Rotate log files: keep LOG_MAX_FILES copies, each up to LOG_MAX_SIZE."""
    if not os.path.exists(LOG_FILE):
        return
    if os.path.getsize(LOG_FILE) < LOG_MAX_SIZE:
        return

    # Shift: backend.log.N → backend.log.(N+1), oldest dropped
    oldest = LOG_FILE + f".{LOG_MAX_FILES - 1}"
    if os.path.exists(oldest):
        os.remove(oldest)
    for i in range(LOG_MAX_FILES - 2, -1, -1):
        src = LOG_FILE if i == 0 else LOG_FILE + f".{i}"
        dst = LOG_FILE + f".{i + 1}"
        if os.path.exists(src):
            shutil.move(src, dst)

    # Now LOG_FILE is gone (moved to .1), create empty placeholder
    # so the backend can write to it after restart
    Path(LOG_FILE).touch()


def _find_backend_pids():
    """Return list of running yuhuang-backend PIDs (excluding grep)."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "yuhuang-backend"],
            capture_output=True, text=True, timeout=3
        )
        return [p.strip() for p in result.stdout.strip().split('\n') if p.strip()]
    except Exception:
        return []


def send_command(command: dict, silent: bool = False) -> dict:
    """Send a JSON command to the backend via Unix socket.

    Returns the response dict, or {} on failure.
    When silent=True, errors are not printed (caller handles them).
    """
    import socket
    import struct

    sock_path = get_socket_path()
    if not os.path.exists(sock_path):
        if not silent:
            print(f"Error: Backend not running (socket not found: {sock_path})")
        return {}

    data = json.dumps(command).encode("utf-8")
    length = struct.pack("!I", len(data))

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(sock_path)
        sock.sendall(length + data)

        resp_len_data = sock.recv(4)
        if len(resp_len_data) < 4:
            return {}
        resp_len = struct.unpack("!I", resp_len_data)[0]
        if resp_len == 0:
            return {}

        resp_data = b""
        while len(resp_data) < resp_len:
            chunk = sock.recv(resp_len - len(resp_data))
            if not chunk:
                break
            resp_data += chunk

        return json.loads(resp_data.decode("utf-8"))
    except Exception as e:
        if not silent:
            print(f"Error communicating with backend: {e}")
        return {}
    finally:
        sock.close()


def cmd_status(args):
    sock_path = get_socket_path()
    sock_exists = os.path.exists(sock_path)
    pids = _find_backend_pids()

    print("YuHuang Status")
    print("=" * 48)

    # --- Backend status ---
    if pids:
        if sock_exists:
            resp = send_command({"type": "ping"}, silent=True)
            if resp:
                print(f"Backend:  Running OK  (PID: {pids[0]})")
            else:
                print(f"Backend:  Running but not responding  (PID: {pids[0]})")
        else:
            print(f"Backend:  Process exists but socket missing (PID: {pids[0]})")
    else:
        if sock_exists:
            print("Backend:  Socket exists but no process (stale socket)")
        else:
            print("Backend:  Not running")

    print(f"Socket:   {sock_path}")

    # --- Log file ---
    if os.path.exists(LOG_FILE):
        size_kb = os.path.getsize(LOG_FILE) / 1024
        print(f"Log:      {LOG_FILE}  ({size_kb:.1f} KB)")
    else:
        print(f"Log:      {LOG_FILE}  (not present)")

    # --- fcitx5 IM status ---
    print()
    try:
        result = subprocess.run(
            ["fcitx5-remote", "-n"],
            capture_output=True, text=True, timeout=2
        )
        if result.returncode == 0:
            current_im = result.stdout.strip()
            print(f"Current IM: {current_im}")
            if "yuhuang" in current_im.lower():
                print("YuHuang:   Active")
            else:
                print("YuHuang:   Not active (switch with: fcitx5-remote -s yuhuang)")
        else:
            print("fcitx5:     Not running or fcitx5-remote not found")
    except Exception:
        print("fcitx5:     Not detected")


def cmd_toggle(args):
    send_command({"type": "toggle"})
    print("Toggle command sent")


def cmd_reset(args):
    send_command({"type": "reset"})
    print("Reset command sent")


def cmd_mic(args):
    try:
        import sounddevice as sd
        devices = sd.query_devices()

        # If a device name is provided, set it
        if args.device:
            # Find matching device
            matched = None
            for i, dev in enumerate(devices):
                if dev["max_input_channels"] > 0 and args.device.lower() in dev["name"].lower():
                    matched = dev["name"]
                    break
            if matched:
                print(f"Setting audio device to: {matched}")
                resp = send_command({
                    "type": "config",
                    "audio_device": matched,
                })
                if resp:
                    print("Device updated (hot-swapped, no restart needed)")
                else:
                    print("Config sent (backend will hot-swap)")
            else:
                print(f"No input device found matching '{args.device}'")
                print("Available devices:")
                _list_mic_devices(devices)
            return

        # Just list devices
        _list_mic_devices(devices)

    except ImportError:
        print("sounddevice not installed. Install with: pip install sounddevice")
    except Exception as e:
        print(f"Error: {e}")


def _list_mic_devices(devices):
    """Print available input devices in a copy-friendly format"""
    import sounddevice as sd
    print("Available Audio Input Devices")
    print("=" * 70)
    default_name = sd.default.device[0] if sd.default.device else ""
    for i, dev in enumerate(devices):
        if dev["max_input_channels"] > 0:
            marker = "  ← default" if dev["name"] == default_name else ""
            print(f"  [{i}] {dev['name']}{marker}")
    print()
    print("Copy a device name and paste into:")
    print("  fcitx5-configtool → 附加组件 → YuHuang → 配置 → AudioDevice")
    print()
    print("Or set directly:")
    print('  yuhuang-ctl mic "<device name part>"')


def cmd_start(args):
    sock_path = get_socket_path()
    if os.path.exists(sock_path):
        print("Backend may already be running. Trying to stop first...")
        cmd_stop(args)
        time.sleep(0.5)

    try:
        subprocess.run(["which", "yuhuang-backend"],
                       capture_output=True, check=True)
    except subprocess.CalledProcessError:
        print("yuhuang-backend not found in PATH.")
        print("Install with: pip install -e /path/to/yuhuang")
        return

    # Ensure config dir exists
    os.makedirs(YUHUANG_DIR, exist_ok=True)

    # Rotate logs before starting (if log is too big)
    _rotate_logs()

    config_path = args.config if args.config else ""
    cmd = ["yuhuang-backend", "--log-file", LOG_FILE]
    if config_path:
        cmd.extend(["-c", config_path])

    print("Starting yuhuang-backend...")
    if args.foreground:
        os.execvp(cmd[0], cmd)
        return  # unreachable

    # Backend manages its own log file (auto-reopens if deleted).
    # We don't redirect stdout/stderr — the backend's _ReopeningFileWriter handles it.
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,   # backend writes to file via its own handler
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    print(f"Process started (PID: {process.pid})")

    # Wait for socket to appear (models load ~10-30s on GPU)
    print("Waiting for models to load", end="", flush=True)
    MAX_WAIT = 45
    for i in range(MAX_WAIT):
        if os.path.exists(sock_path):
            print("\nBackend is ready!")
            return
        print(".", end="", flush=True)
        time.sleep(1)
    print()

    # Timeout — check if process is still alive
    if process.poll() is None:
        print(f"Backend still loading (PID: {process.pid}). "
              f"Models may take longer on first run.")
        print(f"Monitor progress: tail -f {LOG_FILE}")
    else:
        print(f"Backend exited with code {process.returncode}. "
              f"Check log: {LOG_FILE}")


def cmd_restart(args):
    print("Restarting backend...")
    cmd_stop(args)
    # Wait for old process to fully exit + socket cleanup
    time.sleep(1)
    args.foreground = getattr(args, 'foreground', False)
    args.config = getattr(args, 'config', '')
    cmd_start(args)


def cmd_stop(args):
    sock_path = get_socket_path()

    # Remove stale socket first
    if os.path.exists(sock_path):
        try:
            os.unlink(sock_path)
        except OSError:
            pass

    pids = _find_backend_pids()
    if not pids:
        print("Backend is not running")
        return

    # Send SIGTERM
    for pid in pids:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except ProcessLookupError:
            continue
    print(f"Sent SIGTERM to PID(s): {', '.join(pids)}")

    # Wait up to 5s for graceful shutdown
    for _ in range(50):
        remaining = _find_backend_pids()
        if not remaining:
            print("Backend stopped")
            return
        time.sleep(0.1)

    # Force kill stragglers
    remaining = _find_backend_pids()
    if remaining:
        for pid in remaining:
            try:
                os.kill(int(pid), signal.SIGKILL)
            except ProcessLookupError:
                continue
        print(f"Force-killed PID(s): {', '.join(remaining)}")


def cmd_switch(args):
    try:
        subprocess.run(
            ["fcitx5-remote", "-s", "yuhuang"],
            check=True, timeout=2
        )
        print("Switched to YuHuang input method")
    except FileNotFoundError:
        print("fcitx5-remote not found. Is fcitx5 installed?")
    except subprocess.CalledProcessError:
        print("Failed to switch. Make sure YuHuang addon is installed.")


def cmd_gpu(args):
    """Detect GPU and allow selecting/saving CUDA device"""
    import yaml

    config_path = os.path.expanduser("~/.config/yuhuang/config.yaml")

    # Detect GPU
    try:
        import torch
        cuda_available = torch.cuda.is_available()
    except ImportError:
        cuda_available = False

    print("GPU Detection")
    print("=" * 50)

    if cuda_available:
        count = torch.cuda.device_count()
        print(f"CUDA available: {count} GPU(s)")
        for i in range(count):
            name = torch.cuda.get_device_name(i)
            mem = torch.cuda.get_device_properties(i).total_memory // (1024**3)
            print(f"  cuda:{i}  →  {name}  ({mem} GiB)")
    else:
        print("CUDA: Not available")

    try:
        import torch
        if torch.backends.mps.is_available():
            print("MPS: Available (Apple Silicon GPU)")
    except (ImportError, AttributeError):
        pass

    print(f"\nCPU: Available (fallback)")

    # Read current config
    config = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                config = yaml.safe_load(f) or {}
        except Exception:
            pass

    current = config.get("asr", {}).get("device", "cuda")
    print(f"\nCurrent setting: {current}")

    # Set new device
    if args.device:
        new_device = args.device.lower()
        config.setdefault("asr", {})["device"] = new_device
        try:
            import yaml as _yaml
            with open(config_path, "w") as f:
                _yaml.safe_dump(config, f, default_flow_style=False,
                               allow_unicode=True, sort_keys=False)
            print(f"Updated: device={new_device}")
            print(f"Saved to: {config_path}")
            print("\nRestart backend for changes to take effect:")
            print("  yuhuang-ctl restart")
        except Exception as e:
            print(f"Failed to save config: {e}")
    else:
        print()
        print("Set device (examples):")
        print("  yuhuang-ctl gpu cuda      # Use NVIDIA GPU 0 (default)")
        print("  yuhuang-ctl gpu cuda:1    # Use NVIDIA GPU 1")
        print("  yuhuang-ctl gpu mps       # Use Apple Silicon GPU")
        print("  yuhuang-ctl gpu cpu       # Use CPU only")


def main():
    parser = argparse.ArgumentParser(
        prog="yuhuang-ctl",
        description="YuHuang voice input method control tool"
    )
    subparsers = parser.add_subparsers(dest="command", help="Command")

    subparsers.add_parser("status", help="Show YuHuang status")
    start_parser = subparsers.add_parser("start", help="Start backend service")
    start_parser.add_argument("-c", "--config", help="Config file path")
    start_parser.add_argument("-f", "--foreground", action="store_true",
                               help="Run in foreground")
    subparsers.add_parser("stop", help="Stop backend service")
    restart_parser = subparsers.add_parser("restart", help="Restart backend service")
    restart_parser.add_argument("-c", "--config", help="Config file path")
    subparsers.add_parser("toggle", help="Toggle listening pause/resume")
    subparsers.add_parser("reset", help="Reset current recognition state")
    mic_parser = subparsers.add_parser("mic", help="List or set audio input device")
    mic_parser.add_argument("device", nargs="?", default=None,
                            help="Device name to search and set (partial match)")
    subparsers.add_parser("switch", help="Switch to YuHuang input method")
    gpu_parser = subparsers.add_parser("gpu", help="Detect GPU and set CUDA device")
    gpu_parser.add_argument("device", nargs="?", default=None,
                            help="Device to use: cuda, cuda:0, cuda:1, mps, cpu")

    args = parser.parse_args()

    commands = {
        "status": cmd_status,
        "start": cmd_start,
        "stop": cmd_stop,
        "restart": cmd_restart,
        "toggle": cmd_toggle,
        "reset": cmd_reset,
        "mic": cmd_mic,
        "switch": cmd_switch,
        "gpu": cmd_gpu,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
