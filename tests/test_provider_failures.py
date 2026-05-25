from pollypm.provider_failures import has_auth_failure, has_capacity_failure


def test_auth_failure_detects_claude_login_prompt() -> None:
    assert has_auth_failure("Not logged in · Please run /login")


def test_capacity_failure_detects_claude_limit_prompt() -> None:
    assert has_capacity_failure(
        "You've hit your limit · resets May 26 at 8pm\n/usage-credits"
    )


def test_capacity_failure_ignores_regular_status_text() -> None:
    assert not has_capacity_failure("Claude Code ready; 72% left")
