from __future__ import annotations
import importlib.util, json, os, subprocess, tempfile
from pathlib import Path
import yaml

ROOT=Path(__file__).resolve().parents[1]

def conformance_data_root():
    """Keep admitted fixture evidence where the trusted worker can inspect it."""
    task_runtime=os.environ.get("ADAOS_TASK_RUNTIME_DIR")
    if task_runtime:
        root=Path(task_runtime)/"tlp_structured_proof_02"/"runner-conformance"
        root.mkdir(parents=True,exist_ok=True)
        return root
    # Ordinary local pytest remains hermetic and outside the candidate tree.
    return Path(tempfile.mkdtemp(prefix="tlp-structured-proof-"))
def module():
    spec=importlib.util.spec_from_file_location("tlp_runner",ROOT/"handlers"/"main.py"); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m
def request(arm_id="model_pool2_tlp_centered", role="intervention"):
    limits=[{"name":"train_samples","maximum":128,"unit":"samples"},{"name":"validation_samples","maximum":64,"unit":"samples"},{"name":"robustness_samples","maximum":64,"unit":"samples"},{"name":"epochs","maximum":3,"unit":"epochs"},{"name":"paired_units","maximum":1,"unit":"pairs"},{"name":"wall_time","maximum":900,"unit":"seconds"}]
    return {"experiment_id":"e1","experiment_revision_id":"r1","trial_id":"t1","run_id":f"run-{role}","attempt_number":1,"profile":"preflight","seed":17,"arm":{"id":arm_id,"role":role},"conditions":{"dataset":{},"operators":{},"execution":{},"randomization":{},"analysis":{},"tracker":{},"runner":{}},"profile_conditions":{"source_stage_id":"workflow_smoke","device":"cpu","epochs":3,"seeds":[17],"evidence_class":"workflow_smoke","inference_allowed":False,"network_mode":"unrestricted","workload":{"mode":"bounded","limits":limits},"input_policy":{"source":"deterministic_contract_fixture","readiness":"required_before_execution","sampling":"deterministic_prefix"}}}

def test_manifest_boundary_and_exact_abi():
    manifest=yaml.safe_load((ROOT/"skill.yaml").read_text(encoding="utf-8")); assert manifest["version"] and manifest["updated_at"]
    assert manifest["research_direction"]["owner"]=="skill:tlp_structured_proof_02"
    assert manifest["provider_contracts"]==[{"contract":"adaos.research.runner.v1","capability":"research.runner","operations":["prepare_attempt","collect_attempt","verify_artifact","dataset_status"]}]
    tools={item["name"]:item for item in manifest["tools"]}
    assert tools["prepare_attempt"]["input_schema"]["properties"]["request"]["additionalProperties"] is True
    for name in ("prepare_attempt","collect_attempt","verify_artifact","dataset_status"):
        output=tools[name]["output_schema"]
        assert output["additionalProperties"] is True
        assert isinstance(output["properties"],dict) and output["properties"]
    split_schema=tools["dataset_status"]["output_schema"]["properties"]["split_bindings"]
    assert split_schema["additionalProperties"] is False
    assert set(split_schema["properties"])=={"validation","robustness","test"}
    observation=tools["collect_attempt"]["output_schema"]["properties"]["observations"]["items"]
    assert set(observation["properties"])=={"metric","value","value_type","unit","direction","split_role","dataset_digest","step","aggregation","observed_at","producer","evidence_role","event_id"}
    assert "result" in tools["collect_attempt"]["output_schema"]["required"]
    assert tools["collect_attempt"]["output_schema"]["allOf"]

def test_dataset_identities(monkeypatch,tmp_path):
    monkeypatch.setenv("ADAOS_SKILL_INTERNAL_DATA_ROOT",str(tmp_path)); m=module(); status=m.dataset_status(); assert not status["ready"]
    dataset=tmp_path/"datasets"/"stl10_v1"; dataset.mkdir(parents=True); (dataset/"accepted_dataset_manifest.json").write_text("{}",encoding="utf-8"); status=m.dataset_status(); splits=status["split_bindings"]
    assert status["ready"] and status["execution_ready_without_network"] and set(splits)=={"validation","robustness","test"}
    assert len({x["digest"] for x in splits.values()})==3 and len({x["dataset_digest"] for x in splits.values()})==1
    assert splits["test"]["sealed"] and all("\\" not in x["locator"] for x in splits.values())

def test_operator_and_analysis():
    m=module(); assert m.centered_tlp_pool2([[1,4],[2,3]],[0,0,0,0])==4
    pairs=[{"maxpool_accuracy":float(i),"tlp_accuracy":float(i)+1} for i in range(10)]
    assert m.paired_analysis(pairs)==m.paired_analysis(pairs) and m.paired_analysis(pairs)["mean_diff"]==1

def test_production_provider_sequence_both_arms_without_private_selector(monkeypatch):
    data_root=conformance_data_root()
    monkeypatch.setenv("ADAOS_SKILL_INTERNAL_DATA_ROOT",str(data_root)); m=module()
    expected=["run_log.json","evaluation_audit.json","implementation_observation.json","result_record.json","artifacts_index.json"]
    pairing_digests=[]
    for arm_id,role in (("model_pool2_max","baseline"),("model_pool2_tlp_centered","intervention")):
        standard_request=request(arm_id,role)
        assert "conformance_fixture" not in standard_request["conditions"]
        prepared=m.prepare_attempt(standard_request); assert prepared["expected_outputs"]==expected
        done=subprocess.run(prepared["command"],cwd=prepared["working_directory"],env={**os.environ,"ADAOS_SKILL_INTERNAL_DATA_ROOT":str(data_root)},capture_output=True,text=True)
        assert done.returncode==0,done.stderr; work=Path(prepared["working_directory"]); assert all((work/n).is_file() for n in expected)
        run=json.loads((work/"run_log.json").read_text(encoding="utf-8")); assert run["seeds"]==["seed-17"] and run["network"]=={"mode":"unrestricted","accessed":False}
        assert run["network_policy"]=={"requested_mode":"unrestricted","enforcement":"provider_not_isolated","accessed":False,"observation_not_isolation":True}
        assert run["split_usage"]["test_used"] is False
        assert all(v["within_limits"] for v in json.loads((work/"counts_report.json").read_text(encoding="utf-8"))["checks"].values())
        assert run["input_policy"]["source"]=="deterministic_contract_fixture"
        assert json.loads((work/"evaluation_audit.json").read_text(encoding="utf-8"))["test_access"]==[]
        observation=json.loads((work/"implementation_observation.json").read_text(encoding="utf-8")); assert observation["observed"]["input_shape"]==[1,3,96,96]
        result_record=json.loads((work/"result_record.json").read_text(encoding="utf-8")); assert set(("status","result","observations","evidence_class","tracker_session_calls"))<=set(result_record)
        assert observation["arm"]==standard_request["arm"] and observation["observed"]["pool2_only_intervention"] is True
        assert observation["observed"]["optimizer_steps"]==3 and observation["observed"]["theta_initial_max_abs"]==0.0
        assert run["workload"]["observed"]["epochs"]==3 and run["workload"]["observed"]["wall_time"]<=900
        index=json.loads((work/"artifacts_index.json").read_text(encoding="utf-8")); assert all(x["path"]!="artifacts_index.json" for x in index["files"])
        result=m.collect_attempt(prepared["output_ref"]); assert result["complete"] and result["tracker_session_calls"]==0
        assert result["result"]["arm_id"]==arm_id and result["result"]["seed"]==17
        assert result["result"]["evidence_class"]=="workflow_smoke" and isinstance(result["result"]["primary_metric"],float)
        pairing_digests.append(result["result"]["pairing_identity_digest"])
        primary=[o for o in result["observations"] if o["metric"]["name"]=="primary_metric"]
        assert len(primary)==1 and primary[0]["value"]==result["result"]["primary_metric"]
        assert all(o["metric"]["name"] and "value" in o for o in result["observations"])
        assert all(a["role"] for a in result["artifacts"])
        assert {a["digest"] for a in result["artifacts"]}=={x["digest"] for x in index["files"]}
        assert all(m.verify_artifact(a["uri"],a["digest"])["ok"] for a in result["artifacts"])
    assert len(set(pairing_digests))==1

def test_compiled_plan_and_system_identity():
    m=module()
    assert m.EXPERIMENT_PLAN_DIGEST=="sha256:3988b4d349dbf2cb1a9e8375c720835392c0f0cfe5ae1283ee2db7e3fed34161"
    assert m.SYSTEM_DIGEST=="sha256:8cc460031faa79704367eb066d596c735552b157743edba7e04482f0b6222285"

def test_accepted_dataset_is_not_substituted(monkeypatch,tmp_path):
    monkeypatch.setenv("ADAOS_SKILL_INTERNAL_DATA_ROOT",str(tmp_path)); m=module(); called={}
    def scientific(work,r,p): called["source"]=r["profile_conditions"]["input_policy"]["source"]; return 23
    monkeypatch.setattr(m,"_scientific",scientific); r=request(); r["profile_conditions"]["input_policy"]={"source":"accepted_dataset","readiness":"required_before_execution","sampling":"deterministic_prefix"}
    # A stale private selector must be inert: input_policy.source is the sole ABI.
    r["conditions"]["conformance_fixture"]=True
    prepared=m.prepare_attempt(r)
    assert m._execute(Path(prepared["working_directory"])/"attempt_spec.json")==23 and called["source"]=="accepted_dataset"

def test_rejects_old_abi(monkeypatch,tmp_path):
    monkeypatch.setenv("ADAOS_SKILL_INTERNAL_DATA_ROOT",str(tmp_path)); r=request(); r["profile"]="stage-smoke"
    try: module().prepare_attempt(r)
    except ValueError: pass
    else: raise AssertionError("obsolete lifecycle profile was accepted")

def test_incomplete_collection_has_contract_result(monkeypatch,tmp_path):
    monkeypatch.setenv("ADAOS_SKILL_INTERNAL_DATA_ROOT",str(tmp_path)); m=module()
    prepared=m.prepare_attempt(request("model_pool2_max","baseline"))
    assert m.collect_attempt(prepared["output_ref"])["result"] is None
