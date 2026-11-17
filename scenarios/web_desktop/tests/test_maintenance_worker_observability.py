import json
from pathlib import Path


def test_running_intent_is_not_presented_as_proof_of_worker_activity():
    ui=json.loads((Path(__file__).resolve().parents[1]/'webui.json').read_text(encoding='utf-8'))
    widgets={row['id']:row for row in ui['ui']['application']['desktop']['pageSchema']['widgets']}
    jobs=widgets['maintenance-migration-jobs']
    assert jobs['inputs']['subtitleKey']=='execution_status'
    for identity,key in [('maintenance-migration-jobs','meta'),('maintenance-migration-progress','fields')]:
        fields={field['key'] for field in widgets[identity]['inputs'][key]}
        assert {'execution_status','files','bytes','pending_directories','updated_at','worker_heartbeat_at'} <= fields
    controls=widgets['maintenance-migration-controls']
    actions={row['params']['action']:row for row in controls['actions']}
    assert set(actions)=={'status','resume','cancel'}
    assert all(row['params']['job_id']=='$state.storageMigrationJob.job_id' for row in actions.values())
