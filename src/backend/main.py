"""YuHuang backend service entry point"""
import asyncio
import signal
import sys
import os
import time
import logging
import logging.handlers
import argparse
import json
from pathlib import Path

# Strip proxy env vars to avoid SOCKS proxy interfering with urllib3/httpx
# (funasr's AutoModel does a PyPI version check that hangs on SOCKS)
for _key in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy",
             "HTTPS_PROXY", "https_proxy", "SOCKS_PROXY", "socks_proxy"):
    os.environ.pop(_key, None)

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.asr_engine import ASREngine
from backend.llm_optimizer import LLMOptimizer
from backend.audio_capture import AudioCapture
from backend.unix_server import UnixSocketServer

logger = logging.getLogger("yuhuang")


class _ReopeningFileWriter:
    """File-like object that writes to a log file and reopens it if deleted.

    Used to redirect sys.stdout/stderr so that even C library output
    (funasr debug prints) survives log file deletion.
    """
    def __init__(self, path: str, fallback=None):
        self._path = path
        self._fallback = fallback  # backup stream (e.g. original stderr)
        self._fd = -1
        self._open()

    def _open(self):
        if self._fd >= 0:
            try:
                os.close(self._fd)
            except OSError:
                pass
        try:
            self._fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        except OSError:
            self._fd = -1

    def write(self, data):
        if self._fd >= 0:
            try:
                os.write(self._fd, data.encode() if isinstance(data, str) else data)
                return
            except (OSError, ValueError):
                pass
        # File gone or fd invalid — reopen and retry
        self._open()
        if self._fd >= 0:
            try:
                os.write(self._fd, data.encode() if isinstance(data, str) else data)
                return
            except (OSError, ValueError):
                pass
        # Last resort: fallback stream
        if self._fallback:
            self._fallback.write(data)

    def flush(self):
        if self._fd >= 0:
            try:
                os.fsync(self._fd)
            except OSError:
                pass

    def fileno(self):
        return self._fd if self._fd >= 0 else (
            self._fallback.fileno() if self._fallback else -1)


def setup_logging(level: str = "INFO", log_file: str = ""):
    fmt = "[%(asctime)s] %(name)s %(levelname)s: %(message)s"
    formatter = logging.Formatter(fmt)

    # 1) If log file requested, redirect raw stdout/stderr FIRST so the
    #    StreamHandler below captures the file writer, not /dev/null.
    if log_file:
        try:
            fw = _ReopeningFileWriter(log_file, fallback=sys.__stderr__)
            sys.stdout = fw
            sys.stderr = fw
        except Exception:
            pass

    # 2) Build handlers (StreamHandler now points to the file writer)
    handlers = [logging.StreamHandler(sys.stderr)]

    # Also add a WatchedFileHandler so logging module writes even if
    # stdout/stderr are monkey-patched away by another library.
    if log_file:
        try:
            fh = logging.handlers.WatchedFileHandler(log_file)
            fh.setFormatter(formatter)
            handlers.append(fh)
        except Exception:
            pass

    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format=fmt,
        handlers=handlers,
        force=True,
    )


def load_config(config_path: str) -> dict:
    try:
        import yaml
        with open(config_path, "r") as f:
            return yaml.safe_load(f)
    except Exception as e:
        logger.warning(f"Failed to load config from {config_path}: {e}")
        return {}


def main():
    parser = argparse.ArgumentParser(description="YuHuang Backend Service")
    parser.add_argument("-c", "--config",
                        default=os.path.expanduser("~/.config/yuhuang/config.yaml"),
                        help="Config file path")
    parser.add_argument("-l", "--log-file",
                        default=os.path.expanduser("~/.config/yuhuang/backend.log"),
                        help="Log file path (auto-reopened if deleted)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable verbose logging")
    args = parser.parse_args()

    setup_logging("DEBUG" if args.verbose else "INFO", log_file=args.log_file)
    config = load_config(args.config)

    socket_path = config.get("backend", {}).get(
        "socket_path", "/tmp/yuhuang-backend.sock")

    logger.info("=" * 50)
    logger.info("YuHuang Backend v0.2.0")
    logger.info("=" * 50)

    # ---- Init modules ----
    asr_config = config.get("asr", {})
    audio_config = config.get("audio", {})
    llm_config = config.get("llm", {})

    # ASR engine
    logger.info("Loading ASR models...")
    try:
        asr_engine = ASREngine(
            online_model=asr_config.get("online_model", "paraformer-zh-streaming"),
            offline_model=asr_config.get("offline_model",
                "damo/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"),
            vad_model=asr_config.get("vad_model", "fsmn-vad"),
            punc_model=asr_config.get("punc_model", "ct-punc"),
            sample_rate=audio_config.get("sample_rate", 16000),
            intermediate_interval=asr_config.get("intermediate_interval", 0.3),
            device=asr_config.get("device", "cuda"),
        )
        logger.info("ASR models loaded successfully")
    except Exception as e:
        logger.error(f"Failed to load ASR models: {e}")
        logger.info("Continuing without ASR - mock mode")
        asr_engine = None

    # LLM optimizer (initially disabled, enabled by fcitx5 config)
    llm_optimizer = None
    if llm_config.get("enabled", False):
        llm_optimizer = LLMOptimizer(
            base_url=llm_config.get("base_url", "http://localhost:8000/v1"),
            api_key=llm_config.get("api_key", ""),
            model=llm_config.get("model", "qwen2.5-7b-instruct"),
            temperature=llm_config.get("temperature", 0.3),
            max_tokens=llm_config.get("max_tokens", 2000),
            system_prompt=llm_config.get("system_prompt", ""),
            optimize_delay=llm_config.get("optimize_delay", 0.5),
            auto_commit_delay=llm_config.get("auto_commit_delay", 0.2),
        )
        logger.info(f"LLM optimizer configured: {llm_config.get('model', 'unknown')}")
    else:
        logger.info("LLM optimization disabled in config (can be enabled via fcitx5 GUI)")

    # Audio capture — 完全由 PTT 按键控制，无需音量阈值/VAD
    audio_capture = AudioCapture(
        sample_rate=audio_config.get("sample_rate", 16000),
        channels=audio_config.get("channels", 1),
        frame_size=audio_config.get("frame_size", 4800),
        device=audio_config.get("device", None) or None,
    )

    # ---- PTT 流式管道: 三层流水线 ----

    class PTTStreamPipeline:
        """PTT 语音识别流水线:

        流式模型 (paraformer-zh-streaming): 实时预览，不参与上屏决策
        离线模型 (SenseVoiceSmall):    权威文本，连续两次结果 LCP=稳定→上屏
        候选框:                          仅显示未稳定尾巴（≤200字）

        设计理念:
        - 流式模型精度有限，仅用于预览
        - 离线纠正连续两次 LCP 前缀 = 已稳定 → 提交上屏
        - 候选框只保留最近不稳定部分，长语音不堆积
        - 松键时 SenseVoiceSmall 最终结果提交全部剩余
        """

        MIN_STABLE_LEN = 3      # 最小稳定前缀长度（连续两次离线结果LCP≥此值才提交）
        CANDIDATE_WARN = 500    # 候选框超过此长度输出告警（帮助判断离线纠正频率是否够）

        def __init__(self):
            self.reset()

        def reset(self):
            self._raw = ""               # 当前最佳文本
            self._committed_text = ""    # 已上屏文本内容（用于去重）
            self._prev_text = ""         # 上一次流式中间结果（防重复）
            self._prev_offline_text = "" # 上一次离线纠正文本（用于LCP稳定性检测）
            self._offline_gen = -1       # 最新离线结果版本号
            self._offline_len = 0        # 最新离线文本长度
            self._done = False
            # LCP 双重确认：防止单次大跨度跳涨直接提交
            self._prev_lcp_above = False

        async def on_intermediate(self, text: str):
            """流式模型中间结果 → 仅更新候选框预览，不上屏"""
            if not text:
                return
            self._apply_text(text)

        async def on_offline_correction(self, text: str, generation: int):
            """离线模型纠正结果 → LCP 判定稳定性 → 提交 + 候选框"""
            if not text or generation <= self._offline_gen:
                return
            self._offline_gen = generation
            self._offline_len = len(text)

            logger.info(
                f"PTT pipeline: offline correction "
                f"(#{generation}, {len(text)} chars): {text[:80]}..."
            )

            # ── LCP 稳定性判定 ──
            # 首次纠正仅设基线不上屏，后续每次与前次对比 LCP。
            # ★ 双重确认：要求连续两次离线纠正的 LCP 都超过 committed 才提交
            # 防止模型剧烈修订后单次 LCP 跳涨就提交大段文本
            if not self._prev_offline_text:
                # 首次纠正：仅设基线，不提交
                self._prev_offline_text = text
                logger.info(
                    "PTT pipeline: first correction, setting baseline "
                    f"({len(text)} chars), no commit yet"
                )
            else:
                lcp = 0
                max_lcp = min(len(text), len(self._prev_offline_text))
                while lcp < max_lcp and text[lcp] == self._prev_offline_text[lcp]:
                    lcp += 1

                logger.info(
                    f"PTT pipeline: LCP(prev, current) = {lcp}, "
                    f"committed={self._committed_len}"
                )

                # 模型修订早期文本 → committed_len 来自实际已提交文字，不回退
                # 只是 LCP 暂时小于已提交长度，继续等 LCP 追上来
                if lcp <= self._committed_len:
                    logger.info(
                        f"PTT pipeline: model still revising, "
                        f"LCP={lcp} <= committed={self._committed_len}, "
                        f"waiting for stability"
                    )
                    self._prev_lcp_above = False
                else:
                    new_stable = text[self._committed_len:lcp]
                    if len(new_stable.strip()) >= self.MIN_STABLE_LEN:
                        # ★ 双重确认：要求连续两次离线纠正的 LCP 都超过 committed
                        # 防止模型剧烈修订后单次 LCP 跳涨就提交大段文本
                        if self._prev_lcp_above:
                            await self._try_commit(new_stable.strip())
                            self._prev_lcp_above = (
                                lcp > self._committed_len
                            )  # 提交后 committed 已增长，重新判定
                        else:
                            logger.info(
                                f"PTT pipeline: LCP recovered ({lcp} > "
                                f"{self._committed_len}), "
                                f"awaiting next confirmation"
                            )
                            self._prev_lcp_above = True
                    else:
                        self._prev_lcp_above = True

                self._prev_offline_text = text

            # ── 候选框：未稳定尾巴，长度由 LCP 自然决定 ──
            candidate = text[self._committed_len:]
            if candidate:
                if len(candidate) > self.CANDIDATE_WARN:
                    logger.warning(
                        f"Candidate box large: {len(candidate)} chars "
                        f"(offline corrections may be too slow)"
                    )
                await self._show(candidate)

        def _apply_text(self, text: str):
            """流式中间结果：仅更新 _raw 和候选框预览。

            不参与上屏决策。上屏由 on_offline_correction（LCP稳定性）负责。
            """
            if text == self._prev_text:
                return

            self._raw = text
            self._prev_text = text

            # 候选框：未上屏部分，长度由 LCP 稳定性自然决定
            committed_len = len(self._committed_text)
            candidate = text[committed_len:] if committed_len < len(text) else ""
            if candidate:
                if len(candidate) > self.CANDIDATE_WARN:
                    logger.warning(
                        f"Candidate box large: {len(candidate)} chars "
                        f"(offline corrections may be too slow)"
                    )
                asyncio.create_task(self._show(candidate))

        async def _try_commit(self, text: str):
            """提交文本（带去重：已上屏部分不重复提交）"""
            if not text or not text.strip():
                return
            text = text.strip()

            # 去重：找出与已提交文本不重叠的部分
            commit_text = text
            if self._committed_text:
                # 找后缀重叠：committed_text 尾部 = commit_text 头部
                max_ol = min(len(self._committed_text), len(text))
                for ol in range(max_ol, 0, -1):
                    if self._committed_text.endswith(text[:ol]):
                        commit_text = text[ol:]
                        break
                if not commit_text.strip():
                    return  # 完全重叠，跳过

            if llm_optimizer:
                try:
                    refined = await llm_optimizer.optimize(commit_text)
                    if refined:
                        commit_text = refined
                except Exception:
                    pass

            await self._commit(commit_text)
            self._committed_text += commit_text

        @property
        def _committed_len(self):
            return len(self._committed_text)

        async def finalize(self):
            """松开按键：最后一次离线纠正 + 提交全部剩余"""
            self._done = True

            # 获取最佳文本: 优先离线纠正，否则流式累积
            if asr_engine:
                offline_text, _ = asr_engine.get_offline_text()
                if offline_text:
                    self._raw = offline_text
                else:
                    self._raw = asr_engine.get_accumulated_text() or self._raw

            # 最后一次离线 ASR（带标点恢复）
            if asr_engine and self._raw:
                try:
                    final_raw = await asr_engine.finalize()
                    if final_raw and final_raw.strip():
                        self._raw = final_raw.strip()
                except Exception as e:
                    logger.warning(f"Final offline ASR failed: {e}")

            # 提交所有未上屏内容（带去重）
            committed_len = len(self._committed_text)
            remaining = self._raw[committed_len:] if committed_len < len(self._raw) else ""
            if remaining and remaining.strip():
                remaining = remaining.strip()
                # 去重
                if self._committed_text:
                    max_ol = min(len(self._committed_text), len(remaining))
                    for ol in range(max_ol, 0, -1):
                        if self._committed_text.endswith(remaining[:ol]):
                            remaining = remaining[ol:]
                            break
                if remaining and remaining.strip():
                    if llm_optimizer:
                        try:
                            refined = await llm_optimizer.optimize(remaining)
                            if refined:
                                remaining = refined
                        except Exception:
                            pass
                    await server.broadcast({"type": "final", "text": remaining})
                    await self._commit(remaining)
            else:
                # ★ 无剩余文本时也要清除候选框（防止 preedit 残留）
                await server.broadcast({"type": "reset"})

            self.reset()

        async def _show(self, text: str):
            if server and text:
                logger.info(f"BROADCAST intermediate: {text[:60]}...")
                await server.broadcast({"type": "intermediate", "text": text})

        async def _commit(self, text: str):
            if server and text and text.strip():
                logger.info(f"BROADCAST commit: {text[:60]}...")
                await server.broadcast({"type": "commit", "text": text.strip()})


    _pipeline = PTTStreamPipeline()

    # ---- Callbacks ----

    async def on_audio_data(pcm_data: bytes):
        if asr_engine:
            await asr_engine.process_audio(pcm_data)

    async def on_asr_intermediate(text: str):
        """流式模型中间结果"""
        if text:
            await _pipeline.on_intermediate(text)

    async def on_asr_offline(text: str, generation: int):
        """离线模型纠正结果"""
        if text:
            await _pipeline.on_offline_correction(text, generation)

    if asr_engine:
        asr_engine.set_intermediate_callback(on_asr_intermediate)
        asr_engine.set_offline_callback(on_asr_offline)

    # ---- Server setup ----
    server = UnixSocketServer(socket_path)

    # PTT handlers
    async def on_start_listening():
        audio_capture.start_listening()
        _pipeline.reset()
        if asr_engine:
            asr_engine.reset()
        if server:
            await server.broadcast({"type": "reset"})

    async def on_stop_listening():
        audio_capture.stop_listening()
        await _pipeline.finalize()

    async def on_toggle():
        if audio_capture.is_listening:
            await on_stop_listening()
        else:
            await on_start_listening()

    async def on_reset():
        if asr_engine:
            asr_engine.reset()

    async def on_config(cmd: dict):
        """Handle config update from fcitx5 plugin (LLM + audio device)"""
        nonlocal llm_optimizer

        llm_cfg = cmd.get("llm", {})

        # Only create/update LLM optimizer when explicitly enabled
        llm_enabled = llm_cfg.get("enabled", False)
        if not llm_enabled and llm_optimizer:
            # LLM was disabled → discard optimizer
            logger.info("LLM optimization disabled by fcitx5 config")
            llm_optimizer = None
        elif llm_enabled and not llm_optimizer:
            logger.info("Creating LLM optimizer from fcitx5 config")
            llm_optimizer = LLMOptimizer(
                base_url=llm_cfg.get("base_url", "http://localhost:8000/v1"),
                api_key=llm_cfg.get("api_key", ""),
                model=llm_cfg.get("model", "qwen2.5-7b-instruct"),
                temperature=llm_cfg.get("temperature", 0.3),
                max_tokens=llm_cfg.get("max_tokens", 2000),
                optimize_delay=llm_cfg.get("optimize_delay", 0.5),
                auto_commit_delay=llm_cfg.get("auto_commit_delay", 0.2),
            )
            logger.info(f"LLM optimizer created: {llm_cfg.get('model', 'unknown')}")
        elif llm_optimizer and llm_cfg:
            llm_optimizer.update_config(
                base_url=llm_cfg.get("base_url", llm_optimizer.base_url),
                api_key=llm_cfg.get("api_key", llm_optimizer.api_key),
                model=llm_cfg.get("model", llm_optimizer.model),
                temperature=llm_cfg.get("temperature", llm_optimizer.temperature),
                max_tokens=llm_cfg.get("max_tokens", llm_optimizer.max_tokens),
                optimize_delay=llm_cfg.get("optimize_delay", llm_optimizer.optimize_delay),
                auto_commit_delay=llm_cfg.get("auto_commit_delay", llm_optimizer.auto_commit_delay),
            )
            logger.info("LLM optimizer config updated from fcitx5 GUI")

        # Hot-swap audio device
        new_device = cmd.get("audio_device", "")
        old_device = audio_capture.device or ""
        if new_device != old_device:
            audio_capture.set_device(new_device if new_device else None)
            logger.info(f"Audio device changed: '{old_device or 'default'}' → '{new_device or 'default'}'")

    server.on_audio_data = on_audio_data
    server.on_start_listening = on_start_listening
    server.on_stop_listening = on_stop_listening
    server.on_toggle = on_toggle
    server.on_reset = on_reset
    server.on_config = on_config

    # ---- Event loop ----
    loop = asyncio.new_event_loop()
    stop_event = asyncio.Event()

    def signal_handler():
        logger.info("Received shutdown signal")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    async def run():
        if os.path.exists(socket_path):
            os.unlink(socket_path)

        server_task = asyncio.create_task(server.serve())
        audio_task = asyncio.create_task(
            audio_capture.start(on_audio_data)
        )

        # ★ 启动 ASR 后台处理循环（必须在 event loop 运行后）
        if asr_engine:
            asr_engine.start_processing()

        logger.info(f"Unix socket listening on: {socket_path}")
        logger.info("Audio capture started")
        logger.info("YuHuang Backend is ready!")
        logger.info("")
        logger.info("Usage:")
        logger.info("  Hold the trigger key (default: Right Ctrl) to speak")
        logger.info("  Release trigger key -> text appears at cursor")
        logger.info("  ESC in fcitx5: cancel preedit")
        logger.info("  Enter in fcitx5: commit preedit")
        logger.info("  F5: force LLM optimize")
        logger.info("  F6: toggle listening mode")
        logger.info("")

        await stop_event.wait()

        logger.info("Shutting down...")
        audio_capture.stop()
        server.stop()
        if asr_engine:
            await asr_engine.stop_processing()

        # Cancel all pending tasks gracefully
        current = asyncio.current_task()
        pending = [t for t in asyncio.all_tasks() if t is not current]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        await asyncio.gather(server_task, audio_task, return_exceptions=True)
        if os.path.exists(socket_path):
            os.unlink(socket_path)
        logger.info("Shutdown complete")

    loop.run_until_complete(run())
    loop.close()


if __name__ == "__main__":
    main()
