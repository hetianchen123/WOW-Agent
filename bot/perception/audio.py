"""Bite detection from game audio loopback.

Band energy vs rolling baseline. On macOS this needs a loopback device
(e.g. BlackHole) selected via config; on Windows the default output can be
opened through WASAPI loopback (sounddevice hostapi) if exposed.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Deque, Optional, Tuple

import numpy as np

from ..core.config import AudioConfig

log = logging.getLogger(__name__)


class AudioMonitor:
    def __init__(self, cfg: AudioConfig) -> None:
        self.cfg = cfg
        self.available = False
        self._lock = threading.Lock()
        self._blocks: Deque[np.ndarray] = deque(maxlen=8)
        self._baseline: Deque[float] = deque(maxlen=40)  # ~2 s at 20 blocks/s
        self._last_bite_t = 0.0
        self._stream = None
        self._baseline_ready = False
        self.last_ratio = 0.0
        self.last_energy = 0.0
        self.block_count = 0

    # ---------------------------------------------------------------- setup
    def start(self) -> bool:
        if not self.cfg.enabled:
            log.info("audio disabled in config")
            return False
        try:
            import sounddevice as sd

            block = int(self.cfg.sample_rate * self.cfg.block_ms / 1000)
            self._stream = sd.InputStream(
                samplerate=self.cfg.sample_rate,
                channels=1,
                dtype="float32",
                blocksize=block,
                device=self.cfg.device,
                callback=self._on_audio,
            )
            self._stream.start()
            self.available = True
            log.info("audio monitor started (device=%s, block=%d)", self.cfg.device, block)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("audio unavailable (%s) — vision-only bite detection", exc)
            self.available = False
            return False

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:  # noqa: BLE001
                pass
            self._stream = None
        self.available = False

    # -------------------------------------------------------------- callback
    def _on_audio(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        if status:
            log.debug("audio status: %s", status)
        with self._lock:
            self._blocks.append(indata[:, 0].copy())

    # ------------------------------------------------------------- analysis
    def _band_energy(self, samples: np.ndarray) -> float:
        n = len(samples)
        if n == 0:
            return 0.0
        window = np.hanning(n)
        spec = np.abs(np.fft.rfft(samples * window))
        freqs = np.fft.rfftfreq(n, d=1.0 / self.cfg.sample_rate)
        band = (freqs >= self.cfg.band_low_hz) & (freqs <= self.cfg.band_high_hz)
        return float(np.sqrt(np.mean(spec[band] ** 2))) if band.any() else 0.0

    def _process_blocks(self) -> None:
        with self._lock:
            blocks = list(self._blocks)
            self._blocks.clear()
        for b in blocks:
            energy = self._band_energy(b)
            self.block_count += 1
            self.last_energy = energy
            self._baseline.append(energy)

    def baseline(self) -> Optional[float]:
        if len(self._baseline) < 8:
            return None
        return float(np.median(self._baseline))

    def is_bite(self) -> bool:
        """True when the newest block's band energy spikes above baseline."""
        if not self.available:
            return False
        self._process_blocks()
        base = self.baseline()
        if base is None or base <= 1e-9:
            self.last_ratio = 0.0
            return False
        ratio = self.last_energy / base
        self.last_ratio = ratio
        now = time.time()
        if ratio >= self.cfg.bite_threshold and (now - self._last_bite_t) >= self.cfg.refractory_s:
            self._last_bite_t = now
            return True
        return False

    # ------------------------------------------------------------- probing
    def probe_tick(self) -> Tuple[float, float, float]:
        """(energy, baseline, ratio) for the probe CLI."""
        self._process_blocks()
        base = self.baseline() or 0.0
        ratio = self.last_energy / base if base > 1e-9 else 0.0
        return self.last_energy, base, ratio
