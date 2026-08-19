from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from adaos.sdk.data.relational import RelationalMigration, RelationalStorageRequirements, database

from research.contracts import canonical_json, now


MIGRATIONS = (
    RelationalMigration(
        version=1,
        name="research direction formulation ledger",
        idempotent=True,
        statements=(
            "CREATE TABLE research_directions (direction_id TEXT PRIMARY KEY, title TEXT NOT NULL, project_kind TEXT NOT NULL, status TEXT NOT NULL, generation INTEGER NOT NULL, current_bundle_digest TEXT, current_prototype_digest TEXT, accepted_prototype_digest TEXT, automation_brief_digest TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
            "CREATE TABLE research_prototypes (digest TEXT PRIMARY KEY, direction_id TEXT NOT NULL, revision INTEGER NOT NULL, parent_digest TEXT, source_bundle_digest TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)",
            "CREATE UNIQUE INDEX research_prototypes_revision ON research_prototypes(direction_id, revision)",
            "CREATE TABLE research_automation_briefs (digest TEXT PRIMARY KEY, direction_id TEXT NOT NULL, prototype_digest TEXT NOT NULL, source_bundle_digest TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)",
            "CREATE TABLE research_activity (event_id TEXT PRIMARY KEY, direction_id TEXT NOT NULL, seq INTEGER NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL, message TEXT NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL)",
            "CREATE UNIQUE INDEX research_activity_seq ON research_activity(direction_id, seq)",
            "CREATE TABLE research_commands (idempotency_key TEXT PRIMARY KEY, command_name TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL)",
        ),
    ),
    RelationalMigration(
        version=2,
        name="durable staged formulation artifacts",
        idempotent=True,
        statements=(
            "CREATE TABLE research_formulation_stages (run_id TEXT NOT NULL, direction_id TEXT NOT NULL, stage_index INTEGER NOT NULL, stage_name TEXT NOT NULL, status TEXT NOT NULL, input_digest TEXT NOT NULL, output_digest TEXT, payload_json TEXT NOT NULL, telemetry_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(run_id, stage_name))",
            "CREATE INDEX research_formulation_direction ON research_formulation_stages(direction_id, created_at)",
        ),
    ),
    RelationalMigration(
        version=3,
        name="research direction task and implementation lineage",
        idempotent=True,
        statements=(
            "ALTER TABLE research_directions ADD COLUMN description TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE research_directions ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE research_directions ADD COLUMN artifact_owner_skill_id TEXT",
            "ALTER TABLE research_directions ADD COLUMN legacy_project_ref TEXT",
            "ALTER TABLE research_directions ADD COLUMN active_task_id TEXT",
            "ALTER TABLE research_prototypes ADD COLUMN task_id TEXT",
            "ALTER TABLE research_automation_briefs ADD COLUMN task_id TEXT",
            "ALTER TABLE research_automation_briefs ADD COLUMN implementation_track_id TEXT",
            "ALTER TABLE research_formulation_stages ADD COLUMN task_id TEXT",
            "ALTER TABLE research_activity ADD COLUMN actor TEXT NOT NULL DEFAULT 'system:legacy'",
            "ALTER TABLE research_activity ADD COLUMN origin TEXT NOT NULL DEFAULT 'research_orchestrator'",
            "ALTER TABLE research_activity ADD COLUMN subject_ref TEXT",
            "CREATE TABLE research_tasks (task_id TEXT PRIMARY KEY, direction_id TEXT NOT NULL, revision INTEGER NOT NULL, title TEXT NOT NULL, research_question TEXT NOT NULL, status TEXT NOT NULL, parent_task_id TEXT, branch_of_task_id TEXT, dependency_refs_json TEXT NOT NULL, metadata_json TEXT NOT NULL, source_bundle_digest TEXT, current_prototype_digest TEXT, accepted_compilation_digest TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
            "CREATE INDEX research_tasks_direction ON research_tasks(direction_id, updated_at)",
            "CREATE TABLE research_compilations (digest TEXT PRIMARY KEY, compilation_id TEXT NOT NULL, direction_id TEXT NOT NULL, task_id TEXT NOT NULL, revision INTEGER NOT NULL, parent_digest TEXT, prototype_digest TEXT NOT NULL, source_bundle_digest TEXT NOT NULL, payload_json TEXT NOT NULL, accepted_at TEXT NOT NULL, accepted_by TEXT NOT NULL)",
            "CREATE UNIQUE INDEX research_compilations_revision ON research_compilations(task_id, revision)",
            "CREATE TABLE research_implementation_tracks (track_id TEXT PRIMARY KEY, direction_id TEXT NOT NULL, task_id TEXT NOT NULL, revision INTEGER NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL, condition_id TEXT, parent_track_id TEXT, project_ref TEXT, primary_target_ref TEXT, development_session_id TEXT, candidate_release_digest TEXT, metadata_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
            "CREATE INDEX research_tracks_task ON research_implementation_tracks(task_id, updated_at)",
            "CREATE TABLE research_aliases (alias_ref TEXT PRIMARY KEY, canonical_ref TEXT NOT NULL, provenance_json TEXT NOT NULL, created_at TEXT NOT NULL)",
            "UPDATE research_directions SET artifact_owner_skill_id=direction_id WHERE artifact_owner_skill_id IS NULL",
            "INSERT INTO research_tasks(task_id, direction_id, revision, title, research_question, status, parent_task_id, branch_of_task_id, dependency_refs_json, metadata_json, source_bundle_digest, current_prototype_digest, accepted_compilation_digest, created_at, updated_at) SELECT direction_id || '.task-001', direction_id, 1, title || ' — primary task', '', CASE WHEN accepted_prototype_digest IS NOT NULL THEN 'accepted' ELSE 'draft' END, NULL, NULL, '[]', '{}', current_bundle_digest, current_prototype_digest, NULL, created_at, updated_at FROM research_directions",
            "UPDATE research_directions SET active_task_id=direction_id || '.task-001' WHERE active_task_id IS NULL",
            "UPDATE research_prototypes SET task_id=(SELECT active_task_id FROM research_directions WHERE research_directions.direction_id=research_prototypes.direction_id) WHERE task_id IS NULL",
            "UPDATE research_automation_briefs SET task_id=(SELECT active_task_id FROM research_directions WHERE research_directions.direction_id=research_automation_briefs.direction_id) WHERE task_id IS NULL",
            "UPDATE research_formulation_stages SET task_id=(SELECT active_task_id FROM research_directions WHERE research_directions.direction_id=research_formulation_stages.direction_id) WHERE task_id IS NULL",
        ),
    ),
    RelationalMigration(
        version=4,
        name="project release study realization and federated activity lineage",
        idempotent=True,
        statements=(
            "ALTER TABLE research_implementation_tracks ADD COLUMN project_release_ref TEXT",
            "ALTER TABLE research_implementation_tracks ADD COLUMN project_release_digest TEXT",
            "ALTER TABLE research_implementation_tracks ADD COLUMN study_id TEXT",
            "ALTER TABLE research_implementation_tracks ADD COLUMN study_realization_ref TEXT",
            "ALTER TABLE research_implementation_tracks ADD COLUMN study_realization_digest TEXT",
            "ALTER TABLE research_implementation_tracks ADD COLUMN runner_ref TEXT",
            "ALTER TABLE research_implementation_tracks ADD COLUMN experiment_id TEXT",
            "ALTER TABLE research_activity ADD COLUMN source_event_id TEXT",
            "CREATE UNIQUE INDEX research_activity_external_event ON research_activity(origin, source_event_id)",
        ),
    ),
    RelationalMigration(
        version=5,
        name="scope research prototype revisions to tasks",
        idempotent=True,
        statements=(
            "DROP INDEX research_prototypes_revision",
            "CREATE UNIQUE INDEX research_prototypes_task_revision ON research_prototypes(task_id, revision)",
        ),
    ),
)


def db():
    value = database(
        "research_orchestrator",
        requirements=RelationalStorageRequirements(
            durability="durable",
            transactions_required=True,
            json_required=False,
            backup_required=True,
            restore_required=True,
            rollback_policy="restore",
            locality="any",
            migration_owner="skill:research_orchestrator_skill",
        ),
    )
    value.migrate(MIGRATIONS, staged=True)
    return value


class OrchestratorRepository:
    def __init__(self) -> None:
        self._db = db()

    @staticmethod
    def _direction(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        return {
            "schema": "adaos.research.direction.v1",
            "direction_id": str(row["direction_id"]),
            "ref": f"research-direction:{row['direction_id']}",
            # ``generation`` remains the optimistic-concurrency token stored by
            # the v1 ledger.  ``revision`` is its one-based domain projection;
            # both therefore advance atomically without a second lifecycle
            # counter that could drift.
            "revision": int(row["generation"]) + 1,
            "title": str(row["title"]),
            "project_kind": str(row["project_kind"]),
            "status": str(row["status"]),
            "generation": int(row["generation"]),
            "current_bundle_digest": row.get("current_bundle_digest"),
            "current_prototype_digest": row.get("current_prototype_digest"),
            "accepted_prototype_digest": row.get("accepted_prototype_digest"),
            "automation_brief_digest": row.get("automation_brief_digest"),
            "description": str(row.get("description") or ""),
            "tags": json.loads(str(row.get("tags_json") or "[]")),
            "artifact_owner_skill_id": str(row.get("artifact_owner_skill_id") or row["direction_id"]),
            "artifact_owner_ref": f"skill:{row.get('artifact_owner_skill_id') or row['direction_id']}",
            "legacy_project_ref": row.get("legacy_project_ref"),
            "active_task_id": row.get("active_task_id"),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def initialize(
        self,
        direction_id: str,
        title: str,
        *,
        description: str = "",
        tags: list[str] | None = None,
        artifact_owner_skill_id: str | None = None,
        legacy_project_ref: str | None = None,
    ) -> dict[str, Any]:
        existing = self.get_direction(direction_id)
        if existing:
            return existing
        timestamp = now()
        self._db.execute(
            "INSERT INTO research_directions(direction_id, title, project_kind, status, generation, current_bundle_digest, current_prototype_digest, accepted_prototype_digest, automation_brief_digest, description, tags_json, artifact_owner_skill_id, legacy_project_ref, active_task_id, created_at, updated_at) VALUES (:direction_id, :title, 'research_direction', 'intake', 0, NULL, NULL, NULL, NULL, :description, :tags_json, :artifact_owner_skill_id, :legacy_project_ref, :active_task_id, :created_at, :updated_at)",
            {
                "direction_id": direction_id,
                "title": title,
                "description": str(description or ""),
                "tags_json": canonical_json(list(tags or [])),
                "artifact_owner_skill_id": str(artifact_owner_skill_id or direction_id),
                "legacy_project_ref": legacy_project_ref,
                "active_task_id": f"{direction_id}.task-001",
                "created_at": timestamp,
                "updated_at": timestamp,
            },
        )
        self.create_task(
            direction_id,
            task_id=f"{direction_id}.task-001",
            title=f"{title} — primary task",
            research_question="",
        )
        return dict(self.get_direction(direction_id) or {})

    def get_direction(self, direction_id: str) -> dict[str, Any] | None:
        row = self._db.fetch_one("SELECT * FROM research_directions WHERE direction_id=:direction_id", {"direction_id": direction_id})
        return self._direction(row)

    def list_directions(self, limit: int = 500) -> list[dict[str, Any]]:
        rows = self._db.fetch_all(
            "SELECT * FROM research_directions ORDER BY updated_at DESC, direction_id",
            {},
        )[: max(1, min(int(limit), 5000))]
        return [value for row in rows if (value := self._direction(row)) is not None]

    @staticmethod
    def _task(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        return {
            "schema": "adaos.research.task.v1",
            "task_id": str(row["task_id"]),
            "ref": f"research-task:{row['task_id']}",
            "direction_id": str(row["direction_id"]),
            "direction_ref": f"research-direction:{row['direction_id']}",
            "revision": int(row["revision"]),
            "title": str(row["title"]),
            "research_question": str(row["research_question"]),
            "status": str(row["status"]),
            "parent_task_id": row.get("parent_task_id"),
            "branch_of_task_id": row.get("branch_of_task_id"),
            "dependency_refs": json.loads(str(row.get("dependency_refs_json") or "[]")),
            "metadata": json.loads(str(row.get("metadata_json") or "{}")),
            "source_bundle_digest": row.get("source_bundle_digest"),
            "current_prototype_digest": row.get("current_prototype_digest"),
            "accepted_compilation_digest": row.get("accepted_compilation_digest"),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def create_task(
        self,
        direction_id: str,
        *,
        task_id: str,
        title: str,
        research_question: str = "",
        parent_task_id: str | None = None,
        branch_of_task_id: str | None = None,
        dependency_refs: list[str] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        direction = self.get_direction(direction_id)
        if not direction:
            raise ValueError("research direction does not exist")
        existing = self.get_task(task_id)
        if existing:
            if existing["direction_id"] != direction_id:
                raise ValueError("research task id belongs to another direction")
            return existing
        for relation in [parent_task_id, branch_of_task_id, *(dependency_refs or [])]:
            if not relation:
                continue
            relation_id = str(relation).removeprefix("research-task:")
            if relation_id == task_id:
                raise ValueError("research task cannot depend on itself")
            related = self.get_task(relation_id)
            if not related or related["direction_id"] != direction_id:
                raise ValueError(
                    f"research task relation must resolve inside research-direction:{direction_id}: {relation}"
                )
        normalized_dependencies = list(
            dict.fromkeys(
                f"research-task:{str(value).removeprefix('research-task:')}"
                for value in dependency_refs or []
            )
        )
        timestamp = now()
        self._db.execute(
            "INSERT INTO research_tasks(task_id, direction_id, revision, title, research_question, status, parent_task_id, branch_of_task_id, dependency_refs_json, metadata_json, source_bundle_digest, current_prototype_digest, accepted_compilation_digest, created_at, updated_at) VALUES (:task_id, :direction_id, 1, :title, :research_question, 'draft', :parent_task_id, :branch_of_task_id, :dependency_refs_json, :metadata_json, NULL, NULL, NULL, :created_at, :updated_at)",
            {
                "task_id": task_id,
                "direction_id": direction_id,
                "title": title,
                "research_question": research_question,
                "parent_task_id": str(parent_task_id).removeprefix("research-task:") if parent_task_id else None,
                "branch_of_task_id": str(branch_of_task_id).removeprefix("research-task:") if branch_of_task_id else None,
                "dependency_refs_json": canonical_json(normalized_dependencies),
                "metadata_json": canonical_json(dict(metadata or {})),
                "created_at": timestamp,
                "updated_at": timestamp,
            },
        )
        return dict(self.get_task(task_id) or {})

    def get_task(self, task_id: str | None) -> dict[str, Any] | None:
        if not task_id:
            return None
        return self._task(
            self._db.fetch_one(
                "SELECT * FROM research_tasks WHERE task_id=:task_id",
                {"task_id": task_id},
            )
        )

    def list_tasks(self, direction_id: str) -> list[dict[str, Any]]:
        rows = self._db.fetch_all(
            "SELECT * FROM research_tasks WHERE direction_id=:direction_id ORDER BY created_at, task_id",
            {"direction_id": direction_id},
        )
        return [value for row in rows if (value := self._task(row)) is not None]

    def set_active_task(self, direction_id: str, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        if not task or task["direction_id"] != direction_id:
            raise ValueError("active ResearchTask must belong to the research direction")
        direction = self.get_direction(direction_id)
        if not direction:
            raise ValueError("research direction does not exist")
        if direction.get("active_task_id") == task_id:
            return direction
        compilation = self.latest_compilation_for_task(task_id)
        brief_row = self._db.fetch_one(
            "SELECT digest FROM research_automation_briefs WHERE task_id=:task_id ORDER BY created_at DESC",
            {"task_id": task_id},
        )
        self._db.execute(
            "UPDATE research_directions SET active_task_id=:task_id, current_bundle_digest=:bundle_digest, current_prototype_digest=:prototype_digest, accepted_prototype_digest=:accepted_prototype_digest, automation_brief_digest=:brief_digest, status=:status, generation=generation+1, updated_at=:updated_at WHERE direction_id=:direction_id",
            {
                "direction_id": direction_id,
                "task_id": task_id,
                "bundle_digest": task.get("source_bundle_digest"),
                "prototype_digest": task.get("current_prototype_digest"),
                "accepted_prototype_digest": (compilation or {}).get("prototype_digest"),
                "brief_digest": (brief_row or {}).get("digest"),
                "status": "handoff_ready" if brief_row else str(task.get("status") or "draft"),
                "updated_at": now(),
            },
        )
        return dict(self.get_direction(direction_id) or {})

    def bind_task_formulation(
        self,
        task_id: str,
        *,
        source_bundle_digest: str,
        prototype_digest: str,
        research_question: str,
    ) -> dict[str, Any]:
        self._db.execute(
            "UPDATE research_tasks SET source_bundle_digest=:bundle, current_prototype_digest=:prototype, research_question=:question, status='formulation', revision=revision+1, updated_at=:updated_at WHERE task_id=:task_id",
            {
                "task_id": task_id,
                "bundle": source_bundle_digest,
                "prototype": prototype_digest,
                "question": research_question,
                "updated_at": now(),
            },
        )
        result = self.get_task(task_id)
        if not result:
            raise ValueError("research task does not exist")
        return result

    def merge_task_metadata(
        self,
        task_id: str,
        metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        task = self.get_task(task_id)
        if not task:
            raise ValueError("research task does not exist")
        merged = {**dict(task.get("metadata") or {}), **dict(metadata)}
        if merged == dict(task.get("metadata") or {}):
            return task
        self._db.execute(
            "UPDATE research_tasks SET metadata_json=:metadata_json, revision=revision+1, updated_at=:updated_at WHERE task_id=:task_id",
            {
                "task_id": task_id,
                "metadata_json": canonical_json(merged),
                "updated_at": now(),
            },
        )
        return dict(self.get_task(task_id) or {})

    def put_compilation(
        self,
        direction_id: str,
        task_id: str,
        compilation: Mapping[str, Any],
        *,
        prototype_digest: str,
        actor: str,
    ) -> dict[str, Any]:
        value = dict(compilation)
        compilation_digest = str(value["digest"])
        previous = self._db.fetch_one(
            "SELECT * FROM research_compilations WHERE digest=:digest",
            {"digest": compilation_digest},
        )
        if previous:
            return dict(self._compilation(previous) or {})
        revision_row = self._db.fetch_one(
            "SELECT COALESCE(MAX(revision), 0) AS revision FROM research_compilations WHERE task_id=:task_id",
            {"task_id": task_id},
        )
        revision = int((revision_row or {}).get("revision") or 0) + 1
        parent_row = self._db.fetch_one(
            "SELECT digest FROM research_compilations WHERE task_id=:task_id ORDER BY revision DESC",
            {"task_id": task_id},
        )
        timestamp = now()
        with self._db.transaction() as tx:
            tx.execute(
                "INSERT INTO research_compilations(digest, compilation_id, direction_id, task_id, revision, parent_digest, prototype_digest, source_bundle_digest, payload_json, accepted_at, accepted_by) VALUES (:digest, :compilation_id, :direction_id, :task_id, :revision, :parent_digest, :prototype_digest, :source_bundle_digest, :payload_json, :accepted_at, :accepted_by)",
                {
                    "digest": compilation_digest,
                    "compilation_id": str(
                        value.get("compilation_id") or f"{task_id}.r{revision}"
                    ).removeprefix("research-compilation:"),
                    "direction_id": direction_id,
                    "task_id": task_id,
                    "revision": revision,
                    "parent_digest": (parent_row or {}).get("digest"),
                    "prototype_digest": prototype_digest,
                    "source_bundle_digest": str(value["source_bundle_digest"]),
                    "payload_json": canonical_json(value),
                    "accepted_at": timestamp,
                    "accepted_by": actor,
                },
            )
            tx.execute(
                "UPDATE research_tasks SET accepted_compilation_digest=:digest, status='accepted', revision=revision+1, updated_at=:updated_at WHERE task_id=:task_id AND direction_id=:direction_id",
                {
                    "digest": compilation_digest,
                    "updated_at": timestamp,
                    "task_id": task_id,
                    "direction_id": direction_id,
                },
            )
        return dict(self.get_compilation_record(compilation_digest) or {})

    @staticmethod
    def _compilation(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        payload = json.loads(str(row["payload_json"]))
        compilation_id = str(row["compilation_id"]).removeprefix("research-compilation:")
        return {
            "schema": "adaos.research.compilation.v1",
            "compilation_id": compilation_id,
            "ref": f"research-compilation:{compilation_id}",
            "direction_id": str(row["direction_id"]),
            "direction_ref": f"research-direction:{row['direction_id']}",
            "task_id": str(row["task_id"]),
            "task_ref": f"research-task:{row['task_id']}",
            "revision": int(row["revision"]),
            "parent_digest": row.get("parent_digest"),
            "digest": str(row["digest"]),
            "prototype_digest": str(row["prototype_digest"]),
            "source_bundle_digest": str(row["source_bundle_digest"]),
            "payload": payload,
            "accepted_at": str(row["accepted_at"]),
            "accepted_by": str(row["accepted_by"]),
        }

    def get_compilation(self, digest: str | None) -> dict[str, Any] | None:
        if not digest:
            return None
        row = self._db.fetch_one(
            "SELECT payload_json FROM research_compilations WHERE digest=:digest",
            {"digest": digest},
        )
        return json.loads(str(row["payload_json"])) if row else None

    def get_compilation_record(self, digest: str | None) -> dict[str, Any] | None:
        if not digest:
            return None
        return self._compilation(
            self._db.fetch_one(
                "SELECT * FROM research_compilations WHERE digest=:digest",
                {"digest": digest},
            )
        )

    def latest_compilation_for_task(self, task_id: str) -> dict[str, Any] | None:
        return self._compilation(
            self._db.fetch_one(
                "SELECT * FROM research_compilations WHERE task_id=:task_id ORDER BY revision DESC",
                {"task_id": task_id},
            )
        )

    @staticmethod
    def _track(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        return {
            "schema": "adaos.research.implementation_track.v1",
            "track_id": str(row["track_id"]),
            "ref": f"implementation-track:{row['track_id']}",
            "direction_id": str(row["direction_id"]),
            "task_id": str(row["task_id"]),
            "revision": int(row["revision"]),
            "title": str(row["title"]),
            "status": str(row["status"]),
            "condition_id": row.get("condition_id"),
            "parent_track_id": row.get("parent_track_id"),
            "project_ref": row.get("project_ref"),
            "primary_target_ref": row.get("primary_target_ref"),
            "development_session_id": row.get("development_session_id"),
            "candidate_release_digest": row.get("candidate_release_digest"),
            "project_release_ref": row.get("project_release_ref"),
            "project_release_digest": row.get("project_release_digest"),
            "study_id": row.get("study_id"),
            "study_realization_ref": row.get("study_realization_ref"),
            "study_realization_digest": row.get("study_realization_digest"),
            "runner_ref": row.get("runner_ref"),
            "experiment_id": row.get("experiment_id"),
            "metadata": json.loads(str(row.get("metadata_json") or "{}")),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def create_track(
        self,
        direction_id: str,
        task_id: str,
        *,
        track_id: str,
        title: str,
        project_ref: str | None = None,
        primary_target_ref: str | None = None,
        condition_id: str | None = None,
        parent_track_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        existing = self.get_track(track_id)
        if existing:
            if existing["task_id"] != task_id:
                raise ValueError("implementation track belongs to another task")
            return existing
        timestamp = now()
        self._db.execute(
            "INSERT INTO research_implementation_tracks(track_id, direction_id, task_id, revision, title, status, condition_id, parent_track_id, project_ref, primary_target_ref, development_session_id, candidate_release_digest, metadata_json, created_at, updated_at) VALUES (:track_id, :direction_id, :task_id, 1, :title, 'planned', :condition_id, :parent_track_id, :project_ref, :primary_target_ref, NULL, NULL, :metadata_json, :created_at, :updated_at)",
            {
                "track_id": track_id,
                "direction_id": direction_id,
                "task_id": task_id,
                "title": title,
                "condition_id": condition_id,
                "parent_track_id": parent_track_id,
                "project_ref": project_ref,
                "primary_target_ref": primary_target_ref,
                "metadata_json": canonical_json(dict(metadata or {})),
                "created_at": timestamp,
                "updated_at": timestamp,
            },
        )
        return dict(self.get_track(track_id) or {})

    def record_track_evaluation(
        self,
        track_id: str,
        *,
        status: str,
        metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        current = self.get_track(track_id)
        if not current:
            raise ValueError("implementation track does not exist")
        if current.get("status") == status and dict(current.get("metadata") or {}) == dict(metadata):
            return current
        self._db.execute(
            "UPDATE research_implementation_tracks SET status=:status, metadata_json=:metadata_json, revision=revision+1, updated_at=:updated_at WHERE track_id=:track_id",
            {
                "track_id": track_id,
                "status": status,
                "metadata_json": canonical_json(dict(metadata)),
                "updated_at": now(),
            },
        )
        result = self.get_track(track_id)
        if not result:
            raise ValueError("implementation track does not exist")
        return result

    def put_alias(
        self,
        alias_ref: str,
        canonical_ref: str,
        provenance: Mapping[str, Any],
    ) -> dict[str, Any]:
        existing = self._db.fetch_one(
            "SELECT canonical_ref, provenance_json, created_at FROM research_aliases WHERE alias_ref=:alias_ref",
            {"alias_ref": alias_ref},
        )
        if existing:
            if str(existing["canonical_ref"]) != canonical_ref:
                raise ValueError("research alias already resolves to another object")
            return {
                "alias_ref": alias_ref,
                "canonical_ref": canonical_ref,
                "provenance": json.loads(str(existing["provenance_json"])),
                "created_at": str(existing["created_at"]),
            }
        created_at = now()
        self._db.execute(
            "INSERT INTO research_aliases(alias_ref, canonical_ref, provenance_json, created_at) VALUES (:alias_ref, :canonical_ref, :provenance_json, :created_at)",
            {
                "alias_ref": alias_ref,
                "canonical_ref": canonical_ref,
                "provenance_json": canonical_json(dict(provenance)),
                "created_at": created_at,
            },
        )
        return {
            "alias_ref": alias_ref,
            "canonical_ref": canonical_ref,
            "provenance": dict(provenance),
            "created_at": created_at,
        }

    def list_aliases(self, canonical_ref: str) -> list[dict[str, Any]]:
        rows = self._db.fetch_all(
            "SELECT alias_ref, canonical_ref, provenance_json, created_at FROM research_aliases WHERE canonical_ref=:canonical_ref ORDER BY alias_ref",
            {"canonical_ref": canonical_ref},
        )
        return [
            {
                "alias_ref": str(row["alias_ref"]),
                "canonical_ref": str(row["canonical_ref"]),
                "provenance": json.loads(str(row["provenance_json"])),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def bind_track_development(
        self,
        track_id: str,
        *,
        project_ref: str,
        primary_target_ref: str,
        development_session_id: str,
    ) -> dict[str, Any]:
        self._db.execute(
            "UPDATE research_implementation_tracks SET project_ref=:project_ref, primary_target_ref=:primary_target_ref, development_session_id=:session_id, status='development_ready', revision=revision+1, updated_at=:updated_at WHERE track_id=:track_id",
            {
                "track_id": track_id,
                "project_ref": project_ref,
                "primary_target_ref": primary_target_ref,
                "session_id": development_session_id,
                "updated_at": now(),
            },
        )
        result = self.get_track(track_id)
        if not result:
            raise ValueError("implementation track does not exist")
        return result

    def bind_track_release(
        self,
        track_id: str,
        *,
        candidate_release_digest: str,
        project_release_ref: str | None = None,
        project_release_digest: str | None = None,
    ) -> dict[str, Any]:
        current = self.get_track(track_id)
        if not current:
            raise ValueError("implementation track does not exist")
        candidate_digest = str(candidate_release_digest or "").strip()
        if not candidate_digest:
            raise ValueError("candidate_release_digest is required")
        release_ref = str(project_release_ref or "").strip() or None
        release_digest = str(project_release_digest or "").strip() or None
        if bool(release_ref) != bool(release_digest):
            raise ValueError("project release ref and digest must be bound together")
        if release_digest and release_digest != candidate_digest:
            raise ValueError("promoted ProjectRelease digest differs from the prepared candidate")
        status = "release_ready" if release_ref else "trial_ready"
        if (
            current.get("candidate_release_digest") == candidate_digest
            and current.get("project_release_ref") == release_ref
            and current.get("project_release_digest") == release_digest
            and current.get("status") == status
        ):
            return current
        self._db.execute(
            "UPDATE research_implementation_tracks SET candidate_release_digest=:candidate_digest, project_release_ref=:release_ref, project_release_digest=:release_digest, status=:status, revision=revision+1, updated_at=:updated_at WHERE track_id=:track_id",
            {
                "track_id": track_id,
                "candidate_digest": candidate_digest,
                "release_ref": release_ref,
                "release_digest": release_digest,
                "status": status,
                "updated_at": now(),
            },
        )
        return dict(self.get_track(track_id) or {})

    def bind_track_study(
        self,
        track_id: str,
        *,
        study_id: str,
        study_realization_ref: str,
        study_realization_digest: str,
        runner_ref: str,
        experiment_id: str | None = None,
    ) -> dict[str, Any]:
        current = self.get_track(track_id)
        if not current:
            raise ValueError("implementation track does not exist")
        values = {
            "study_id": str(study_id or "").strip(),
            "study_realization_ref": str(study_realization_ref or "").strip(),
            "study_realization_digest": str(study_realization_digest or "").strip(),
            "runner_ref": str(runner_ref or "").strip(),
            "experiment_id": str(experiment_id or "").strip() or None,
        }
        if any(not values[key] for key in ("study_id", "study_realization_ref", "study_realization_digest", "runner_ref")):
            raise ValueError("complete StudyRealization identity is required")
        if current.get("project_release_digest") is None:
            raise ValueError("implementation track has no promoted ProjectRelease")
        status = "experiment_ready" if values["experiment_id"] else "study_ready"
        if all(current.get(key) == value for key, value in values.items()) and current.get("status") == status:
            return current
        self._db.execute(
            "UPDATE research_implementation_tracks SET study_id=:study_id, study_realization_ref=:study_realization_ref, study_realization_digest=:study_realization_digest, runner_ref=:runner_ref, experiment_id=:experiment_id, status=:status, revision=revision+1, updated_at=:updated_at WHERE track_id=:track_id",
            {**values, "track_id": track_id, "status": status, "updated_at": now()},
        )
        return dict(self.get_track(track_id) or {})

    def get_track(self, track_id: str | None) -> dict[str, Any] | None:
        if not track_id:
            return None
        return self._track(
            self._db.fetch_one(
                "SELECT * FROM research_implementation_tracks WHERE track_id=:track_id",
                {"track_id": track_id},
            )
        )

    def list_tracks(self, task_id: str) -> list[dict[str, Any]]:
        rows = self._db.fetch_all(
            "SELECT * FROM research_implementation_tracks WHERE task_id=:task_id ORDER BY created_at, track_id",
            {"task_id": task_id},
        )
        return [value for row in rows if (value := self._track(row)) is not None]

    def set_bundle(self, direction_id: str, bundle_digest: str) -> dict[str, Any]:
        self._db.execute(
            "UPDATE research_directions SET current_bundle_digest=:bundle, status='formulation', generation=generation+1, updated_at=:updated_at WHERE direction_id=:direction_id",
            {"direction_id": direction_id, "bundle": bundle_digest, "updated_at": now()},
        )
        return dict(self.get_direction(direction_id) or {})

    def put_prototype(
        self,
        direction_id: str,
        prototype: Mapping[str, Any],
        *,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        value = dict(prototype)
        task = self.get_task(task_id or (self.get_direction(direction_id) or {}).get("active_task_id"))
        if not task or task["direction_id"] != direction_id:
            raise ValueError("prototype requires a task owned by the research direction")
        if task["task_id"] != (self.get_direction(direction_id) or {}).get("active_task_id"):
            raise ValueError("prototype may only update the active ResearchTask")
        with self._db.transaction() as tx:
            existing = tx.fetch_one("SELECT payload_json FROM research_prototypes WHERE digest=:digest", {"digest": value["digest"]})
            if existing:
                return json.loads(str(existing["payload_json"]))
            tx.execute(
                "INSERT INTO research_prototypes(digest, direction_id, revision, parent_digest, source_bundle_digest, payload_json, created_at, task_id) VALUES (:digest, :direction_id, :revision, :parent_digest, :source_bundle_digest, :payload_json, :created_at, :task_id)",
                {
                    "digest": value["digest"], "direction_id": direction_id, "revision": value["revision"],
                    "parent_digest": value.get("parent_digest"), "source_bundle_digest": value["source_bundle_digest"],
                    "payload_json": canonical_json(value), "created_at": value["created_at"], "task_id": task["task_id"],
                },
            )
            tx.execute(
                "UPDATE research_directions SET current_prototype_digest=:digest, current_bundle_digest=:bundle, status='formulation', generation=generation+1, updated_at=:updated_at WHERE direction_id=:direction_id",
                {"digest": value["digest"], "bundle": value["source_bundle_digest"], "updated_at": now(), "direction_id": direction_id},
            )
        self.bind_task_formulation(
            task["task_id"],
            source_bundle_digest=str(value["source_bundle_digest"]),
            prototype_digest=str(value["digest"]),
            research_question=str(value.get("research_question") or ""),
        )
        return value

    def get_prototype(self, digest: str | None) -> dict[str, Any] | None:
        if not digest:
            return None
        row = self._db.fetch_one("SELECT payload_json FROM research_prototypes WHERE digest=:digest", {"digest": digest})
        return json.loads(str(row["payload_json"])) if row else None

    def list_prototypes(self, direction_id: str) -> list[dict[str, Any]]:
        rows = self._db.fetch_all("SELECT payload_json FROM research_prototypes WHERE direction_id=:direction_id ORDER BY revision", {"direction_id": direction_id})
        return [json.loads(str(row["payload_json"])) for row in rows]

    def accept(
        self,
        direction_id: str,
        *,
        expected_generation: int,
        prototype: Mapping[str, Any],
        brief: Mapping[str, Any],
        task_id: str | None = None,
        implementation_track_id: str | None = None,
    ) -> dict[str, Any]:
        with self._db.transaction() as tx:
            row = tx.fetch_one("SELECT generation FROM research_directions WHERE direction_id=:direction_id", {"direction_id": direction_id})
            if not row:
                raise ValueError("research direction is not initialized")
            selected_task_id = str(task_id or "").strip()
            if not selected_task_id:
                raise ValueError("acceptance requires an exact ResearchTask")
            task = tx.fetch_one(
                "SELECT direction_id, current_prototype_digest FROM research_tasks WHERE task_id=:task_id",
                {"task_id": selected_task_id},
            )
            if not task or str(task["direction_id"]) != direction_id:
                raise ValueError("ResearchTask does not belong to this direction")
            if str(task.get("current_prototype_digest") or "") != str(prototype["digest"]):
                raise ValueError("only the current ResearchPrototype for the selected task can be accepted")
            existing = tx.fetch_one(
                "SELECT prototype_digest, payload_json FROM research_automation_briefs WHERE task_id=:task_id ORDER BY created_at DESC",
                {"task_id": selected_task_id},
            )
            if existing:
                if str(existing["prototype_digest"]) != str(prototype["digest"]):
                    raise ValueError("another ResearchPrototype is already accepted for this ResearchTask")
                return json.loads(str(existing["payload_json"])) if existing else dict(brief)
            if int(row["generation"]) != int(expected_generation):
                raise ValueError(f"stale generation: expected {expected_generation}, current {row['generation']}")
            tx.execute(
                "INSERT INTO research_automation_briefs(digest, direction_id, prototype_digest, source_bundle_digest, payload_json, created_at, task_id, implementation_track_id) VALUES (:digest, :direction_id, :prototype_digest, :source_bundle_digest, :payload_json, :created_at, :task_id, :implementation_track_id)",
                {
                    "digest": brief["digest"], "direction_id": direction_id, "prototype_digest": prototype["digest"],
                    "source_bundle_digest": prototype["source_bundle_digest"], "payload_json": canonical_json(brief), "created_at": brief["created_at"],
                    "task_id": task_id,
                    "implementation_track_id": implementation_track_id,
                },
            )
            tx.execute(
                "UPDATE research_directions SET accepted_prototype_digest=:prototype, automation_brief_digest=:brief, status='handoff_ready', generation=generation+1, updated_at=:updated_at WHERE direction_id=:direction_id",
                {"prototype": prototype["digest"], "brief": brief["digest"], "updated_at": now(), "direction_id": direction_id},
            )
        return dict(brief)

    def get_brief(self, digest: str | None) -> dict[str, Any] | None:
        if not digest:
            return None
        row = self._db.fetch_one("SELECT payload_json FROM research_automation_briefs WHERE digest=:digest", {"digest": digest})
        return json.loads(str(row["payload_json"])) if row else None

    def get_brief_for_task(
        self,
        task_id: str,
        *,
        implementation_track_id: str | None = None,
    ) -> dict[str, Any] | None:
        statement = (
            "SELECT payload_json FROM research_automation_briefs "
            "WHERE task_id=:task_id"
        )
        parameters: dict[str, Any] = {"task_id": task_id}
        if implementation_track_id:
            statement += " AND implementation_track_id=:track_id"
            parameters["track_id"] = implementation_track_id
        statement += " ORDER BY created_at DESC"
        row = self._db.fetch_one(statement, parameters)
        return json.loads(str(row["payload_json"])) if row else None

    def activity(
        self,
        direction_id: str,
        stage: str,
        status: str,
        message: str,
        detail: Mapping[str, Any] | None = None,
        *,
        actor: str = "system:research_orchestrator",
        origin: str = "research_orchestrator",
        subject_ref: str | None = None,
        source_event_id: str | None = None,
    ) -> dict[str, Any]:
        external_id = str(source_event_id or "").strip() or None
        with self._db.transaction() as tx:
            if external_id:
                existing = tx.fetch_one(
                    "SELECT event_id, direction_id, seq, stage, status, message, detail_json, actor, origin, subject_ref, source_event_id, created_at FROM research_activity WHERE origin=:origin AND source_event_id=:source_event_id",
                    {"origin": origin, "source_event_id": external_id},
                )
                if existing:
                    return {**dict(existing), "detail": json.loads(str(existing["detail_json"]))}
            row = tx.fetch_one("SELECT COALESCE(MAX(seq), 0) AS seq FROM research_activity WHERE direction_id=:direction_id", {"direction_id": direction_id})
            seq = int(row["seq"] if row else 0) + 1
            event_id = f"activity-{direction_id}-{seq:06d}"
            event = {"event_id": event_id, "direction_id": direction_id, "seq": seq, "stage": stage, "status": status, "message": message, "detail": dict(detail or {}), "actor": actor, "origin": origin, "subject_ref": subject_ref, "source_event_id": external_id, "created_at": now()}
            tx.execute(
                "INSERT INTO research_activity(event_id, direction_id, seq, stage, status, message, detail_json, actor, origin, subject_ref, source_event_id, created_at) VALUES (:event_id, :direction_id, :seq, :stage, :status, :message, :detail_json, :actor, :origin, :subject_ref, :source_event_id, :created_at)",
                {**event, "detail_json": canonical_json(event["detail"])},
            )
        return event

    def activities(self, direction_id: str, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._db.fetch_all(
            "SELECT event_id, direction_id, seq, stage, status, message, detail_json, actor, origin, subject_ref, source_event_id, created_at FROM research_activity WHERE direction_id=:direction_id ORDER BY seq DESC",
            {"direction_id": direction_id},
        )[: max(1, min(int(limit), 500))]
        return [{**dict(row), "detail": json.loads(str(row["detail_json"]))} for row in reversed(rows)]

    def put_formulation_stage(
        self,
        *,
        run_id: str,
        direction_id: str,
        stage_index: int,
        stage_name: str,
        status: str,
        input_digest: str,
        output_digest: str | None,
        payload: Mapping[str, Any] | None,
        telemetry: Mapping[str, Any] | None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        value = {
            "run_id": str(run_id),
            "direction_id": str(direction_id),
            "task_id": str(task_id or (self.get_direction(direction_id) or {}).get("active_task_id") or ""),
            "stage_index": int(stage_index),
            "stage_name": str(stage_name),
            "status": str(status),
            "input_digest": str(input_digest),
            "output_digest": str(output_digest) if output_digest else None,
            "payload": dict(payload or {}),
            "telemetry": dict(telemetry or {}),
            "created_at": now(),
        }
        parameters = {
            **value,
            "payload_json": canonical_json(value["payload"]),
            "telemetry_json": canonical_json(value["telemetry"]),
        }
        existing = self._db.fetch_one(
            "SELECT run_id FROM research_formulation_stages WHERE run_id=:run_id AND stage_name=:stage_name",
            {"run_id": value["run_id"], "stage_name": value["stage_name"]},
        )
        if existing:
            self._db.execute(
                "UPDATE research_formulation_stages SET status=:status, input_digest=:input_digest, output_digest=:output_digest, payload_json=:payload_json, telemetry_json=:telemetry_json, task_id=:task_id, created_at=:created_at WHERE run_id=:run_id AND stage_name=:stage_name",
                parameters,
            )
        else:
            self._db.execute(
                "INSERT INTO research_formulation_stages(run_id, direction_id, stage_index, stage_name, status, input_digest, output_digest, payload_json, telemetry_json, task_id, created_at) VALUES (:run_id, :direction_id, :stage_index, :stage_name, :status, :input_digest, :output_digest, :payload_json, :telemetry_json, :task_id, :created_at)",
                parameters,
            )
        return value

    def formulation_stages(self, direction_id: str, *, run_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if run_id:
            rows = self._db.fetch_all(
                "SELECT * FROM research_formulation_stages WHERE direction_id=:direction_id AND run_id=:run_id ORDER BY stage_index",
                {"direction_id": direction_id, "run_id": run_id},
            )
        else:
            rows = self._db.fetch_all(
                "SELECT * FROM research_formulation_stages WHERE direction_id=:direction_id ORDER BY created_at DESC, stage_index DESC",
                {"direction_id": direction_id},
            )[: max(1, min(int(limit), 500))]
        return [
            {
                **{key: value for key, value in dict(row).items() if key not in {"payload_json", "telemetry_json"}},
                "stage_index": int(row["stage_index"]),
                "payload": json.loads(str(row["payload_json"])),
                "telemetry": json.loads(str(row["telemetry_json"])),
            }
            for row in rows
        ]

    def once(self, key: str, command: str, operation: Callable[[], Mapping[str, Any]]) -> dict[str, Any]:
        previous = self._db.fetch_one("SELECT command_name, result_json FROM research_commands WHERE idempotency_key=:key", {"key": key})
        if previous:
            if str(previous["command_name"]) != command:
                raise ValueError("idempotency key is bound to another command")
            return json.loads(str(previous["result_json"]))
        result = dict(operation())
        try:
            self._db.execute(
                "INSERT INTO research_commands(idempotency_key, command_name, result_json, created_at) VALUES (:key, :command, :result, :created_at)",
                {"key": key, "command": command, "result": canonical_json(result), "created_at": now()},
            )
        except Exception:
            winner = self._db.fetch_one("SELECT command_name, result_json FROM research_commands WHERE idempotency_key=:key", {"key": key})
            if not winner or str(winner["command_name"]) != command:
                raise
            return json.loads(str(winner["result_json"]))
        return result


__all__ = ["MIGRATIONS", "OrchestratorRepository", "db"]
