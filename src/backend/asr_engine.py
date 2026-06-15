"""ASR 语音识别引擎 — 封装 FunASR (修复: 流式缓存持久化 + GPU 加速 + 增量累积)"""
import asyncio
import logging
import time
from typing import Callable, Optional
import numpy as np

logger = logging.getLogger("yuhuang.asr")


def _detect_device(preferred: str = "cuda") -> str:
    """Detect available compute device (CUDA GPU, MPS, or CPU fallback).

    Returns the actual device string to use (e.g. "cuda:0", "cpu").
    """
    if preferred in ("cuda", "gpu"):
        try:
            import torch
            if torch.cuda.is_available():
                idx = 0
                if ":" in preferred:
                    try:
                        idx = int(preferred.split(":")[1])
                    except (ValueError, IndexError):
                        idx = 0
                count = torch.cuda.device_count()
                if idx >= count:
                    logger.warning(
                        f"Requested cuda:{idx} but only {count} GPU(s) found, using cuda:0"
                    )
                    idx = 0
                device = f"cuda:{idx}"
                logger.info(
                    f"GPU detected: {torch.cuda.get_device_name(idx)} (cuda:{idx})"
                )
                return device
        except ImportError:
            pass
        logger.info("CUDA not available, falling back to CPU")
        return "cpu"

    if preferred == "mps":
        try:
            import torch
            if torch.backends.mps.is_available():
                logger.info("MPS (Apple Silicon GPU) detected")
                return "mps"
        except ImportError:
            pass
        logger.info("MPS not available, falling back to CPU")
        return "cpu"

    return preferred  # pass through explicit device like "cpu"


class ASREngine:
    """基于 FunASR 的语音识别引擎

    修复重点:
      - 流式识别 cache 状态跨调用持久化 (原 bug: 每次传入空 dict)
      - 音频缓冲管理优化
      - 线程池任务隔离
      - GPU 自动检测 (cuda / mps / cpu)
      - 中间结果增量累积 (流式 chunk 文本拼接，不再覆盖丢失)
    """

    def __init__(
        self,
        online_model: str = "paraformer-zh-streaming",
        offline_model: str = "damo/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        vad_model: str = "fsmn-vad",
        punc_model: str = "ct-punc",
        sample_rate: int = 16000,
        intermediate_interval: float = 0.3,
        device: str = "cuda",
    ):
        self.sample_rate = sample_rate
        self.intermediate_interval = intermediate_interval
        self._intermediate_callback: Optional[Callable] = None
        self._offline_callback: Optional[Callable] = None
        self._models_loaded = False

        # 计算设备
        self.device = _detect_device(device)

        # 音频缓冲
        self._audio_buffer = bytearray()
        self._finalized_text = ""

        # ★ 修复: 流式解码状态 (跨多次 _transcribe_partial 调用保持)
        self._stream_cache = {}          # FunASR streaming cache
        self._stream_audio_offset = 0    # 已处理音频样本数 (int16 samples)

        # ★ 增量文本累积 (流式 chunk 结果累积，不覆盖)
        self._accumulated_raw = ""       # 所有 chunk 文本的累积结果
        self._last_raw_text = ""         # 上次返回的 raw 文本（用于 diff）

        # ★ 后台异步处理
        self._new_audio_event = asyncio.Event()
        self._processing = False         # 防止并发 executor 调用
        self._running = False
        self._processing_task = None
        self._offline_task = None        # 定期离线纠正任务
        self._offline_busy = False       # 防止并发离线调用
        self._offline_text = ""          # 离线模型最新纠正结果（整段音频的完整转写）
        self._offline_text_generation = 0  # 离线文本版本号，用于 pipeline 判断是否需要更新
        self._offline_last_text_len = 0  # 上次离线纠正后的文本长度（reset 归零）
        self._offline_last_audio_samples = 0  # 上次纠正时的音频样本数（reset 归零）
        self._simple_append = False     # True=直追加模式（离线同步后，不检测重叠）

        # 模型
        self._vad_model = None
        self._online_model = None
        self._offline_model = None
        self._punc_model = None
        self._sense_voice_model = None  # SenseVoiceSmall for mixed zh-en (offline)

        # 加载模型
        self._load_models(online_model, offline_model, vad_model, punc_model)

    # ── 模型加载 ──────────────────────────────────────

    def _load_models(self, online_model, offline_model, vad_model, punc_model):
        """加载 FunASR 模型（使用检测到的 GPU/CPU 设备）"""
        try:
            from funasr import AutoModel

            logger.info(f"Loading VAD model: {vad_model}  (device={self.device})")
            self._vad_model = AutoModel(
                model="damo/speech_fsmn_vad_zh-cn-16k-common-pytorch",
                device=self.device,
                disable_update=True,
            )

            logger.info(f"Loading online ASR model: {online_model}  (device={self.device})")
            self._online_model = AutoModel(
                model=online_model,
                device=self.device,
                disable_update=True,
            )

            logger.info(f"Loading offline ASR model: {offline_model}  (device={self.device})")
            self._offline_model = AutoModel(
                model=offline_model,
                device=self.device,
                disable_update=True,
            )

            logger.info(f"Loading punctuation model: {punc_model}  (device={self.device})")
            self._punc_model = AutoModel(
                model="damo/punc_ct-transformer_zh-cn-common-vocab272727-pytorch",
                device=self.device,
                disable_update=True,
            )

            logger.info(f"Loading SenseVoiceSmall for offline correction  (device={self.device})")
            self._sense_voice_model = AutoModel(
                model="iic/SenseVoiceSmall",
                device=self.device,
                disable_update=True,
            )

            self._models_loaded = True
            logger.info("All ASR models loaded successfully")

        except ImportError:
            logger.warning("FunASR not installed. Running in MOCK mode.")
            self._models_loaded = False
        except Exception as e:
            logger.error(f"Failed to load ASR models: {e}")
            self._models_loaded = False

    # ── 回调注册 ──────────────────────────────────────

    def set_intermediate_callback(self, callback: Callable):
        self._intermediate_callback = callback

    def set_offline_callback(self, callback: Callable):
        """离线模型纠正结果回调（参数: text, generation）"""
        self._offline_callback = callback

    def get_accumulated_text(self) -> str:
        """返回当前累积的完整 ASR 文本（用于 finalize 等场景）"""
        return self._accumulated_raw

    # ── 状态管理 ──────────────────────────────────────

    def reset(self):
        """重置识别状态 (每次 speech_start 时调用)"""
        self._audio_buffer = bytearray()
        self._finalized_text = ""
        self._stream_cache = {}       # ★ 修复: 清空流式缓存
        self._stream_audio_offset = 0
        self._accumulated_raw = ""    # ★ 清空增量累积
        self._last_raw_text = ""
        self._offline_text = ""       # ★ 清空离线纠正结果
        self._offline_text_generation = 0
        self._offline_last_text_len = 0  # ★ 重置离线计数基准，防止跨 session 污染
        self._offline_last_audio_samples = 0  # ★ 重置音频计数基准
        self._simple_append = False     # ★ 回到正常重叠检测模式
        self._new_audio_event.clear()

    # ── 后台处理循环 ──────────────────────────────────

    def start_processing(self):
        """启动后台 ASR 处理协程（与音频采集并行运行）"""
        if self._processing_task is None:
            self._running = True
            self._processing_task = asyncio.create_task(self._processing_loop())
            self._offline_task = asyncio.create_task(self._periodic_offline_correction())
            logger.info("ASR background processing loop started")

    async def stop_processing(self):
        """停止后台处理"""
        self._running = False
        self._new_audio_event.set()
        for task in (self._processing_task, self._offline_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._processing_task = None
        self._offline_task = None

    async def _processing_loop(self):
        """后台处理循环：独立于音频采集，持续处理缓冲区中的新音频"""
        process_interval = 0.15  # 150ms 轮询间隔，保证低延迟
        while self._running:
            try:
                # 等待新音频事件（带超时，防止死等）
                await asyncio.wait_for(
                    self._new_audio_event.wait(), timeout=process_interval
                )
            except asyncio.TimeoutError:
                # 定期检查是否有未处理的音频
                pass
            except asyncio.CancelledError:
                break

            self._new_audio_event.clear()

            if not self._models_loaded or len(self._audio_buffer) < 800:
                continue

            # 防止并发 executor 调用
            if self._processing:
                continue
            self._processing = True

            try:
                loop = asyncio.get_event_loop()
                text = await loop.run_in_executor(None, self._transcribe_partial)
                if text and text.strip():
                    logger.info(f"ASR intermediate: {text.strip()}")
                    if self._intermediate_callback:
                        await self._intermediate_callback(text.strip())
            except Exception as e:
                logger.error(f"ASR intermediate error: {e}")
            finally:
                self._processing = False

    async def _periodic_offline_correction(self):
        """按字数+时长双重触发的离线纠正任务。

        SenseVoiceSmall RTF≈0.013 (GPU)，10s音频仅需0.13s，可高频纠正。
        触发条件（满足任一即触发）:
          1. 流式模型累积新增 ≥25 字（首轮 ≥10 字）
          2. 新增录音 ≥2.5 秒 —— 兜底触发
        """
        TRIGGER_CHARS = 25       # 新增字数阈值
        FIRST_TRIGGER_CHARS = 10 # 首轮更低，快速给出纠正
        TRIGGER_AUDIO_S = 2.5    # 新增录音时长兜底触发（秒）
        FIRST_AUDIO_S = 1.5      # 首轮音频兜底
        MIN_INTERVAL = 1.0       # 最小间隔秒数（SenseVoice 很快，可高频）
        POLL_INTERVAL = 0.3      # 轮询间隔
        STATUS_EVERY = 6         # 每 ~1.8s 输出一次状态心跳

        _last_offline_at = 0.0
        _cycle_count = 0

        logger.info("Offline correction task started "
                    "(trigger: +%d chars or +%.0fs audio, min interval: %.1fs)",
                    FIRST_TRIGGER_CHARS, FIRST_AUDIO_S, MIN_INTERVAL)

        while self._running:
            try:
                await asyncio.sleep(POLL_INTERVAL)
            except asyncio.CancelledError:
                break
            if not self._running:
                break

            _cycle_count += 1

            if not self._models_loaded:
                if _cycle_count % STATUS_EVERY == 0:
                    logger.warning("Offline check: models not loaded, skipping")
                continue
            if self._offline_busy:
                continue

            current_text_len = len(self._accumulated_raw)
            new_chars = current_text_len - self._offline_last_text_len
            first_run = (self._offline_last_text_len == 0)

            current_audio_samples = len(self._audio_buffer) // 2
            new_audio_s = (current_audio_samples - self._offline_last_audio_samples) / self.sample_rate

            char_threshold = FIRST_TRIGGER_CHARS if first_run else TRIGGER_CHARS
            audio_threshold = FIRST_AUDIO_S if first_run else TRIGGER_AUDIO_S

            # 周期状态心跳
            if _cycle_count % STATUS_EVERY == 0:
                buf_len = len(self._audio_buffer)
                audio_dur = buf_len / (self.sample_rate * 2) if self.sample_rate else 0
                logger.info(
                    f"Offline check: text={current_text_len} chars "
                    f"(new={new_chars}/{char_threshold}), "
                    f"audio={audio_dur:.1f}s (new={new_audio_s:.1f}s/{audio_threshold}s), "
                    f"first_run={first_run}, busy={self._offline_busy}"
                )

            trigger_by_chars = new_chars >= char_threshold
            trigger_by_audio = new_audio_s >= audio_threshold
            if not (trigger_by_chars or trigger_by_audio):
                continue

            now = time.time()
            if now - _last_offline_at < MIN_INTERVAL:
                continue

            buf_len = len(self._audio_buffer)
            audio_dur = buf_len / (self.sample_rate * 2) if self.sample_rate else 0
            if audio_dur < 1.0:
                continue

            _last_offline_at = now
            self._offline_last_audio_samples = current_audio_samples
            self._offline_busy = True

            reason = "chars" if trigger_by_chars else "audio_duration"
            try:
                logger.info(
                    f"Offline correction trigger: "
                    f"+{new_chars} chars, +{new_audio_s:.0f}s audio "
                    f"(reason={reason}), running..."
                )
                loop = asyncio.get_event_loop()
                text = await loop.run_in_executor(None, self._run_offline_quick)
                if text and text.strip():
                    self._offline_text = text.strip()
                    self._offline_text_generation += 1
                    self._offline_last_text_len = len(self._offline_text)
                    logger.info(
                        f"Offline correction (#{self._offline_text_generation}): "
                        f"{len(self._offline_text)} chars "
                        f"(+{new_chars} new, {audio_dur:.0f}s audio)"
                    )
                    if self._offline_callback:
                        await self._offline_callback(
                            self._offline_text,
                            self._offline_text_generation,
                        )
                    self._sync_streaming_from_offline()
                else:
                    logger.warning(
                        f"Offline correction returned empty text "
                        f"(audio={audio_dur:.0f}s)"
                    )
            except Exception as e:
                logger.error(f"Periodic offline correction failed: {e}", exc_info=True)
            finally:
                self._offline_busy = False

    @staticmethod
    def _clean_sense_voice_text(text: str) -> str:
        """Strip SenseVoice special tokens: <|zh|>, <|NEUTRAL|>, <|Speech|>, etc."""
        import re
        return re.sub(r'<\|[^|]+\|>', '', text).strip()

    def _run_offline_quick(self) -> str:
        """在 executor 线程中快速运行离线 ASR（SenseVoiceSmall，带 ITN，含标点）"""
        buf_snapshot = bytes(self._audio_buffer)
        if len(buf_snapshot) < 1600:  # 至少 0.1s 音频
            return ""
        audio_np = np.frombuffer(buf_snapshot, dtype=np.int16)
        audio_float = audio_np.astype(np.float32) / 32768.0
        audio_float = self._preprocess_audio(audio_float, self.sample_rate)

        try:
            # 优先使用 SenseVoiceSmall（中英混合识别 + ITN + 自带标点）
            if self._sense_voice_model:
                res = self._sense_voice_model.generate(
                    input=audio_float,
                    language="zh",
                    use_itn=True,
                )
                if res and len(res) > 0:
                    raw = res[0].get("text", "")
                    text = self._clean_sense_voice_text(raw)
                    if text:
                        logger.debug(
                            f"Offline quick ASR (SenseVoice): "
                            f"{len(buf_snapshot)/self.sample_rate:.1f}s audio "
                            f"→ {len(text)} chars"
                        )
                        return text
                logger.debug(
                    f"Offline quick ASR (SenseVoice): returned empty text "
                    f"({len(buf_snapshot)/self.sample_rate:.1f}s audio)"
                )
                return ""

            # Fallback: 原始 paraformer 离线模型
            res = self._offline_model.generate(input=audio_float)
            if res and len(res) > 0:
                text = res[0].get("text", "")
                if text:
                    logger.debug(
                        f"Offline quick ASR (paraformer): {len(buf_snapshot)/self.sample_rate:.1f}s audio "
                        f"→ {len(text)} chars"
                    )
                    return text
                else:
                    logger.debug(
                        f"Offline quick ASR (paraformer): returned empty text "
                        f"({len(buf_snapshot)/self.sample_rate:.1f}s audio)"
                    )
        except Exception as e:
            logger.warning(f"Offline quick ASR failed: {e}", exc_info=True)
        return ""

    def get_offline_text(self) -> tuple:
        """返回 (离线纠正文本, 版本号)"""
        return self._offline_text, self._offline_text_generation

    def _sync_streaming_from_offline(self):
        """离线纠正后重置流式模型状态，让后续增量追加到离线文本上。

        离线模型纠正了全部音频 → 文本是权威的。
        流式模型 cache 重置 → 从当前音频位置重新开始，
        _accumulated_raw 替换为离线文本 → 后续流式 chunk 追加到离线文本尾部。
        """
        offline_text = self._offline_text
        if not offline_text:
            return
        # 替换累积文本为离线纠正结果
        self._accumulated_raw = offline_text
        # ★ 关键：不把离线文本作为 LCP 参照物。
        # 流式模型已重置，后续 chunk 代表全新录音，与离线文本无重叠。
        # LCP 只在连续 chunk 之间进行（由 _accumulate 处理）。
        self._last_raw_text = ""
        # 重置流式解码状态，从当前缓冲区末尾开始
        self._stream_cache = {}
        self._stream_audio_offset = max(0, len(self._audio_buffer) // 2)
        # 切换为直追加模式：后续流式 chunk 代表全新音频，直接追加不检测重叠
        self._simple_append = True
        logger.info(
            f"Streaming reset after offline correction: "
            f"accumulated={len(offline_text)} chars, "
            f"audio_offset={self._stream_audio_offset} samples"
        )

    # ── 音频处理 ──────────────────────────────────────

    async def process_audio(self, pcm_data: bytes):
        """接收音频数据（非阻塞：只积累缓冲区，触发后台处理）"""
        self._audio_buffer.extend(pcm_data)
        if self._models_loaded:
            self._new_audio_event.set()

    # ── 流式中间识别 (online) ──────────────────────────

    def _transcribe_partial(self) -> str:
        """★ 流式识别：只送新增的音频段，配合 cache 维持解码状态

        关键修复:
          - 只处理执行时刻已有的音频，不猜后面新增的
          - offset 只前进实际处理的样本数，防止跳帧
          - 限制单次处理时长 (MAX_CHUNK_SECONDS) 防止级联延迟
        ★ 增量累积: 流式 chunk 返回的非累积文本，由引擎拼接为完整文本。
        """
        if not self._models_loaded or len(self._audio_buffer) < 800:
            return ""

        # 快照当前缓冲区长度，防止处理过程中 buffer 继续增长导致 offset 跳帧
        buf_len_snapshot = len(self._audio_buffer)
        byte_offset = self._stream_audio_offset * 2  # samples → bytes

        if byte_offset >= buf_len_snapshot:
            return ""

        # 限制单次处理最大时长: 最多处理 3 秒音频，避免卡死
        MAX_BYTES = self.sample_rate * 3 * 2  # 3s * 16000Hz * 2bytes/int16
        process_end = min(byte_offset + MAX_BYTES, buf_len_snapshot)
        new_bytes = bytes(self._audio_buffer[byte_offset:process_end])

        if len(new_bytes) < 800:
            return ""

        try:
            audio_np = np.frombuffer(new_bytes, dtype=np.int16)
            audio_float = audio_np.astype(np.float32) / 32768.0
            audio_float = self._preprocess_audio(audio_float, self.sample_rate)

            res = self._online_model.generate(
                input=audio_float,
                cache=self._stream_cache,    # 持久化解码状态
                is_final=False,
                chunk_size=[5, 10, 5],
            )

            # ★ 关键修复: 只前进已处理的样本数，不跳帧
            self._stream_audio_offset += len(audio_np)

            if res and len(res) > 0:
                chunk_text = (res[0].get("text", "") or "").strip()
                if chunk_text:
                    self._accumulate(chunk_text)
                    logger.info(
                        f"ASR partial result: [{chunk_text[:30]}]"
                        f"  (accumulated: {len(self._accumulated_raw)} chars)"
                    )
                    return self._accumulated_raw
                else:
                    logger.info(
                        f"ASR partial returned empty (res has {len(res)} items)"
                    )
        except Exception as e:
            logger.debug(f"Partial transcription error: {e}")
            # On error, still advance offset to avoid infinite loop;
            # use a conservative advance since audio_np may not be defined.
            try:
                self._stream_audio_offset += len(audio_np)
            except NameError:
                self._stream_audio_offset += int(len(new_bytes) // 2)

        return self._accumulated_raw if self._accumulated_raw else ""

    def _accumulate(self, chunk_text: str):
        """增量累积：抗上下文重置的拼接策略。

        正常模式: 流式 chunk 通过 LCP+后缀匹配拼回完整文本。
        直追加模式 (_simple_append=True): 离线纠正后流式模型已重置，
        后续 chunk 代表全新音频，直接追加不检测重叠。
        """
        if not chunk_text:
            return

        # 直追加模式：离线纠正后的全新流式输出，直接拼到离线文本尾部
        if self._simple_append:
            prev = self._last_raw_text
            lcp = 0
            max_lcp = min(len(chunk_text), len(prev))
            while lcp < max_lcp and chunk_text[lcp] == prev[lcp]:
                lcp += 1
            if lcp >= len(prev):
                # chunk 包含上次全部内容 → 只追加尾部新增
                new_content = chunk_text[lcp:]
                if new_content:
                    self._accumulated_raw += new_content
            elif lcp > 0:
                # 部分前缀重叠 → 追加非重叠部分
                new_content = chunk_text[lcp:]
                if new_content:
                    self._accumulated_raw += new_content
            else:
                # lcp==0: 流式模型上下文重置，chunk 是全新内容，直接整段追加
                self._accumulated_raw += chunk_text
            self._last_raw_text = chunk_text
            return

        if not self._accumulated_raw:
            self._accumulated_raw = chunk_text
            self._last_raw_text = chunk_text
            return

        prev = self._last_raw_text
        lcp = 0
        max_lcp = min(len(chunk_text), len(prev))
        while lcp < max_lcp and chunk_text[lcp] == prev[lcp]:
            lcp += 1

        if lcp >= len(prev):
            # Case A: chunk 完全包含上次结果 → 只追加尾部新增
            new_content = chunk_text[lcp:]
            if new_content:
                self._accumulated_raw += new_content
        elif lcp > 0:
            # Case B: 部分前缀重叠 → 追加非重叠部分
            new_content = chunk_text[lcp:]
            if new_content:
                self._accumulated_raw += new_content
        else:
            # Case C: lcp==0，前缀完全不重叠 — 模型窗口滑动或上下文重置
            # 不能盲目追加！先找 chunk 与 accumulated 的任意位置重叠点
            # 策略：在 accumulated_raw 尾部找 chunk 前缀的最长匹配
            best_overlap = 0
            search_limit = min(len(chunk_text), len(self._accumulated_raw), 80)
            for i in range(search_limit, 0, -1):
                if self._accumulated_raw.endswith(chunk_text[:i]):
                    best_overlap = i
                    break

            if best_overlap > 0:
                # 找到后缀重叠 → 只追加真正新增的部分
                new_content = chunk_text[best_overlap:]
                if new_content:
                    self._accumulated_raw += new_content
                logger.debug(
                    f"ASR lcp=0 → suffix overlap={best_overlap}, "
                    f"added {len(new_content)} chars"
                )
            else:
                # 无任何重叠 — 检查 chunk 是否完全冗余 (模型重启相同内容)
                if chunk_text in self._accumulated_raw:
                    logger.debug(
                        f"ASR lcp=0 → chunk already in accumulated (len={len(chunk_text)}), skip"
                    )
                else:
                    # 真正的新内容，追加
                    self._accumulated_raw += chunk_text
                    logger.debug(
                        f"ASR lcp=0 → no overlap, appending {len(chunk_text)} chars"
                    )

        self._last_raw_text = chunk_text

    # ── 最终识别 (offline + VAD + 标点) ────────────────

    async def finalize(self) -> str:
        """最终识别 — 使用完整离线模型"""
        if not self._audio_buffer:
            return self._finalized_text

        if not self._models_loaded:
            audio_len = len(self._audio_buffer) / self.sample_rate
            if audio_len < 0.5:
                return ""
            return "（语音识别结果 — 请安装 FunASR）"

        try:
            loop = asyncio.get_event_loop()
            text = await loop.run_in_executor(None, self._transcribe_final)
            if text and text.strip():
                self._finalized_text = text.strip()
                logger.info(f"ASR final: {self._finalized_text}")
            return self._finalized_text
        except Exception as e:
            logger.error(f"Final transcription error: {e}")
            return self._finalized_text

    @staticmethod
    def _preprocess_audio(audio: 'np.ndarray', sample_rate: int) -> 'np.ndarray':
        """音频预处理: 去直流偏移 + 轻度降噪，提升 ASR 准确率"""
        # 去直流偏移
        audio = audio - np.mean(audio)
        # 简单峰值归一化（防止削波）
        peak = np.max(np.abs(audio))
        if peak > 0.9:
            audio = audio * (0.9 / peak)
        return audio.astype(np.float32)

    def _transcribe_final(self) -> str:
        """最终转写: SenseVoiceSmall 全量识别（中英混合 + ITN + 自带标点）

        全量送入离线模型，不做 VAD 分段（GPU 显存充足，无 OOM 风险）。
        SenseVoiceSmall 自带标点恢复和 ITN（数字/英文标准化）。
        回退到 paraformer + ct-punc 当 SenseVoice 不可用时。
        """
        audio_np = np.frombuffer(bytes(self._audio_buffer), dtype=np.int16)
        if len(audio_np) < 160:  # 至少 10ms 音频
            return ""
        audio_float = audio_np.astype(np.float32) / 32768.0
        audio_float = self._preprocess_audio(audio_float, self.sample_rate)

        audio_duration = len(audio_float) / self.sample_rate
        logger.info(f"Final offline ASR: {audio_duration:.1f}s audio (full, no VAD)")

        try:
            # 优先使用 SenseVoiceSmall（中英混合 + ITN + 标点）
            if self._sense_voice_model:
                res = self._sense_voice_model.generate(
                    input=audio_float,
                    language="zh",
                    use_itn=True,
                )
                if res and len(res) > 0:
                    raw = res[0].get("text", "")
                    text = self._clean_sense_voice_text(raw)
                    if text:
                        logger.info(
                            f"Offline ASR final (SenseVoice, {len(text)} chars): "
                            f"{text[:80]}..."
                        )
                        return text
                    logger.info("SenseVoice ASR returned empty text, falling back to paraformer")
                else:
                    logger.info("SenseVoice ASR returned no result, falling back to paraformer")

            # Fallback: paraformer + punctuation
            res = self._offline_model.generate(input=audio_float)
            if not res or len(res) == 0:
                logger.info("Offline ASR returned no result")
                return ""

            text = (res[0].get("text", "") or "").strip()
            if not text:
                logger.info("Offline ASR returned empty text")
                return ""

            # 标点恢复（短文本 ≤500 字）
            if len(text) <= 500:
                try:
                    punc_res = self._punc_model.generate(input=text)
                    if punc_res and len(punc_res) > 0:
                        text = punc_res[0].get("text", text)
                except Exception as e:
                    logger.warning(f"Punctuation restoration failed: {e}")

            logger.info(f"Offline ASR final (paraformer, {len(text)} chars): {text[:80]}...")
            return text

        except Exception as e:
            logger.error(f"Final transcription error: {e}", exc_info=True)
            return ""

    @property
    def is_loaded(self) -> bool:
        return self._models_loaded
