from __future__ import annotations

from pollypm.cockpit_ui import _celebrate_first_shipped


def test_first_shipped_celebration_respects_no_confetti(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    class DummyApp:
        def notify(self, message: str, **kwargs):  # noqa: ANN001
            calls.append(("notify", message))

        def push_screen(self, screen):  # noqa: ANN001
            calls.append(("push", type(screen).__name__))

    monkeypatch.setenv("POLLY_NO_CONFETTI", "1")
    _celebrate_first_shipped(DummyApp())

    assert calls == [("notify", "🎉 First PR shipped. Nicely done.")]


def test_first_shipped_celebration_pushes_modal_when_enabled(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    class DummyApp:
        def notify(self, message: str, **kwargs):  # noqa: ANN001
            calls.append(("notify", message))

        def push_screen(self, screen):  # noqa: ANN001
            calls.append(("push", type(screen).__name__))

    monkeypatch.delenv("POLLY_NO_CONFETTI", raising=False)
    _celebrate_first_shipped(DummyApp())

    assert calls[0] == ("notify", "🎉 First PR shipped. Nicely done.")
    assert calls[1] == ("push", "_FirstShippedCelebrationModal")
