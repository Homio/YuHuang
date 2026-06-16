"""Audio capture module — sounddevice + Push-to-Talk (no VAD threshold needed)"""
import asyncio
import logging
import time
from typing import Callable, Optional
import numpy as np

logger = logging.getLogger("yuhuang.audio")


class AudioCapture:
    """Audio capture based on sounddevice with Push-to-Talk

    精简设计: 不需要音量阈值 / VAD / 静音超时
    因为 PTT 按键 (右Ctrl) 已经提供了精确的开关:

      - _listening=False: 丢弃音频
      - _listening=True:  所有音频直接送给 ASR
      - 松键 → ASR finalize → 上屏

    ★ 热插拔：回调限速 + 断线重连看门狗，防止 ALSA xrun 风暴撑爆内存。
    """

    # 回调频率监控：正常帧率 ~3.3fps (4800 samples @ 16kHz)
    _MAX_CALLBACK_RATE_HZ = 60      # 超过此值即异常
    _CALLBACK_RATE_WINDOW = 1.0     # 统计窗口（秒）
    _STREAM_FAIL_THRESHOLD = 3      # 连续超限几次判定流断开
    _RECONNECT_INTERVAL = 2.0       # 重连尝试间隔（秒）

    def __init__(
        self,
        sample_rate: int = 16000,
        channels: int = 1,
        frame_size: int = 4800,
        device: Optional[str] = None,
    ):
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_size = frame_size
        self.device = device

        self._stream = None
        self._running = False
        self._audio_callback: Optional[Callable] = None

        # PTT 开关 (由 trigger key 控制)
        self._listening = False

        # 音频队列 (audio thread → event loop)
        self._audio_queue = asyncio.Queue(maxsize=200)

        # ★ 回调频率监控
        self._callback_times = []  # 最近 _CALLBACK_RATE_WINDOW 秒内的回调时间戳
        self._last_dropped_log = 0.0
        self._drop_count = 0
        self._consecutive_drops = 0

        # ★ 热插拔状态
        self._stream_failed_flag = False  # 流断开信号
        self._last_audio_time = 0.0

    # ── sounddevice callback (在音频线程中运行) ───────

    def _sounddevice_callback(self, indata, frames, time_info, status):
        if status:
            logger.warning(f"Audio input status: {status}")

        self._last_audio_time = time.time()

        # ★ 回调频率监控: 检测 ALSA xrun 异常高频回调
        now = time.time()
        self._callback_times.append(now)
        cutoff = now - self._CALLBACK_RATE_WINDOW
        self._callback_times = [t for t in self._callback_times if t > cutoff]
        current_rate = len(self._callback_times)

        if current_rate > self._MAX_CALLBACK_RATE_HZ:
            self._drop_count += 1
            self._consecutive_drops += 1

            # 每 5 秒打印一次限速摘要
            if now - self._last_dropped_log > 5.0:
                logger.warning(
                    f"Callback rate {current_rate} Hz exceeds limit "
                    f"{self._MAX_CALLBACK_RATE_HZ} Hz — "
                    f"dropped {self._drop_count} frames in last 5s. "
                    f"Microphone may be unplugged."
                )
                self._last_dropped_log = now
                self._drop_count = 0

            # 连续超限 → 判定流已断开，触发重连
            if self._consecutive_drops >= self._STREAM_FAIL_THRESHOLD:
                if not self._stream_failed_flag:
                    logger.warning(
                        "Audio stream failure detected "
                        f"({self._consecutive_drops} consecutive drops) — "
                        "triggering reconnection..."
                    )
                    self._stream_failed_flag = True
            return  # 丢弃此帧

        # 正常回调：重置计数器
        self._drop_count = 0
        self._consecutive_drops = 0

        # PTT: 没按着键 → 丢弃
        if not self._listening:
            return

        audio = indata.copy()
        pcm_bytes = audio.flatten().astype(np.int16).tobytes()

        try:
            self._audio_queue.put_nowait(pcm_bytes)
        except asyncio.QueueFull:
            logger.warning("Audio queue full — dropping frame")

    # ── 底层流管理 ──────────────────────────────────

    async def _open_stream(self) -> bool:
        """打开新的音频输入流，成功返回 True"""
        try:
            import sounddevice as sd

            device_id = None
            if self.device:
                all_devices = sd.query_devices()
                for i, dev in enumerate(all_devices):
                    if self.device in dev["name"]:
                        device_id = i
                        break

            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
                blocksize=self.frame_size,
                device=device_id,
                callback=self._sounddevice_callback,
            )
            self._stream.start()
            logger.info(f"🎤 Audio stream opened (device: {device_id or 'default'})")
            return True
        except Exception as e:
            self._stream = None
            logger.debug(f"Audio stream open failed: {e}")
            return False

    def _close_stream(self):
        """安全关闭音频流"""
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def _is_device_available(self) -> bool:
        """检查是否有可用音频输入设备"""
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            if self.device:
                for dev in devices:
                    if self.device in dev["name"] and dev["max_input_channels"] > 0:
                        return True
                return False
            default = sd.default.device
            if default is not None:
                info = sd.query_devices(default)
                return info["max_input_channels"] > 0
            for dev in devices:
                if dev["max_input_channels"] > 0:
                    return True
            return False
        except Exception:
            return False

    # ── 断线重连看门狗 ──────────────────────────────

    async def _reconnect_watchdog(self):
        """监视音频流健康状态，设备断开时自动重连

        工作机制：
          1. callback 检测到 xrun 风暴（连续超限），设置 _stream_failed_flag
          2. 看门狗收到信号 → 关旧流 → 清队列 → 每 2 秒尝试重连
          3. 重连成功 → 重置状态
          4. 如果 PTT 在断线时是按下状态，自动停止（用户需重新按键）
        """
        while self._running:
            await asyncio.sleep(self._RECONNECT_INTERVAL)

            if not self._stream_failed_flag:
                continue

            # ── 收到断线信号，执行重连 ──
            self._stream_failed_flag = False

            # 如果 PTT 按下中，强制停止
            was_listening = self._listening
            if was_listening:
                self._listening = False
                logger.info("🎤 PTT: FORCE STOP (audio device disconnected)")

            # 关旧流
            self._close_stream()

            # 清空音频队列
            drained = 0
            while not self._audio_queue.empty():
                try:
                    self._audio_queue.get_nowait()
                    drained += 1
                except asyncio.QueueEmpty:
                    break
            if drained > 0:
                logger.debug(f"Audio queue drained: {drained} frames discarded")

            # 重置频率计数器
            self._callback_times.clear()
            self._consecutive_drops = 0
            self._drop_count = 0

            # ── 重连循环 ──
            retry_count = 0
            while self._running and not self._stream:
                retry_count += 1
                if await self._open_stream():
                    logger.info("✅ Audio device reconnected!")
                    if was_listening:
                        logger.info(
                            "Release and press the trigger key again to continue"
                        )
                    break
                if retry_count == 1:
                    logger.info(
                        f"Audio device not available, retrying every "
                        f"{self._RECONNECT_INTERVAL}s "
                        f"(plug in your microphone to resume)..."
                    )
                elif retry_count % 15 == 0:
                    logger.info(
                        f"Still waiting for audio device... "
                        f"(attempt {retry_count})"
                    )
                await asyncio.sleep(self._RECONNECT_INTERVAL)

    # ── 启动 / 停止 ──────────────────────────────────

    async def start(self, audio_callback: Callable):
        """启动音频捕获 + 后台转发循环 + 断线重连看门狗"""
        self._audio_callback = audio_callback
        self._running = True

        # 列出现有设备供诊断
        try:
            import sounddevice as sd
            all_devices = sd.query_devices()
            logger.info("Available audio input devices:")
            for i, dev in enumerate(all_devices):
                if dev["max_input_channels"] > 0:
                    logger.info(f"  [{i}] {dev['name']}")
        except Exception:
            pass

        # 初次打开流（失败不要紧，看门狗会重试）
        if await self._open_stream():
            logger.info("🎤 Hold trigger key to speak, release to commit")
        else:
            logger.warning(
                "No audio input device available at startup — "
                "will retry in background. "
                "Plug in a microphone to use voice input."
            )

        self._last_audio_time = time.time()

        # 启动后台任务
        asyncio.create_task(self._audio_forward_loop())
        asyncio.create_task(self._reconnect_watchdog())

    def set_device(self, device: Optional[str]):
        """运行时切换音频设备，通过看门狗重启流"""
        old_device = self.device
        self.device = device

        logger.info(
            f"Audio device: '{old_device or 'default'}' → '{device or 'default'}'"
        )
        # 触发看门狗重连以应用新设备
        self._stream_failed_flag = True

    async def stop(self):
        """完全停止音频捕获"""
        self._running = False
        self._close_stream()
        # 清空 audio queue 残余帧
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        # 重置频率监控
        self._callback_times.clear()
        self._consecutive_drops = 0
        self._drop_count = 0
        self._stream_failed_flag = False
        logger.info("Audio capture stopped")

    # ── PTT 控制 ─────────────────────────────────────

    def start_listening(self):
        """按住触发键 → 开始接收音频"""
        self._listening = True
        logger.info("🎤 PTT: START")

    def stop_listening(self):
        """松开触发键 → 停止接收"""
        self._listening = False
        logger.info("🎤 PTT: STOP")

    # ── 音频转发循环 (在 event loop 中运行) ──────────

    _FORWARD_LOOP_TIMEOUT = 2.0  # 音频队列超时（秒）

    async def _audio_forward_loop(self):
        """从队列取出音频帧，直接转发给 ASR（无音量/静音判断）"""
        while self._running:
            try:
                pcm_bytes = await asyncio.wait_for(
                    self._audio_queue.get(), timeout=self._FORWARD_LOOP_TIMEOUT
                )
            except asyncio.TimeoutError:
                if not self._running:
                    break
                # 健康检查: 长时间没收到音频
                if self._listening:
                    idle = time.time() - self._last_audio_time
                    if idle > 3.0 and not self._stream_failed_flag:
                        logger.warning(f"⏳ No audio for {idle:.0f}s — mic working?")
                continue
            except (RuntimeError, asyncio.CancelledError):
                break

            if not self._listening:
                continue

            if self._audio_callback:
                try:
                    await self._audio_callback(pcm_bytes)
                except Exception as e:
                    logger.error(f"Audio callback error: {e}")

    # ── 属性 ─────────────────────────────────────────

    @property
    def is_listening(self) -> bool:
        return self._listening

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_stream_alive(self) -> bool:
        """音频流是否存活（用于前端状态显示）"""
        return self._stream is not None and not self._stream_failed_flag

    @property
    def status_str(self) -> str:
        """前端友好的状态字符串"""
        if not self._running:
            return "stopped"
        if not self._stream:
            return "no_device"
        if self._stream_failed_flag:
            return "disconnected"
        return "connected"

    # ── 工具 ─────────────────────────────────────────

    @staticmethod
    def list_devices() -> list:
        try:
            import sounddevice as sd
            result = []
            for i, dev in enumerate(sd.query_devices()):
                if dev["max_input_channels"] > 0:
                    result.append({
                        "id": i,
                        "name": dev["name"],
                        "channels": dev["max_input_channels"],
                        "sample_rate": dev["default_samplerate"],
                    })
            return result
        except Exception as e:
            logger.error(f"List devices error: {e}")
            return []
