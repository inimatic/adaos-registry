"""Durable synchronization observations, independent of UI projection."""
import json


class SyncStateStore:
    def __init__(self, repository):
        self.repository = repository

    def load(self):
        with self.repository.connect() as connection:
            row = connection.execute(
                "SELECT value FROM coordinator_meta WHERE key='sync_runtime_state'"
            ).fetchone()
        return json.loads(row[0]) if row else {}

    def save(self, value):
        with self.repository.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO coordinator_meta(key,value) VALUES ('sync_runtime_state',?)",
                (json.dumps(value, sort_keys=True),),
            )
            connection.commit()
