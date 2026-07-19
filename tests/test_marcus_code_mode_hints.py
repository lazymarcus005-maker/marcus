from marcus_code.runtime.modes import AgentMode, mode_help, mode_hint


def test_every_mode_has_a_hint():
    assert all(mode_hint(mode) for mode in AgentMode)


def test_mode_help_lists_every_mode():
    help_text = mode_help()
    assert all(mode.value in help_text for mode in AgentMode)
