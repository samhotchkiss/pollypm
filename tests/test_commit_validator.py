from __future__ import annotations

from pollypm.commit_validator import validate_commit_message


def test_valid_feat_message() -> None:
    result = validate_commit_message("feat: add commit validator")

    assert result.is_valid is True
    assert result.errors == []
    assert result.commit_type == "feat"
    assert result.scope is None
    assert result.description == "add commit validator"
    assert result.body is None
    assert result.footers == []
    assert result.has_breaking_change is False


def test_valid_fix_message_with_scope() -> None:
    result = validate_commit_message("fix(parser): handle footer parsing")

    assert result.is_valid is True
    assert result.commit_type == "fix"
    assert result.scope == "parser"
    assert result.description == "handle footer parsing"


def test_valid_message_with_body() -> None:
    message = "docs: explain validator\n\nAdd module usage examples.\nInclude footer guidance."

    result = validate_commit_message(message)

    assert result.is_valid is True
    assert result.body == "Add module usage examples.\nInclude footer guidance."
    assert result.footers == []


def test_valid_message_with_breaking_change_footer() -> None:
    message = (
        "refactor(api)!: simplify validator result\n\n"
        "BREAKING CHANGE: ValidationResult field names changed"
    )

    result = validate_commit_message(message)

    assert result.is_valid is True
    assert result.commit_type == "refactor"
    assert result.scope == "api"
    assert result.has_breaking_change is True
    assert result.footers == ["BREAKING CHANGE: ValidationResult field names changed"]


def test_invalid_missing_type() -> None:
    result = validate_commit_message(": missing type")

    assert result.is_valid is False
    assert "Commit header must start with a lowercase type prefix." in result.errors


def test_invalid_missing_colon() -> None:
    result = validate_commit_message("feat add validator")

    assert result.is_valid is False
    assert "Commit header must contain a `: ` separator after type/scope." in result.errors


def test_invalid_empty_description() -> None:
    result = validate_commit_message("feat: ")

    assert result.is_valid is False
    assert "Commit description cannot be empty." in result.errors


def test_invalid_unknown_type() -> None:
    result = validate_commit_message("update: add validator")

    assert result.is_valid is False
    assert any("Unknown commit type `update`." in error for error in result.errors)
