"""Commit the generated protocol and enforce the owner's archive limit."""
from . import control, retention, store


def complete_protocol(meeting: dict, **fields) -> None:
    meeting_id = meeting["id"]
    user_id = meeting.get("user_id")
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        # Recheck cancellation under the same lock that publishes the result.
        if control.pending(conn, meeting_id):
            raise control.ProcessingStopped()
        store.update_meeting(
            meeting_id, connection=conn, **fields, status="done", error=None, progress=100,
            status_message="Готово",
        )
        removed = store.prune_user_meetings(user_id, connection=conn) if user_id else []
    retention.remove_pruned(removed)
