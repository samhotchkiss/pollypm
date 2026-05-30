from __future__ import annotations

from pollypm.inbox_sweep import sweep_watchdog_notifies_for_terminal_task


class _Store:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.closed: list[int] = []

    def query_messages(self, **filters: object) -> list[dict[str, object]]:
        out = []
        for row in self.rows:
            if all(row.get(key) == value for key, value in filters.items()):
                out.append(row)
        return out

    def close_message(self, msg_id: int) -> None:
        self.closed.append(msg_id)
        for row in self.rows:
            if row.get("id") == msg_id:
                row["state"] = "closed"


def test_watchdog_notify_sweep_closes_exact_terminal_task_refs() -> None:
    store = _Store([
        {
            "id": 1,
            "type": "notify",
            "state": "open",
            "recipient": "user",
            "subject": "Task demo/1 needs operator action",
            "body": "",
            "payload": {},
            "labels": ["notify", "watchdog"],
        },
        {
            "id": 2,
            "type": "notify",
            "state": "open",
            "recipient": "user",
            "subject": "Task demo/10 needs operator action",
            "body": "",
            "payload": {},
            "labels": ["notify", "watchdog"],
        },
        {
            "id": 3,
            "type": "notify",
            "state": "open",
            "recipient": "user",
            "subject": "Pinned demo/1",
            "body": "",
            "payload": {},
            "labels": ["notify", "watchdog", "pinned"],
        },
        {
            "id": 4,
            "type": "notify",
            "state": "open",
            "recipient": "user",
            "subject": "Non-watchdog demo/1",
            "body": "",
            "payload": {},
            "labels": ["notify"],
        },
    ])

    archived = sweep_watchdog_notifies_for_terminal_task(store, "demo/1")

    assert archived == 1
    assert store.closed == [1]
