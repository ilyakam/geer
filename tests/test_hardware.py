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
    with pytest.raises(UnsupportedHardwareError, match="at least 32 GB"):
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
