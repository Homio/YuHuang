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
    """

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

    # ── sounddevice callback (在音频线程中运行) ───────

    def _sounddevice_callback(self, indata, frames, time_info, status):
        if status:
            logger.warning(f"Audio input status: {status}")

        self._last_audio_time = time.time()

        # PTT: 没按着键 → 丢弃
        if not self._listening:
            return

        audio = indata.copy()
        pcm_bytes = audio.flatten().astype(np.int16).tobytes()

        try:
            self._audio_queue.put_nowait(pcm_bytes)
        except asyncio.QueueFull:
            logger.warning("Audio queue full — dropping frame")

    # ── 启动 / 停止 ──────────────────────────────────

    async def start(self, audio_callback: Callable):
        """启动音频捕获 (后台持续运行)"""
        self._audio_callback = audio_callback
        self._running = True

        try:
            import sounddevice as sd

            # 列出可用输入设备
            all_devices = sd.query_devices()
            logger.info("Available audio input devices:")
            for i, dev in enumerate(all_devices):
                if dev["max_input_channels"] > 0:
                    logger.info(f"  [{i}] {dev['name']}")

            device_id = None
            if self.device:
                for i, dev in enumerate(all_devices):
                    if self.device in dev["name"]:
                        device_id = i
                        logger.info(f"Matched device '{self.device}' → [{i}] {dev['name']}")
                        break
                if device_id is None:
                    logger.warning(f"Device '{self.device}' not found, using default")

            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
                blocksize=self.frame_size,
                device=device_id,
                callback=self._sounddevice_callback,
            )
            self._stream.start()
            actual = device_id if device_id is not None else "default"
            logger.info(f"🎤 Audio capture started (device: {actual})")
            logger.info(f"🎤 Hold trigger key to speak, release to commit")

            self._last_audio_time = time.time()
            asyncio.create_task(self._audio_forward_loop())

        except Exception as e:
            logger.error(f"❌ Failed to start audio capture: {e}")
            raise

    def set_device(self, device: Optional[str]):
        """运行时热切换音频设备，不影响音频转发循环"""
        old_device = self.device
        self.device = device

        if not self._stream:
            logger.info(f"Audio device set to '{device or 'default'}' (on next start)")
            return

        logger.info(f"Hot-swap: '{old_device or 'default'}' → '{device or 'default'}'")

        # 停旧流
        try:
            self._stream.stop()
            self._stream.close()
        except Exception as e:
            logger.warning(f"Error stopping old stream: {e}")
        self._stream = None

        # 启新流
        try:
            import sounddevice as sd
            device_id = None
            if self.device:
                for i, dev in enumerate(sd.query_devices()):
                    if self.device in dev["name"]:
                        device_id = i
                        break
                if device_id is None:
                    logger.warning(f"Device '{self.device}' not found, using default")

            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
                blocksize=self.frame_size,
                device=device_id,
                callback=self._sounddevice_callback,
            )
            self._stream.start()
            logger.info(f"Audio device switched to: {device_id or 'default'}")
        except Exception as e:
            logger.error(f"Failed to start new device: {e}")
            # 回退到默认
            self.device = None
            try:
                self._stream = sd.InputStream(
                    samplerate=self.sample_rate, channels=self.channels,
                    dtype="int16", blocksize=self.frame_size,
                    device=None, callback=self._sounddevice_callback,
                )
                self._stream.start()
                logger.info("Fell back to default audio device")
            except Exception as e2:
                logger.error(f"Fallback also failed: {e2}")

    def stop(self):
        self._running = False
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
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

    async def _audio_forward_loop(self):
        """从队列取出音频帧，直接转发给 ASR（无音量/静音判断）"""
        while self._running:
            try:
                pcm_bytes = await asyncio.wait_for(
                    self._audio_queue.get(), timeout=2.0
                )
            except asyncio.TimeoutError:
                if not self._running:
                    break
                # 健康检查: 长时间没收到音频
                idle = time.time() - getattr(self, '_last_audio_time', time.time())
                if idle > 3.0 and self._listening:
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
