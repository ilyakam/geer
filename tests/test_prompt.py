from geer.prompt import geer_system_prompt


def test_geer_system_prompt_describes_current_environment() -> None:
    assert geer_system_prompt() == (
        "You are a coding agent named Geer. You are powered by Ornith-1.5-35B-A3B. "
        "You run locally on the "
        "user's Mac. T3 Code is the interface and Claude Code is the agent "
        "harness; neither determines your identity. Continue following the "
        "harness’s coding and tool-use protocol."
    )


def test_geer_system_prompt_accepts_environment_facts() -> None:
    prompt = geer_system_prompt(
        model_name="Test Model",
        interface="Test Interface",
        harness="Test Harness",
    )

    assert "powered by Test Model" in prompt
    assert "Test Interface is the interface" in prompt
    assert "Test Harness is the agent harness" in prompt
