"""Append-only runner for the frozen verification-budget ablation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .canonical import canonical_json, canonical_sha256
from .compact_protocol import compact_public_task, compact_schema_for_task, decode_compact_decision, parse_compact_response
from .compact_runner import FORBIDDEN_REQUEST_TERMS
from .runner import DEFAULT_BASE_URL, OpenRouterTransport, response_payload_sha256, utc_now
from .schema import response_format

PROTOCOL = "coordcap-1.3.0-verification-budget"
MODELS = ("google/gemini-3.1-flash-lite", "openai/gpt-5.4-mini")
COST_LIMIT = 3.0
CHECKLIST = (
    "hard constraints", "resource conflicts", "omitted principals",
    "incompatible assignments", "unresolved conflicts", "abstention necessity",
)
AUDIT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["suspected_constraint_ids", "suspected_resource_conflicts", "suspected_omissions", "revision_needed"],
    "properties": {
        "suspected_constraint_ids": {"type": "array", "items": {"type": "string"}},
        "suspected_resource_conflicts": {"type": "array", "items": {"type": "string"}},
        "suspected_omissions": {"type": "array", "items": {"type": "string"}},
        "revision_needed": {"type": "boolean"},
    },
}


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object: {path}")
    return value


def write_exclusive(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(dict(value), handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")


def audit_append(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(value), ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")


def content(response: Mapping[str, Any]) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping): return ""
    message = choices[0].get("message")
    return str(message.get("content", "")) if isinstance(message, Mapping) else ""


def finish(response: Mapping[str, Any]) -> str:
    choices = response.get("choices")
    return str(choices[0].get("finish_reason", "")) if isinstance(choices, list) and choices and isinstance(choices[0], Mapping) else ""


class AblationRunner:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.subset = load(root / "results/ablation_subset_manifest.json")
        self.raw_root = root / "outputs/verification_ablation/raw"
        self.parsed_root = root / "outputs/verification_ablation/parsed"
        self.cache_root = root / "outputs/verification_ablation/cache"
        self.audit_path = root / "audits/verification_ablation_call_audit.jsonl"
        self.session_path = root / "audits/verification_ablation_session.json"
        self.progress_path = root / "results/verification_ablation_progress.json"
        self.semaphore = asyncio.Semaphore(8)
        self.lock = asyncio.Lock()
        key = os.getenv("OPENROUTER_API_KEY")
        if not key: raise RuntimeError("OPENROUTER_API_KEY is not set")
        self.transport = OpenRouterTransport(api_key=key, base_url=DEFAULT_BASE_URL)
        self.session = self._session()

    def _session(self) -> dict[str, Any]:
        run_id = "coordcap4a_" + canonical_sha256({"protocol": PROTOCOL, "subset": self.subset["subset_sha256"]})[:24]
        if self.session_path.exists():
            value = load(self.session_path)
            if value["ablation_run_id"] != run_id: raise ValueError("ablation session mismatch")
            return value
        value = {
            "schema_version": "coordcap-verification-ablation-session-1.0",
            "protocol_version": PROTOCOL,
            "ablation_run_id": run_id,
            "started_at_utc": utc_now(),
            "subset_sha256": self.subset["subset_sha256"],
            "expected_condition_records": 144,
            "expected_new_primary_calls": 144,
            "new_cost_hard_limit_usd": COST_LIMIT,
        }
        write_exclusive(self.session_path, value)
        return value

    def b0(self, instance_id: str, model: str) -> dict[str, Any]:
        matches=[]
        for path in (self.root / "outputs/formal_recovery/canonical/parsed").glob("*.json"):
            d=load(path)
            if d.get("instance_id")==instance_id and d.get("model")==model and d.get("method")=="direct_joint_prompt": matches.append(d)
        if len(matches)!=1: raise ValueError(f"expected one B0: {instance_id}/{model}")
        d=matches[0]
        if not d.get("effective_json_valid") or d.get("status")!="complete": raise ValueError("B0 not valid")
        return d

    def rid(self, instance_id: str, model: str, budget: str) -> str:
        return "coordcap4a_" + canonical_sha256({"run":self.session["ablation_run_id"],"instance":instance_id,"model":model,"budget":budget})[:24]

    async def _call(self, *, run_id: str, call_index: int, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
        text=canonical_json(payload).lower()
        leaked=[term for term in FORBIDDEN_REQUEST_TERMS if term in text]
        if leaked: raise RuntimeError(f"ablation request leakage: {leaked}")
        request_hash=canonical_sha256({"base_url":DEFAULT_BASE_URL,"payload":payload})
        cache=self.cache_root/f"{request_hash}.json"
        records=[]
        for transport_index in range(4):
            path=self.raw_root/run_id/f"call_{call_index:02d}_{stage}_transport_{transport_index:02d}.json"
            if path.exists():
                record=load(path); records.append(record)
                if record["transport_success"] or not record["retryable"]: break
                continue
            delay=0.0
            if transport_index:
                delay=0.5*2**(transport_index-1)+(int.from_bytes(hashlib.sha256(f"{run_id}|{call_index}|{transport_index}".encode()).digest()[:2],"big")/65535)*0.25
                await asyncio.sleep(delay)
            if not records and cache.exists():
                response=load(cache)["response_payload"]; source="cache"; latency=0.0
            else:
                started=time.perf_counter()
                async with self.semaphore: response=dict(await self.transport.request(payload=payload,timeout_seconds=180.0))
                latency=time.perf_counter()-started; source="network"
            choices=response.get("choices")
            success=response.get("_coordcap_transport_success") is not False and isinstance(choices,list) and bool(choices)
            usage=response.get("usage") if isinstance(response.get("usage"),Mapping) else {}
            record={
                "schema_version":"coordcap-verification-transport-attempt-1.0","timestamp_utc":utc_now(),"protocol_version":PROTOCOL,
                "ablation_run_id":self.session["ablation_run_id"],"run_id":run_id,"call_index":call_index,"stage":stage,"transport_index":transport_index,
                "request_hash":request_hash,"request_payload":payload,"response_payload":response,"response_payload_sha256":response_payload_sha256(response),
                "source":source,"transport_success":success,"retryable":bool(response.get("retryable",False)),"status_code":response.get("_coordcap_status_code",response.get("status_code")),
                "error":None if success else str(response.get("error","missing choices")),"finish_reason":finish(response),"truncated":finish(response)=="length",
                "returned_model":str(response.get("model","")),"returned_provider":str(response.get("provider","")),"route_consistent":success and str(response.get("model",""))==str(payload["model"]),
                "max_output_tokens":payload["max_tokens"],"raw_content":content(response),"response_content_chars":len(content(response)),"usage":dict(usage),
                "reported_cost":float(usage["cost"]) if isinstance(usage.get("cost"),(int,float)) else None,"latency_seconds":latency,"backoff_seconds":delay,
            }
            write_exclusive(path,record); records.append(record)
            async with self.lock:
                audit_append(self.audit_path,{"event":"transport_attempt","timestamp_utc":record["timestamp_utc"],"run_id":run_id,"call_index":call_index,"stage":stage,"transport_index":transport_index,"source":source,"transport_success":success,"status_code":record["status_code"],"finish_reason":record["finish_reason"],"truncated":record["truncated"],"reported_cost":record["reported_cost"],"latency_seconds":latency,"raw_path":str(path)})
            if success:
                if not cache.exists(): write_exclusive(cache,{"request_hash":request_hash,"response_payload":response,"response_payload_sha256":response_payload_sha256(response)})
                break
            if not record["retryable"]: break
        final=records[-1]
        return {"transport_success":final["transport_success"],"route_consistent":final["route_consistent"],"error":final["error"],"finish_reason":final["finish_reason"],"truncated":final["truncated"],"raw_content":final["raw_content"],"usage":final["usage"],"reported_cost":final["reported_cost"],"latency_seconds":sum(float(r["latency_seconds"]) for r in records),"transport_attempt_count":len(records),"raw_paths":[str(self.raw_root/run_id/f"call_{call_index:02d}_{stage}_transport_{r['transport_index']:02d}.json") for r in records]}

    def _revision_payload(self, *, model:str, task:dict, b0:dict, audit:dict|None) -> dict[str,Any]:
        user={"task":compact_public_task(task),"original_decision":b0,"generic_checklist":list(CHECKLIST)}
        if audit is not None: user["blind_audit"]=audit
        payload={"model":model,"messages":[{"role":"system","content":"Blindly verify a multi-principal catalogue decision. Use only the public task, original decision, generic checklist, and any blind audit supplied. Return exactly one compact JSON decision and nothing else; no explanation or Markdown."},{"role":"user","content":canonical_json(user)}],"max_tokens":448,"response_format":response_format(compact_schema_for_task(task),name="coordcap_verified_decision"),"provider":{"require_parameters":True},"seed":0}
        if not model.startswith("openai/"): payload["temperature"]=0.0
        return payload

    async def execute(self, instance:dict[str,Any], model:str, budget:str) -> dict[str,Any]:
        run_id=self.rid(instance["instance_id"],model,budget); path=self.parsed_root/f"{run_id}.json"
        if path.exists(): return load(path)
        task=instance["public_task"]; b0_terminal=self.b0(instance["instance_id"],model); b0=dict(b0_terminal["compact_output"])
        calls=[]; blind_audit=None; audit_valid=None
        if budget=="B2_AUDIT_AND_REVISION":
            user={"task":compact_public_task(task),"original_decision":b0}
            payload={"model":model,"messages":[{"role":"system","content":"Blindly audit the supplied decision using only the public task. Return only the requested structured audit. Do not solve from feedback because none is provided."},{"role":"user","content":canonical_json(user)}],"max_tokens":192,"response_format":response_format(AUDIT_SCHEMA,name="coordcap_blind_audit"),"provider":{"require_parameters":True},"seed":0}
            if not model.startswith("openai/"): payload["temperature"]=0.0
            call=await self._call(run_id=run_id,call_index=0,stage="blind_audit",payload=payload); calls.append(call)
            try: blind_audit=json.loads(call["raw_content"]); audit_valid=isinstance(blind_audit,dict) and set(blind_audit)==set(AUDIT_SCHEMA["required"])
            except Exception: blind_audit=None; audit_valid=False
        revision=await self._call(run_id=run_id,call_index=len(calls),stage="revision",payload=self._revision_payload(model=model,task=task,b0=b0,audit=blind_audit)); calls.append(revision)
        value,valid,errors=parse_compact_response(revision["raw_content"],task) if revision["transport_success"] else (None,False,[str(revision["error"])])
        route_ok=all(c["route_consistent"] for c in calls)
        terminal={"schema_version":"coordcap-verification-terminal-1.0","created_at_utc":utc_now(),"protocol_version":PROTOCOL,"ablation_run_id":self.session["ablation_run_id"],"run_id":run_id,"instance_id":instance["instance_id"],"model":model,"budget_condition":budget,"subset_sha256":self.subset["subset_sha256"],"b0_formal_run_id":b0_terminal["run_id"],"b0_compact_output":b0,"blind_audit":blind_audit,"blind_audit_valid":audit_valid,"compact_output":value,"decoded_decision":decode_compact_decision(value,task) if valid else None,"effective_json_valid":valid,"validation_errors":errors,"truncated_output":any(c["truncated"] for c in calls),"terminal_transport_failure":any(not c["transport_success"] for c in calls),"route_consistent":route_ok,"status":"complete" if valid and all(c["transport_success"] for c in calls) and route_ok else "invalid_or_transport_failure","semantic_scorable":valid and all(c["transport_success"] for c in calls) and route_ok,"semantic_call_count":len(calls),"network_attempt_count":sum(c["transport_attempt_count"] for c in calls),"transport_retry_count":sum(c["transport_attempt_count"]-1 for c in calls),"reported_cost":sum(float(c["reported_cost"] or 0) for c in calls),"latency_seconds":sum(float(c["latency_seconds"]) for c in calls),"total_prompt_tokens":sum(int(c["usage"].get("prompt_tokens",0)) for c in calls),"total_completion_tokens":sum(int(c["usage"].get("completion_tokens",0)) for c in calls),"calls":[{k:v for k,v in c.items() if k!="raw_content"} for c in calls],"raw_paths":[p for c in calls for p in c["raw_paths"]]}
        write_exclusive(path,terminal)
        async with self.lock: audit_append(self.audit_path,{"event":"terminal_record","timestamp_utc":utc_now(),"run_id":run_id,"instance_id":instance["instance_id"],"model":model,"budget_condition":budget,"status":terminal["status"],"semantic_call_count":len(calls),"reported_cost":terminal["reported_cost"],"parsed_path":str(path)})
        return terminal

    async def run(self) -> dict[str,Any]:
        specs=[(i,m,b) for i in self.subset["instances"] for m in MODELS for b in ("B1_SELF_REVISION","B2_AUDIT_AND_REVISION")]
        terminals=[]; safe=None
        for offset in range(0,len(specs),8):
            chunk=specs[offset:offset+8]
            cost=sum(float(t["reported_cost"]) for t in terminals)
            pending_calls=sum((1 if b.startswith("B1") else 2) for i,m,b in chunk if not (self.parsed_root/f"{self.rid(i['instance_id'],m,b)}.json").exists())
            if cost+pending_calls*0.04>COST_LIMIT: safe="cost_reservation"; break
            got=await asyncio.gather(*(self.execute(*s) for s in chunk)); terminals.extend(got)
            cost=sum(float(t["reported_cost"]) for t in terminals)
            if cost>=COST_LIMIT: safe="cost_limit"; break
            if any(any(load(Path(p)).get("status_code")==402 for p in t["raw_paths"]) for t in got): safe="http402"; break
            progress={"schema_version":"coordcap-verification-progress-1.0","timestamp_utc":utc_now(),"ablation_run_id":self.session["ablation_run_id"],"terminal_records":len(terminals),"expected_new_condition_records":96,"primary_calls":sum(t["semantic_call_count"] for t in terminals),"network_attempts":sum(t["network_attempt_count"] for t in terminals),"reported_cost_usd":cost,"effective_json_validity_rate":sum(t["effective_json_valid"] for t in terminals)/len(terminals),"safe_stop_reason":safe}
            self.progress_path.write_text(json.dumps(progress,indent=2,sort_keys=True)+"\n")
            if len(terminals)%24==0: print(json.dumps({"ablation_progress":progress},sort_keys=True),flush=True)
            if safe: break
        return {"protocol_version":PROTOCOL,"ablation_run_id":self.session["ablation_run_id"],"condition_terminals":len(terminals),"primary_calls":sum(t["semantic_call_count"] for t in terminals),"network_attempts":sum(t["network_attempt_count"] for t in terminals),"reported_cost_usd":sum(float(t["reported_cost"]) for t in terminals),"valid":sum(t["effective_json_valid"] for t in terminals),"safe_stop_reason":safe}
