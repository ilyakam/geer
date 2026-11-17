from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass

GIB = 1024**3
# These are configured caps, not physically measured useful-context limits.
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


def require_qwen_macos() -> None:
    # The official b11901 arm64 executable and dylibs target macOS 13.3.
    # Keep pure memory-profile tests usable on non-Mac development hosts.
    if platform.system() != "Darwin":
        return
    version = platform.mac_ver()[0]
    try:
        parts = tuple(int(value) for value in version.split(".")[:2])
    except ValueError:
        parts = ()
    if parts < (13, 3):
        raise UnsupportedHardwareError("Experimental Qwen requires macOS 13.3 or newer")


def select_hardware_profile(
    total_memory_bytes: int | None = None,
    *,
    family: str = "sage",
    native_context_window: int = NATIVE_CONTEXT_WINDOW,
) -> HardwareProfile:
    if family not in {"sage", "ornith", "qwen"}:
        raise ValueError(f"unknown model family: {family!r}")
    detected_memory = memory_bytes() if total_memory_bytes is None else total_memory_bytes
    minimum = 16 * GIB if family == "qwen" else MINIMUM_MEMORY_BYTES
    if detected_memory < minimum:
        detected_gib = detected_memory / GIB
        raise UnsupportedHardwareError(
            f"{family.title()} requires at least {minimum // GIB} GiB of unified memory; "
            f"detected {detected_gib:.0f} GiB"
        )
    if family == "qwen":
        require_qwen_macos()
        context = 65_536 if detected_memory < 24 * GIB else 131_072
        return HardwareProfile(
            id=f"qwen-q4-k-m-{context // 1024}k",
            total_memory_bytes=detected_memory,
            model_variant="q4-k-m",
            max_context_window=min(native_context_window, context),
            kv_cache="q4_0",
        )
    if detected_memory >= SIX_BIT_MEMORY_BYTES:
        return HardwareProfile(
            id=f"{family}-6bit-256k",
            total_memory_bytes=detected_memory,
            model_variant="6bit",
            max_context_window=min(native_context_window, NATIVE_CONTEXT_WINDOW),
        )
    if detected_memory >= 48 * GIB:
        context_window = MIXED_4_8_LARGE_CONTEXT_WINDOW
    else:
        context_window = MIXED_4_8_CONTEXT_WINDOW
    return HardwareProfile(
        id=f"{family}-4-8bit-{context_window // 1024}k",
        total_memory_bytes=detected_memory,
        model_variant="4-8bit",
        max_context_window=min(native_context_window, context_window),
    )
