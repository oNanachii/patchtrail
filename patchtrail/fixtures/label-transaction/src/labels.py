"""Update a sample label and append its audit history."""
import sqlite3


def update_label(database_path, sample_id, label, note=""):
    if label not in {"ham", "spam"}:
        raise ValueError("label must be ham or spam")
    connection = sqlite3.connect(database_path)
    try:
        row = connection.execute("SELECT label FROM samples WHERE id = ?", (sample_id,)).fetchone()
        if row is None:
            raise LookupError("sample not found")
        old_label = row[0]
        if old_label == label:
            return {"changed": False, "label": label}
        connection.execute("UPDATE samples SET label = ? WHERE id = ?", (label, sample_id))
        connection.commit()
        connection.execute(
            "INSERT INTO history(sample_id, old_label, new_label, note) VALUES (?, ?, ?, ?)",
            (sample_id, old_label, label, note),
        )
        connection.commit()
        return {"changed": True, "label": label}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
