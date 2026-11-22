"""Metadata-only admission regressions; no Application code is imported."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import validate_registry as registry


class CatalogAdmissionTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get('ADAOS_TEST_TMP_ROOT', Path(__file__).resolve().parents[2] / '.tmp/catalog-validation'))
        scratch.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=scratch)
        self.root = Path(self.directory.name).resolve()
        assert self.root.is_relative_to(scratch.resolve())
        self.addCleanup(self.directory.cleanup)
        self.catalog = {'version': 2, 'projects': [], 'skills': [], 'scenarios': []}
        self.project = {'project_id': 'example', 'version': '0.1.0', 'release_digest': 'sha256:' + 'a' * 64,
                        'source_ref': {'revision': 'b' * 40}, 'source_branch': 'main',
                        'components': [{'digest': 'sha256:' + 'c' * 64}]}
        self.record = {'release': {'application_id': 'example', 'release_digest': self.project['release_digest'],
                                   'project_release': self.project}}
        self.entry = {'application_id': 'example', 'version': '0.1.0',
                      'release_digest': self.project['release_digest'], 'path': 'semantic/catalog/release.json'}
        self.write_current()
        self.legacy = {}
        self.previous = {}
        self.available = True
        self.mock = patch.object(registry.subprocess, 'run', side_effect=self.git)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def write(self, name, value):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value), encoding='utf-8')

    def write_current(self):
        self.record['projection_digest'] = registry.canonical_digest({k: v for k, v in self.record.items() if k != 'projection_digest'})
        self.entry['projection_digest'] = self.record['projection_digest']
        self.write('registry.json', self.catalog)
        self.write(self.entry['path'], self.record)
        self.write('semantic/catalog/index.json', {'applications': {}, 'releases': {'example': self.entry}})

    def git(self, args, **kwargs):
        if 'show' not in args:
            return subprocess.CompletedProcess(args, 0, stdout='')
        ref, name = args[-1].split(':', 1)
        if not self.available:
            return subprocess.CompletedProcess(args, 1, stdout=b'')
        index = self.legacy if ref == registry.LEGACY_PUBLISHED_CATALOG_REVISION else self.previous
        value = self.catalog if name == 'registry.json' else index if name == 'semantic/catalog/index.json' else {}
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(value).encode())

    def proof(self):
        value = {'schema': 'adaos.application.catalog_test_proof.v1', 'status': 'passed',
                 'project_id': 'example', 'version': '0.1.0', 'release_digest': self.project['release_digest'],
                 'source_revision': self.project['source_ref']['revision'], 'source_branch': 'main',
                 'package_digests': [self.project['components'][0]['digest']],
                 'test_plan': {'schema': 'adaos.project.test_plan.v1', 'complete_suite': True, 'suites': ['project:example']},
                 'packages': [{'ref': 'project:example', 'log_sha256': 'd' * 64, 'statuses': ['passed']}]}
        for name in ('source_snapshot_digest', 'environment_digest', 'candidate_acceptance_digest', 'test_receipt_digest'):
            value[name] = 'sha256:' + 'e' * 64
        value['proof_digest'] = registry.canonical_digest(value)
        self.write(registry.test_proof_path(self.root, value['release_digest']).relative_to(self.root), value)
        return value

    def test_unchanged_published_legacy_can_be_merged_from_main(self):
        self.legacy = {'releases': {'example': copy.deepcopy(self.entry)}}
        self.assertTrue(registry.validate(self.root, base_ref='f' * 40)['ok'])

    def test_failed_previous_push_cannot_launder_a_new_release(self):
        self.previous = {'releases': {'example': copy.deepcopy(self.entry)}}
        self.assertFalse(registry.validate(self.root, base_ref='f' * 40)['ok'])

    def test_changed_legacy_projection_requires_full_proof(self):
        self.legacy = {'releases': {'example': copy.deepcopy(self.entry)}}
        self.project['source_ref']['revision'] = 'f' * 40
        self.write_current()
        self.assertFalse(registry.validate(self.root)['ok'])

    def test_legacy_record_tampering_is_not_hidden_by_its_old_digest(self):
        self.legacy = {'releases': {'example': copy.deepcopy(self.entry)}}
        self.project['source_ref']['revision'] = 'f' * 40
        self.write(self.entry['path'], self.record)
        result = registry.validate(self.root)
        self.assertFalse(result['ok'])
        self.assertIn('public_application_projection_digest_mismatch', [row['code'] for row in result['errors']])

    def test_exact_new_release_with_full_proof_is_accepted(self):
        self.proof()
        self.assertTrue(registry.validate(self.root)['ok'])

    def test_a_present_invalid_proof_cannot_use_the_legacy_exception(self):
        self.legacy = {'releases': {'example': copy.deepcopy(self.entry)}}
        proof = self.proof()
        proof['status'] = 'failed'
        self.write(registry.test_proof_path(self.root, proof['release_digest']).relative_to(self.root), proof)
        self.assertFalse(registry.validate(self.root)['ok'])

    def test_unavailable_legacy_baseline_fails_closed(self):
        self.available = False
        with self.assertRaisesRegex(ValueError, 'catalog_base_revision_unavailable'):
            registry.validate(self.root)


if __name__ == '__main__':
    unittest.main()
