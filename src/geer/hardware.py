from __future__ import annotations

import subprocess
from dataclasses import dataclass

GIB = 1024**3
NATIVE_CONTEXT_WINDOW = 262_144
MINIMUM_MEMORY_BYTES = 32 * GIB
MIXED_4_8_CONTEXT_WINDOW = 65_536
MIXED_4_8_LARGE_CONTEXT_WINDOW = 131_072
SIX_BIT_MEMORY_BYTES = 64 * GIB
BF16_KV_CACHE = "BF16"


class UnsupportedHardwareError(RuntimeError):
    """Raised when this model family cannot safely run on the detected Mac."""


@dataclass(frozen=True)
class HardwareProfile:
    id: str
    total_memory_bytes: int
    model_variant: str
    max_context_window: int
    kv_cache: str = BF16_KV_CACHE


def memory_bytes() -> int:
    try:
        result = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return int(result.stdout.strip())
    except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return 0


def select_hardware_profile(
    total_memory_bytes: int | None = None,
    *,
    native_context_window: int = NATIVE_CONTEXT_WINDOW,
) -> HardwareProfile:
    detected_memory = memory_bytes() if total_memory_bytes is None else total_memory_bytes
    if detected_memory < MINIMUM_MEMORY_BYTES:
        detected_gib = detected_memory / GIB
        raise UnsupportedHardwareError(
            "Geer requires at least 32 GB of unified memory for Ornith; "
            f"detected {detected_gib:.0f} GB"
        )
    if detected_memory >= SIX_BIT_MEMORY_BYTES:
        return HardwareProfile(
            id="ornith-6bit-256k",
            total_memory_bytes=detected_memory,
            model_variant="6bit",
            max_context_window=min(native_context_window, NATIVE_CONTEXT_WINDOW),
        )
    if detected_memory >= 48 * GIB:
        context_window = MIXED_4_8_LARGE_CONTEXT_WINDOW
    else:
        context_window = MIXED_4_8_CONTEXT_WINDOW
    return HardwareProfile(
        id=f"ornith-4-8bit-{context_window // 1024}k",
        total_memory_bytes=detected_memory,
        model_variant="4-8bit",
        max_context_window=min(native_context_window, context_window),
    )
