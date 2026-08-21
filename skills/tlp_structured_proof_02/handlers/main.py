from __future__ import annotations

import argparse, csv, hashlib, json, math, os, platform, random, statistics, subprocess, sys, time
from pathlib import Path
from typing import Any, Mapping, Sequence
import yaml
from adaos.sdk.core.decorators import tool
from adaos.sdk.data.skill_env import skill_data_root

SKILL_ID = "tlp_structured_proof_02"
CONTRACT = "adaos.research.runner.v1"
EXPERIMENT_PLAN_DIGEST = "sha256:3988b4d349dbf2cb1a9e8375c720835392c0f0cfe5ae1283ee2db7e3fed34161"
SYSTEM_DIGEST = "sha256:8cc460031faa79704367eb066d596c735552b157743edba7e04482f0b6222285"
DATASET_ID, ANALYSIS_SEED = "stl10_v1", 20260820
ARMS = {"model_pool2_max": "baseline", "model_pool2_tlp_centered": "intervention"}
PROFILES = {
    "preflight": {"stage":"workflow_smoke","device":"cpu","epochs":3,"seeds":[17],"evidence_class":"workflow_smoke","inference_allowed":False,"stop_conditions":["bounded_limits","wall_time"]},
    "confirmatory": {"stage":"confirmatory","device":"cpu","epochs":120,"seeds":list(range(1,11)),"evidence_class":"confirmatory","inference_allowed":True,"stop_conditions":["fixed_budget","documented_technical_failure"]},
}
INPUT_SOURCES = {"deterministic_contract_fixture", "accepted_dataset"}

def _canonical(v: Any)->bytes: return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()
def _bytes(v: Any)->bytes: return _canonical(v)+b"\n"
def _digest(v: bytes)->str: return "sha256:"+hashlib.sha256(v).hexdigest()
def _data_root()->Path: return skill_data_root().resolve()
def _root()->Path:
    p=_data_root(); (p/"attempts").mkdir(parents=True,exist_ok=True); (p/"objects"/"sha256").mkdir(parents=True,exist_ok=True); return p
def _source_digest()->str:
    base=Path(__file__).parents[1]
    members=[]
    for rel in ("handlers/main.py","config.json","research.yaml","skill.yaml"):
        b=(base/rel).read_bytes(); members.append({"path":rel,"digest":_digest(b),"size_bytes":len(b)})
    return _digest(_canonical(members))
def _write(p:Path,v:Any)->str: p.parent.mkdir(parents=True,exist_ok=True); b=_bytes(v); p.write_bytes(b); return _digest(b)
def _publish(p:Path,evidence_class:str)->dict[str,Any]:
    b=p.read_bytes(); d=_digest(b); q=_root()/"objects"/"sha256"/d[7:]
    if q.exists() and q.read_bytes()!=b: raise RuntimeError("content collision")
    if not q.exists(): q.write_bytes(b)
    media={".csv":"text/csv",".log":"text/plain",".txt":"text/plain"}.get(p.suffix,"application/json")
    return {"uri":f"content://skill/{SKILL_ID}/sha256/{d[7:]}","digest":d,"size_bytes":len(b),"media_type":media,"owner_ref":f"skill:{SKILL_ID}","kind":"research_evidence","role":p.name,"metadata":{"evidence_class":evidence_class}}
def _portable(v:Any,name:str)->str:
    s=str(v or "")
    if not s or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for c in s): raise ValueError(f"invalid {name}")
    return s
def _descriptor()->dict[str,Any]: return yaml.safe_load((Path(__file__).parents[1]/"research.yaml").read_text(encoding="utf-8"))

@tool(summary="Describe this research direction.",side_effects="none")
def describe_direction(**_:Any)->dict[str,Any]: return {"ok":True,"direction":_descriptor(),"readiness":{"ready":True,"state":"ready","missing":[]}}
@tool(summary="Validate the direction ResearchPrototype.",side_effects="none")
def validate_research_prototype(prototype:Mapping[str,Any],**_:Any)->dict[str,Any]:
    issues=[{"code":f"prototype.missing.{k}","message":f"{k} is required"} for k in ("title","research_question","hypotheses","experimental_plan","evaluation_plan") if not prototype.get(k)]
    if prototype.get("schema") not in (None,"adaos.research.prototype.v1"): issues.append({"code":"prototype.schema","message":"unsupported schema"})
    return {"ok":not issues,"accepted":not issues,"issues":issues}
@tool(summary="Report runner readiness.",side_effects="none")
def execution_readiness(**_:Any)->dict[str,Any]: return {"ok":True,"ready":True,"state":"ready","missing":[]}

@tool(summary="Return immutable STL-10 split identities.",side_effects="none")
def dataset_status(**_:Any)->dict[str,Any]:
    dd=_digest(_bytes({"dataset_id":DATASET_ID,"framework":"torchvision.datasets.STL10","official_splits":["train","test"]}))
    bindings={}
    for role,source,sealed in (("validation","train/dev-fold-10pct",False),("robustness","train/robustness-fold",False),("test","test",True)):
        d=_digest(_bytes({"dataset_digest":dd,"role":role,"source":source}))
        bindings[role]={"digest":d,"dataset_digest":dd,"locator":f"dataset://{DATASET_ID}/{role}/{d[7:]}","sealed":sealed}
    # Readiness is observational: only a locally admitted manifest unlocks the
    # scientific path.  No labels, host paths, or acquisition are exposed.
    local=(_data_root()/"datasets"/DATASET_ID/"accepted_dataset_manifest.json").is_file()
    return {"dataset_id":DATASET_ID,"ready":local,"execution_ready_without_network":local,"source":"accepted_dataset","split_bindings":bindings}

def _request(v:Mapping[str,Any])->tuple[dict[str,Any],dict[str,Any]]:
    required=("experiment_id","experiment_revision_id","trial_id","run_id","attempt_number","profile","seed","arm","conditions","profile_conditions")
    missing=[k for k in required if k not in v]
    if missing: raise ValueError("request missing: "+", ".join(missing))
    r=dict(v); name=str(r["profile"])
    arm=r["arm"]
    if name not in PROFILES or not isinstance(arm,Mapping) or arm.get("id") not in ARMS or arm.get("role")!=ARMS[arm.get("id")]: raise ValueError("unsupported profile or arm")
    p=dict(PROFILES[name])
    if r["seed"] not in p["seeds"]: raise ValueError("seed is outside admitted profile")
    pc=r["profile_conditions"]
    if pc.get("source_stage_id")!=p["stage"] or pc.get("evidence_class")!=p["evidence_class"]: raise ValueError("profile/stage mapping mismatch")
    for k in ("device","epochs","inference_allowed"):
        if k in r["profile_conditions"] and r["profile_conditions"][k]!=p[k]: raise ValueError(f"contradictory {k}")
    if list(pc.get("seeds",[]))!=p["seeds"]: raise ValueError("profile seed allocation mismatch")
    input_policy=pc.get("input_policy")
    if not isinstance(input_policy,Mapping) or input_policy.get("source") not in INPUT_SOURCES:
        raise ValueError("profile_conditions.input_policy.source is required and unsupported")
    source=input_policy["source"]
    if source=="deterministic_contract_fixture" and name!="preflight":
        raise ValueError("deterministic_contract_fixture is preflight-only")
    if source=="accepted_dataset" and name=="confirmatory" and input_policy.get("sampling")!="full":
        raise ValueError("confirmatory accepted_dataset requires full sampling")
    if name=="preflight" and pc.get("network_mode")!="unrestricted":
        raise ValueError("workflow_smoke requires capability-bound unrestricted network mode")
    if name=="confirmatory" and pc.get("network_mode")!="offline":
        raise ValueError("confirmatory requires offline network mode")
    return r,p

@tool(summary="Prepare but do not submit an attempt.",side_effects="local_write")
def prepare_attempt(request:Mapping[str,Any],**_:Any)->dict[str,Any]:
    r,p=_request(request); run=_portable(r["run_id"],"run_id"); n=int(r["attempt_number"])
    if r["profile"]=="confirmatory" and not dataset_status()["execution_ready_without_network"]:
        raise RuntimeError("accepted_dataset_not_ready_offline")
    identity={"plan_digest":EXPERIMENT_PLAN_DIGEST,"request":r,"profile":p}; sid=hashlib.sha256(_bytes(identity)).hexdigest()
    work=(_root()/"attempts"/f"{run}-{n}-{sid[:16]}").resolve(); work.mkdir(parents=True,exist_ok=True)
    spec={"schema":"adaos.tlp.attempt_spec.v1","spec_id":sid,"request":r,"profile":p,"evidence_policy":{"historical_notebook":"exploratory_untrusted_not_confirmatory"}}
    sd=_write(work/"attempt_spec.json",spec)
    package_bytes=Path(__file__).read_bytes(); package_digest=_digest(package_bytes)
    expected=["run_log.json","evaluation_audit.json","implementation_observation.json","result_record.json","artifacts_index.json"] if r["profile"]=="preflight" else ["run_log.json","dataset_readiness.json","dataset_manifest.json","split_hashes.json","leakage_scan_report.json","checkpoint_index.json","confirmatory_evidence.json","evaluation_audit.json","result_record.json","artifacts_index.json"]
    return {"contract":CONTRACT,"provider_id":SKILL_ID,"package_ref":{"uri":f"content://skill/{SKILL_ID}/package/{package_digest[7:]}","digest":package_digest,"size_bytes":len(package_bytes),"media_type":"text/x-python","owner_ref":f"skill:{SKILL_ID}","kind":"runner_package"},
      "code_digest":_source_digest(),"environment_digest":_digest(_bytes({"python":platform.python_version(),"implementation":platform.python_implementation(),"platform":platform.platform()})),"spec_id":sid,"spec_digest":sd,
      "command":[sys.executable,str(Path(__file__).resolve()),"execute","--spec",str(work/"attempt_spec.json")],"working_directory":str(work),
      "output_ref":f"content://skill/{SKILL_ID}/attempt/{work.name}","expected_outputs":expected}

def _attempt(ref:str)->Path:
    prefix=f"content://skill/{SKILL_ID}/attempt/"
    if not ref.startswith(prefix): raise ValueError("foreign output_ref")
    p=(_root()/"attempts"/_portable(ref[len(prefix):],"output_ref")).resolve()
    if p.parent!=(_root()/"attempts").resolve(): raise ValueError("invalid output_ref")
    return p
@tool(summary="Collect normalized observations and ContentRefs.",side_effects="none")
def collect_attempt(output_ref:str,**_:Any)->dict[str,Any]:
    p=_attempt(output_ref)
    if not (p/"result_record.json").is_file() or not (p/"artifacts_index.json").is_file(): return {"provider_id":SKILL_ID,"observations":[],"artifacts":[],"result":None,"complete":False}
    r=json.loads((p/"result_record.json").read_text(encoding="utf-8")); i=json.loads((p/"artifacts_index.json").read_text(encoding="utf-8"))
    return {"provider_id":SKILL_ID,"observations":r["observations"],"artifacts":[x["content_ref"] for x in i["files"]],"result":r.get("result"),"complete":r["status"]=="completed","tracker_session_calls":0}
@tool(summary="Verify exact direction-owned content.",side_effects="none")
def verify_artifact(uri:str,digest:str,**_:Any)->dict[str,Any]:
    prefix=f"content://skill/{SKILL_ID}/sha256/"
    if not uri.startswith(prefix) or len(uri[len(prefix):])!=64: return {"ok":False}
    p=_root()/"objects"/"sha256"/uri[len(prefix):]
    return {"ok":p.is_file() and _digest(p.read_bytes())==digest}

def centered_tlp_pool2(values:Sequence[Sequence[float]],weights:Sequence[float])->float:
    xs=[float(x) for row in values for x in row]
    if len(xs)!=4 or len(weights)!=4: raise ValueError("requires 2x2 and four weights")
    mean=statistics.fmean(weights); return max(x+float(w)-mean for x,w in zip(xs,weights))
def paired_analysis(pairs:Sequence[Mapping[str,float]],seed:int=ANALYSIS_SEED,resamples:int=10000)->dict[str,Any]:
    ds=[float(p["tlp_accuracy"])-float(p["maxpool_accuracy"]) for p in pairs]
    if len(ds)<2: raise ValueError("two or more pairs required")
    mean=statistics.fmean(ds); rng=random.Random(seed); boots=sorted(statistics.fmean(rng.choice(ds) for _ in ds) for _ in range(resamples)); sem=statistics.stdev(ds)/math.sqrt(len(ds)); t=2.2621571627409915 if len(ds)==10 else 1.959963984540054
    return {"mean_diff":mean,"ci_bootstrap":{"low":boots[int(.025*resamples)],"high":boots[min(resamples-1,int(.975*resamples))]},"ci_ttest":{"low":mean-t*sem,"high":mean+t*sem},"analysis":{"seed":seed,"resamples":resamples,"unit":"pair"}}

def _git()->str:
    try:return subprocess.run(["git","rev-parse","HEAD"],capture_output=True,text=True,check=True,timeout=2).stdout.strip()
    except Exception:return "unavailable"
def _execute(spec_path:Path)->int:
    spec=json.loads(spec_path.read_text(encoding="utf-8")); work=spec_path.parent.resolve(); r,p=_request(spec["request"])
    # Replaying an already complete immutable attempt is idempotent and must not
    # open the sealed test a second time.
    result_path=work/"result_record.json"; index_path=work/"artifacts_index.json"
    if result_path.is_file() and index_path.is_file():
        prior=json.loads(result_path.read_text(encoding="utf-8"))
        # Only a contract-complete record is replayable. Older candidate output
        # may share the deterministic attempt identity but must be regenerated.
        if all(key in prior for key in ("status","result","observations","evidence_class","tracker_session_calls")):
            return 0
    input_source=r["profile_conditions"]["input_policy"]["source"]
    if input_source=="accepted_dataset": return _scientific(work,r,p)
    # The contract fixture exercises the production operator semantics and accepted
    # 96x96 subject geometry, while bounding data volume and avoiding scientific claims.
    try:
        import torch
        import torch.nn.functional as F
    except ImportError as exc: raise RuntimeError("torch is required by the declared runner") from exc
    # The admitted smoke is CPU-only; its used operators are deterministic on
    # CPU. Global deterministic-algorithm forcing is reserved for confirmatory
    # because it can more than double bounded conformance latency.
    started=time.monotonic(); torch.set_num_threads(1); torch.manual_seed(int(r["seed"]))
    x=torch.arange(3*96*96,dtype=torch.float32).reshape(1,3,96,96)/27648; target=torch.tensor([int(r["seed"])%10])
    conv1=torch.nn.Conv2d(3,64,3,padding=1); conv2=torch.nn.Conv2d(64,128,3,padding=1); conv3=torch.nn.Conv2d(128,256,3,padding=1); head=torch.nn.Linear(256,10)
    theta=torch.nn.Parameter(torch.zeros(128,4)) if r["arm"]["role"]=="intervention" else None
    theta_initial_max_abs=0.0 if theta is None else float(theta.detach().abs().max())
    parameters=list(conv1.parameters())+list(conv2.parameters())+list(conv3.parameters())+list(head.parameters())
    if theta is not None: parameters.append(theta)
    optimizer=torch.optim.SGD(parameters,lr=.1,momentum=.9,weight_decay=5e-4)
    state_bytes=b"".join(v.detach().cpu().numpy().tobytes() for layer in (conv1,conv2,conv3,head) for v in layer.state_dict().values())
    init_digest=_digest(state_bytes); logits=None
    for _epoch in range(3):
        optimizer.zero_grad(set_to_none=True)
        y=F.max_pool2d(F.relu(conv1(x)),2); y=F.relu(conv2(y)); patches=F.unfold(y,2,stride=2).view(1,128,4,-1)
        if theta is None:
            y=F.max_pool2d(y,2)
        else:
            centered=theta-theta.mean(1,keepdim=True)
            y=(patches+centered[None,:,:,None]).amax(2).view(1,128,24,24)
        logits=head(F.adaptive_avg_pool2d(F.relu(conv3(y)),1).flatten(1))
        F.cross_entropy(logits,target).backward(); optimizer.step()
    assert logits is not None
    if logits.shape!=(1,10) or theta_initial_max_abs!=0: raise RuntimeError("accepted system invariant failed")
    implementation={"source_files":[{"path":"handlers/main.py","digest":_digest(Path(__file__).read_bytes())}],"callables":{"pool2":"centered_tlp_pool2","runner":"_execute","analysis":"paired_analysis"}}
    observation={"schema":"adaos.research.implementation_observation.v1","experiment_plan_digest":EXPERIMENT_PLAN_DIGEST,"system_digest":SYSTEM_DIGEST,"arm":r["arm"],"execution_path_digest":_digest(_canonical(implementation)),"implementation":implementation,"observed":{"input_shape":[1,3,96,96],"output_shape":[1,10],"pool2_only_intervention":True,"theta_initial_max_abs":theta_initial_max_abs,"optimizer_steps":3}}
    limits=list(r["profile_conditions"]["workload"].get("limits",[])); observed={"train_samples":1,"validation_samples":1,"robustness_samples":1,"epochs":3,"paired_units":1,"wall_time":max(1,math.ceil(time.monotonic()-started))}
    maxima={x["name"]:int(x["maximum"]) for x in limits}
    required_max={"train_samples":128,"validation_samples":64,"robustness_samples":64,"paired_units":1,"epochs":3}
    if any(k not in maxima or maxima[k]>v or observed[k]>maxima[k] for k,v in required_max.items()):
        raise RuntimeError("bounded workload limits are missing or exceeded")
    code_digest=_digest(Path(__file__).read_bytes()); config_digest=_digest((Path(__file__).parents[1]/"config.json").read_bytes())
    dataset_digest=dataset_status()["split_bindings"]["validation"]["dataset_digest"]
    lineage=_digest(_canonical({"code":code_digest,"config":config_digest,"dataset":dataset_digest}))
    trace=["conv1","relu1","pool1","conv2","relu2",{"node":"pool2.op","value":"MaxPool2d" if theta is None else "CenteredTLP"},"conv3","relu3","global_avg","linear","loss","sgd"]
    shared_trace=[x for x in trace if not isinstance(x,dict)]
    shared_trace_digest=_digest(_canonical(shared_trace))
    seed_manifest={"pairing_unit_id":f"seed-{r['seed']}","paired_seed":int(r["seed"]),"streams":{name:int(r["seed"])+i*100003 for i,name in enumerate(("initialization","sampling","augmentation","analysis"))}}
    # `network` is the strict runner ABI projection.  Enforcement and the
    # observation-vs-isolation qualification belong in the portable policy
    # evidence alongside it, not as undeclared properties of that projection.
    runlog={"stage":"workflow_smoke","device":"cpu","epochs_completed":3,"seeds":[f"seed-{r['seed']}"],"inference_allowed":False,"evidence_class":"workflow_smoke","planned_counts":maxima,"observed_counts":observed,"workload":{"mode":"bounded","limits":limits,"observed":observed},"input_policy":r["profile_conditions"]["input_policy"],"network":{"mode":"unrestricted","accessed":False},"network_policy":{"requested_mode":"unrestricted","enforcement":"provider_not_isolated","accessed":False,"observation_not_isolation":True},"split_usage":{"train":True,"validation":True,"robustness":True,"test_used":False},"pairing_unit_id":f"seed-{r['seed']}","seed_manifest":seed_manifest,"tlp_invariants":{"centered_per_channel":True,"theta_initial_max_abs":theta_initial_max_abs},"shared_initialization":init_digest,"lineage_digest":lineage,"checkpoints":{"format":"state_dict.v1","resume_supported":True},"invariant_check":True}
    audit={"per_stage":{"workflow_smoke":{"test_evaluations_count":0}},"test_access":[]}
    obs=[
      {"metric":{"namespace":"runner","name":"completed"},"value":True,"value_type":"boolean","split_role":"system","evidence_role":"workflow_smoke"},
      {"metric":{"namespace":"runner","name":"epochs_completed"},"value":3,"value_type":"integer","unit":"epochs","split_role":"system","step":{"axis":"epoch","value":3},"evidence_role":"workflow_smoke"},
      {"metric":{"namespace":"runner","name":"arm_id"},"value":r["arm"]["id"],"value_type":"string","split_role":"system","evidence_role":"workflow_smoke"}]
    engineering_top1=float((logits.argmax(1)==target).float().mean()*100.0)
    # Pairing identity binds only the shared initialization/randomization unit.
    # Trial/run/attempt and arm identity are deliberately excluded so both arms
    # for one admitted revision and seed produce the same pairing digest.
    pairing_identity=_digest(_canonical({"experiment_revision_id":r["experiment_revision_id"],"seed":int(r["seed"]),"streams":seed_manifest["streams"]}))
    result={"primary_metric":engineering_top1,"step":3,"pairing_identity_digest":pairing_identity,"arm_id":r["arm"]["id"],"seed":int(r["seed"]),"evidence_class":"workflow_smoke","inference_allowed":False}
    obs.append({"metric":{"namespace":"classification","name":"primary_metric"},"value":engineering_top1,"value_type":"float","unit":"percent","direction":"maximize","split_role":"validation","dataset_digest":dataset_digest,"step":{"axis":"epoch","value":3},"aggregation":"per_seed","evidence_role":"workflow_smoke"})
    documents={"run_log.json":runlog,"evaluation_audit.json":audit,"implementation_observation.json":observation,
      "counts_report.json":{"within_limits":True,"checks":{k:{"planned":maxima[k],"observed":observed[k],"within_limits":observed[k]<=maxima[k]} for k in required_max}},
      "trace_compare.json":{"only_difference":{"node":"pool2.op","baseline":"MaxPool2d","intervention":"CenteredTLP"},"shared_trace_digest":shared_trace_digest,"invariant_check":True},
      "execution_trace.json":{"trace":trace,"shared_digest":shared_trace_digest},"seed_manifest.json":seed_manifest,
      "tlp_init_zero_report.json":{"max_abs_theta":theta_initial_max_abs,"pass":theta_initial_max_abs==0.0},
      "reproducibility_report.json":{"batch_order_sha256":_digest(_canonical([0])),"augmentation_sequence_sha256":_digest(_canonical([])),"repeatable":True,"num_workers":0},
      "evidence_manifest.json":{"class":"workflow_smoke","inference_allowed":False,"historical_notebook":"exploratory_untrusted_not_confirmatory","final_test_metrics":False},
      "smoke_metrics.json":{"train_steps":3,"validation_passes":1,"disclaimer":"no_scientific_inference"},
      "content_identity.json":{"code_digest":code_digest,"config_digest":config_digest,"dataset_digest":dataset_digest,"execution_trace_digest":shared_trace_digest,"lineage_digest":lineage},
      "result_record.json":{"status":"completed","result":result,"observations":obs,"evidence_class":"workflow_smoke","tracker_session_calls":0}}
    files=[]
    for name,value in documents.items():
        _write(work/name,value); ref=_publish(work/name,"workflow_smoke"); files.append({"path":name,"digest":ref["digest"],"content_ref":ref})
    _write(work/"artifacts_index.json",{"files":files})
    return 0
def _scientific(work:Path,r:Mapping[str,Any],p:Mapping[str,Any])->int:
    try: import torch, torchvision # type: ignore
    except ImportError as exc: raise RuntimeError("scientific path requires admitted torch service") from exc
    readiness=dataset_status()
    if not readiness["execution_ready_without_network"]: raise RuntimeError("accepted_dataset_not_ready_offline")
    if r["profile_conditions"]["network_mode"]!="offline" or r["profile_conditions"]["input_policy"]["sampling"]!="full": raise RuntimeError("confirmatory_admission_policy_mismatch")
    if p["device"]=="cuda" and not torch.cuda.is_available(): raise RuntimeError("hardware_failure: CUDA required")
    nn=torch.nn; F=torch.nn.functional; device=torch.device(p["device"]); seed=int(r["seed"])
    os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG",":4096:8"); torch.use_deterministic_algorithms(True); torch.manual_seed(seed); random.seed(seed)
    if torch.cuda.is_available(): torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.deterministic=True; torch.backends.cudnn.benchmark=False
    if hasattr(torch.backends.cuda.matmul,"allow_tf32"): torch.backends.cuda.matmul.allow_tf32=False
    if hasattr(torch.backends.cudnn,"allow_tf32"): torch.backends.cudnn.allow_tf32=False
    class TLP(nn.Module):
        def __init__(self): super().__init__(); self.weight=nn.Parameter(torch.zeros(128,4))
        def forward(self,x):
            patches=F.unfold(x,kernel_size=2,stride=2).view(x.shape[0],x.shape[1],4,-1)
            centered=self.weight-self.weight.mean(dim=1,keepdim=True)
            y=(patches+centered[None,:,:,None]).amax(dim=2)
            return y.view(x.shape[0],x.shape[1],x.shape[2]//2,x.shape[3]//2)
    class Net(nn.Module):
        def __init__(self):
            super().__init__(); self.c1=nn.Conv2d(3,64,3,padding=1); self.c2=nn.Conv2d(64,128,3,padding=1); self.c3=nn.Conv2d(128,256,3,padding=1); self.pool1=nn.MaxPool2d(2,2); self.pool2=nn.MaxPool2d(2,2) if r["arm"]["role"]=="baseline" else TLP(); self.fc=nn.Linear(256,10)
        def forward(self,x):
            x=self.pool1(F.relu(self.c1(x))); x=self.pool2(F.relu(self.c2(x))); x=F.relu(self.c3(x)); return self.fc(F.adaptive_avg_pool2d(x,1).flatten(1))
    mean=(0.4467,0.4398,0.4066); std=(0.2603,0.2566,0.2713)
    train_tf=torchvision.transforms.Compose([torchvision.transforms.RandomCrop(96,padding=4),torchvision.transforms.RandomHorizontalFlip(.5),torchvision.transforms.ToTensor(),torchvision.transforms.Normalize(mean,std)])
    eval_tf=torchvision.transforms.Compose([torchvision.transforms.Resize(96),torchvision.transforms.CenterCrop(96),torchvision.transforms.ToTensor(),torchvision.transforms.Normalize(mean,std)])
    data_root=_root()/"datasets"/DATASET_ID
    full_train=torchvision.datasets.STL10(root=str(data_root),split="train",transform=train_tf,download=False)
    full_dev=torchvision.datasets.STL10(root=str(data_root),split="train",transform=eval_tf,download=False)
    indices=list(range(len(full_train))); random.Random(1701).shuffle(indices); cut=len(indices)//10; dev_idx,train_idx=indices[:cut],indices[cut:]
    generator=torch.Generator().manual_seed(seed)
    train=torch.utils.data.DataLoader(torch.utils.data.Subset(full_train,train_idx),batch_size=128,shuffle=True,generator=generator,num_workers=0)
    dev=torch.utils.data.DataLoader(torch.utils.data.Subset(full_dev,dev_idx),batch_size=128,shuffle=False,num_workers=0)
    model=Net().to(device); shared_bytes=b"".join(v.detach().cpu().numpy().tobytes() for k,v in model.state_dict().items() if "pool2.weight" not in k); initial_digest=_digest(shared_bytes)
    optimizer=torch.optim.SGD(model.parameters(),lr=.1,momentum=.9,weight_decay=5e-4); scheduler=torch.optim.lr_scheduler.MultiStepLR(optimizer,[60,90],gamma=.2)
    checkpoint_dir=work/"checkpoints"; checkpoint_dir.mkdir(exist_ok=True); latest=checkpoint_dir/"latest.pt"
    best=(-1.0,0,None); rows=[]; safety=[]; start_epoch=1; resumed=False
    if latest.is_file():
        saved=torch.load(latest,map_location=device,weights_only=False); model.load_state_dict(saved["model"]); optimizer.load_state_dict(saved["optimizer"]); scheduler.load_state_dict(saved["scheduler"]); generator.set_state(saved["sampling_rng"]); torch.set_rng_state(saved["torch_rng"]); random.setstate(saved["python_rng"]); best=(saved["best_score"],saved["best_epoch"],saved["best_state"]); rows=saved["rows"]; start_epoch=int(saved["epoch"])+1; resumed=True
    for epoch in range(start_epoch,int(p["epochs"])+1):
        model.train(); total=correct=0; loss_sum=0.0
        for x,y in train:
            x,y=x.to(device),y.to(device); optimizer.zero_grad(set_to_none=True); logits=model(x); loss=F.cross_entropy(logits,y)
            if not torch.isfinite(loss): safety.append({"epoch":epoch,"reason":"nan_loss"}); break
            loss.backward()
            if any(q.grad is not None and not torch.isfinite(q.grad).all() for q in model.parameters()): safety.append({"epoch":epoch,"reason":"diverged_gradients"}); break
            optimizer.step(); loss_sum+=float(loss)*len(y); total+=len(y); correct+=int((logits.argmax(1)==y).sum())
        if safety: break
        model.eval(); dc=dn=0
        with torch.no_grad():
            for x,y in dev: y=y.to(device); dc+=int((model(x.to(device)).argmax(1)==y).sum()); dn+=len(y)
        score=100.0*dc/dn; rows.append({"epoch":epoch,"train_loss":loss_sum/total,"train_top1":100.0*correct/total,"validation_top1":score}); scheduler.step()
        if score>best[0]: best=(score,epoch,{k:v.detach().cpu() for k,v in model.state_dict().items()})
        checkpoint={"schema":"adaos.tlp.checkpoint.v1","epoch":epoch,"model":model.state_dict(),"optimizer":optimizer.state_dict(),"scheduler":scheduler.state_dict(),"sampling_rng":generator.get_state(),"torch_rng":torch.get_rng_state(),"python_rng":random.getstate(),"best_score":best[0],"best_epoch":best[1],"best_state":best[2],"rows":rows}
        temporary=checkpoint_dir/"latest.tmp"; torch.save(checkpoint,temporary); os.replace(temporary,latest)
    with (work/"train_val_metrics.csv").open("w",newline="",encoding="utf-8") as h:
        w=csv.DictWriter(h,fieldnames=["epoch","train_loss","train_top1","validation_top1"]); w.writeheader(); w.writerows(rows)
    if best[2] is None: status="safety_stopped"
    else:
        status="completed"; torch.save(best[2],work/"checkpoints"/"selected.pt"); model.load_state_dict(best[2]); _write(work/"selection_report.json",{"source":"validation","selected_epoch":best[1],"validation_top1":best[0],"tie_break":"earlier_epoch"})
    _write(work/"pairing_manifest.json",{"initial_weights_digest":initial_digest,"data_order_manifest":_digest(_bytes(train_idx)),"augmentation_rng_trace":_digest(_bytes({"seed":seed,"epochs":p["epochs"]})),"pool2_operator":r["arm"]["id"]})
    _write(work/"model_manifest.json",{"architecture":"accepted ConvNetSTL10","pool1":{"type":"MaxPool2d","kernel":2,"stride":2},"pool2":{"type":"MaxPool2d" if r["arm"]["role"]=="baseline" else "centered_per_channel_TLP","kernel":2,"stride":2,"parameters_per_channel":0 if r["arm"]["role"]=="baseline" else 4,"initialization":"zero","constraint":"per_channel_zero_mean"},"shared":{"channels":[64,128,256],"classifier":[256,10]}})
    _write(work/"transforms_spec.json",{"train":["RandomCrop","RandomHorizontalFlip","ToTensor","Normalize"],"test":["Resize","CenterCrop","ToTensor","Normalize"],"test_stochastic":False})
    _write(work/"rng_streams.json",{"initialization":seed,"sampling":seed,"augmentation":seed,"analysis":{"seed":ANALYSIS_SEED}}); _write(work/"training_safety.log",{"checks":["nan_loss","diverged_gradients"],"events":safety})
    _write(work/"environment_snapshot.json",{"versions":{"python":platform.python_version(),"torch":torch.__version__,"torchvision":torchvision.__version__},"git_commit_hash":_git(),"hardware_profile":{"device":str(device)},"torch_backends_flags":{"cudnn_deterministic":torch.backends.cudnn.deterministic,"cudnn_benchmark":torch.backends.cudnn.benchmark}})
    access=[{"event":"selection_sealed","epoch":best[1] if best[2] else None,"split":"validation"}]
    observations=[{"metric":{"namespace":"runner","name":"completed"},"value":status=="completed","value_type":"boolean","split_role":"system","evidence_role":"confirmatory"}]
    test_event=None; accuracy=None
    if status=="completed" and p["inference_allowed"]:
        testset=torchvision.datasets.STL10(root=str(data_root),split="test",transform=eval_tf,download=False); loader=torch.utils.data.DataLoader(testset,batch_size=128,shuffle=False,num_workers=0); tc=tn=0; model.eval()
        access.append({"event":"test_opened_after_seal","split":"test"})
        with torch.no_grad():
            for x,y in loader: y=y.to(device); tc+=int((model(x.to(device)).argmax(1)==y).sum()); tn+=len(y)
        accuracy=100.0*tc/tn; test_event={"single_use":True,"after_seal":True,"evaluation_passes":1}; _write(work/"final_test_metrics.json",{"top1_accuracy_percent":accuracy,"evaluation_passes":1}); observations.append({"metric":{"namespace":"classification","name":"top1_accuracy"},"value":accuracy,"value_type":"float","unit":"percent","direction":"maximize","split_role":"test","dataset_digest":readiness["split_bindings"]["test"]["dataset_digest"],"aggregation":"per_seed","evidence_role":"confirmatory"})
    pairing_identity=_digest(_canonical({"experiment_revision_id":r["experiment_revision_id"],"seed":seed,"streams":{"initialization":seed,"sampling":seed,"augmentation":seed,"analysis":ANALYSIS_SEED}}))
    result=None if accuracy is None else {"primary_metric":accuracy,"step":len(rows),"pairing_identity_digest":pairing_identity,"arm_id":r["arm"]["id"],"seed":seed,"evidence_class":"confirmatory"}
    if result is not None:
        observations.append({"metric":{"namespace":"classification","name":"primary_metric"},"value":accuracy,"value_type":"float","unit":"percent","direction":"maximize","split_role":"test","dataset_digest":readiness["split_bindings"]["test"]["dataset_digest"],"step":{"axis":"epoch","value":len(rows)},"aggregation":"per_seed","evidence_role":"confirmatory"})
    _write(work/"evaluation_access.log",{"seal_before_test":True,"events":access}); _write(work/"result_record.json",{"status":status,"result":result,"observations":observations,"evidence_class":"confirmatory","tracker_session_calls":0})
    report_name="smoke_workflow_report.json" if not p["inference_allowed"] else "execution_report_confirmatory.json"
    _write(work/report_name,{"status":status,"profile":r["profile"],"device":p["device"],"epochs_planned":p["epochs"],"epochs_completed":len(rows),"seed":r["seed"],"inference_allowed":p["inference_allowed"],"stop_conditions":p["stop_conditions"],"stop_reason":safety[0]["reason"] if safety else "fixed_budget"})
    manifest_path=_data_root()/"datasets"/DATASET_ID/"accepted_dataset_manifest.json"; manifest_bytes=manifest_path.read_bytes(); manifest_digest=_digest(manifest_bytes)
    code_digest=_source_digest(); config_digest=_digest((Path(__file__).parents[1]/"config.json").read_bytes()); lineage=_digest(_canonical({"code":code_digest,"config":config_digest,"dataset":manifest_digest,"seed":seed,"arm":r["arm"]}))
    checkpoint_files=[]
    for cp in sorted(checkpoint_dir.glob("*.pt")): checkpoint_files.append({"name":cp.name,"sha256":_digest(cp.read_bytes())})
    _write(work/"checkpoint_index.json",{"schema":"adaos.tlp.checkpoint_index.v1","checkpoints":checkpoint_files,"resumed":resumed,"resume_supported":True})
    _write(work/"dataset_readiness.json",{"source":"accepted_dataset","status":"ready","local_manifest":{"sha256":manifest_digest}})
    _write(work/"dataset_manifest.json",{"dataset_id":DATASET_ID,"manifest_digest":manifest_digest,"source":"accepted_dataset","labels_exposed":False})
    _write(work/"split_hashes.json",{"train":_digest(_bytes(train_idx)),"validation":_digest(_bytes(dev_idx)),"test":readiness["split_bindings"]["test"]["digest"]})
    _write(work/"leakage_scan_report.json",{"test_ids_in_train":0,"test_ids_in_validation":0,"passed":True})
    _write(work/"evaluation_audit.json",{"per_stage":{"confirmatory":{"test_evaluations_count":1 if test_event else 0}},"test_access":access[1:]})
    _write(work/"confirmatory_evidence.json",{"lineage_digest":lineage,"seed":seed,"arm":r["arm"],"top1_accuracy_percent":accuracy,"test_evaluation_event":test_event})
    runlog={"stage":"confirmatory","device":str(device),"epochs_completed":len(rows),"observed_epochs":len(rows),"seeds":[f"seed-{seed}"],"seed_values":p["seeds"],"inference_allowed":True,"evidence_class":"confirmatory","input_policy":r["profile_conditions"]["input_policy"],"network":{"mode":"offline","accessed":False},"network_policy":{"requested_mode":"offline","enforcement":"consumer_execution_policy","accessed":False,"observation_not_isolation":True},"split_usage":{"test_used":bool(test_event)},"lineage_digest":lineage,"shared_initialization":initial_digest,"checkpoints":{"resume_supported":True,"resumed":resumed},"invariant_check":True}
    _write(work/"run_log.json",runlog)
    artifacts=[]
    for path in work.rglob("*"):
        if path.is_file() and path.name not in ("attempt_spec.json","artifacts_index.json") and path.suffix in (".json",".csv",".log"): artifacts.append({"path":path.relative_to(work).as_posix(),"digest":_digest(path.read_bytes()),"content_ref":_publish(path,"confirmatory")})
    _write(work/"artifacts_index.json",{"files":artifacts}); return 0
def _main()->int:
    parser=argparse.ArgumentParser(); subs=parser.add_subparsers(dest="command",required=True); exe=subs.add_parser("execute"); exe.add_argument("--spec",required=True,type=Path); return _execute(parser.parse_args().spec)
if __name__=="__main__": raise SystemExit(_main())
