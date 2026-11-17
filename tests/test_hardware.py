from __future__ import annotations

import pytest

from geer.hardware import (
    BF16_KV_CACHE,
    GIB,
    NATIVE_CONTEXT_WINDOW,
    UnsupportedHardwareError,
    select_hardware_profile,
)


@pytest.mark.parametrize(
    ("memory_gib", "profile_id", "variant", "context_window"),
    (
        (32, "sage-4-8bit-64k", "4-8bit", 65_536),
        (48, "sage-4-8bit-128k", "4-8bit", 131_072),
        (64, "sage-6bit-256k", "6bit", 262_144),
        (96, "sage-6bit-256k", "6bit", 262_144),
        (128, "sage-6bit-256k", "6bit", 262_144),
    ),
)
def test_configured_memory_profiles(
    memory_gib: int,
    profile_id: str,
    variant: str,
    context_window: int,
) -> None:
    profile = select_hardware_profile(memory_gib * GIB)

    assert profile.id == profile_id
    assert profile.model_variant == variant
    assert profile.max_context_window == context_window
    assert profile.kv_cache == BF16_KV_CACHE


def test_profile_selection_detects_installed_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("geer.hardware.memory_bytes", lambda: 48 * GIB)

    profile = select_hardware_profile()

    assert profile.id == "sage-4-8bit-128k"
    assert profile.total_memory_bytes == 48 * GIB


def test_profile_never_exceeds_the_model_context_window() -> None:
    profile = select_hardware_profile(
        64 * GIB,
        native_context_window=196_608,
    )

    assert profile.max_context_window == 196_608


@pytest.mark.parametrize("memory_gib", (0, 16, 24, 31))
def test_less_than_32_gib_is_rejected(memory_gib: int) -> None:
    with pytest.raises(UnsupportedHardwareError, match="at least 32 GiB"):
        select_hardware_profile(memory_gib * GIB)


def test_larger_unified_memory_uses_the_6bit_native_profile() -> None:
    profile = select_hardware_profile(192 * GIB)

    assert profile.model_variant == "6bit"
    assert profile.max_context_window == NATIVE_CONTEXT_WINDOW


@pytest.mark.parametrize("memory_gib", (32, 48, 64, 512))
def test_ornith_keeps_its_family_identity_and_matches_sage_caps(memory_gib: int) -> None:
    sage = select_hardware_profile(memory_gib * GIB, family="sage")
    ornith = select_hardware_profile(memory_gib * GIB, family="ornith")
    assert ornith.id == sage.id.replace("sage", "ornith")
    assert ornith.model_variant == sage.model_variant
    assert ornith.max_context_window == sage.max_context_window
    assert ornith.kv_cache == sage.kv_cache


def test_unknown_hardware_family_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown model family"):
        select_hardware_profile(128 * GIB, family="third-model")


@pytest.mark.parametrize(
    "memory_gib,context", [(16, 65_536), (24, 131_072), (32, 131_072), (512, 131_072)]
)
def test_qwen_profile_preserves_full_vision_and_estimated_context(memory_gib, context):
    profile = select_hardware_profile(memory_gib * GIB, family="qwen")
    assert profile.model_variant == "q4-k-m"
    assert profile.max_context_window == context
    assert profile.kv_cache == "q4_0"


@pytest.mark.parametrize("memory_gib", [0, 8, 15])
def test_qwen_rejects_less_than_16_gib(memory_gib):
    with pytest.raises(UnsupportedHardwareError, match="at least 16 GiB"):
        select_hardware_profile(memory_gib * GIB, family="qwen")


def test_qwen_cap_respects_native_context_limit():
    assert (
        select_hardware_profile(
            24 * GIB,
            family="qwen",
            native_context_window=32_768,
        ).max_context_window
        == 32_768
    )


@pytest.mark.parametrize('version,allowed', [('13.0', False), ('13.2.1', False),
                                           ('13.3', True), ('15.8.1', True)])
def test_qwen_requires_runtime_minimum_macos(monkeypatch, version, allowed):
    monkeypatch.setattr('geer.hardware.platform.system', lambda: 'Darwin')
    monkeypatch.setattr('geer.hardware.platform.mac_ver', lambda: (version, (), ''))
    if allowed:
        assert select_hardware_profile(16 * GIB, family='qwen').model_variant == 'q4-k-m'
    else:
        with pytest.raises(UnsupportedHardwareError, match='macOS 13.3'):
            select_hardware_profile(16 * GIB, family='qwen')
    assert select_hardware_profile(32 * GIB, family='sage').model_variant == '4-8bit'
