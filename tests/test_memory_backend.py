from pathlib import Path

from promptmaster.memory_backends import FileMemoryBackend, get_memory_backend


def test_file_memory_backend_writes_reads_and_compacts(tmp_path: Path) -> None:
    backend = FileMemoryBackend(tmp_path)

    entry = backend.write_entry(
        scope="demo",
        title="North Star",
        body="Keep the project moving in small testable chunks.",
        tags=["vision", "north-star"],
        source="manual",
    )

    assert entry.file_path.exists()
    assert entry.summary_path.exists()

    listed = backend.list_entries(scope="demo", kind="note")
    assert len(listed) == 1
    assert listed[0].title == "North Star"

    read_back = backend.read_entry(entry.entry_id)
    assert read_back is not None
    assert read_back.body.startswith("Keep the project moving")

    summary = backend.summarize("demo")
    assert "Memory Summary: demo" in summary

    compacted = backend.compact("demo")
    assert compacted.summary_path.exists()
    assert compacted.entry_count == 1
    assert backend.store.latest_memory_summary("demo") is not None


def test_get_memory_backend_returns_file_backend(tmp_path: Path) -> None:
    backend = get_memory_backend(tmp_path, "file")
    assert isinstance(backend, FileMemoryBackend)
