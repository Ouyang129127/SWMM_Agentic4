"""Frozen, resumable simulation-to-diagnosis pipeline with bounded API concurrency."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import uuid4

SPEC = importlib.util.spec_from_file_location("pipeline_stage_helpers", Path(__file__).with_name("batch_exp15.py"))
base = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(base)
PROJECT = Path(__file__).resolve().parents[1]
TERMINAL = {"completed", "partial", "failed", "blocked_by_simulation"}


def process_tree(pid):
    """Include Windows venv redirector children in telemetry and timeouts."""
    if os.name != "nt":
        return [pid]
    import ctypes
    from ctypes import wintypes
    class Entry(ctypes.Structure):
        _fields_ = [("size",wintypes.DWORD),("usage",wintypes.DWORD),("pid",wintypes.DWORD),
                    ("heap",ctypes.c_size_t),("module",wintypes.DWORD),("threads",wintypes.DWORD),
                    ("parent",wintypes.DWORD),("priority",wintypes.LONG),("flags",wintypes.DWORD),
                    ("name",wintypes.WCHAR*260)]
    kernel=ctypes.WinDLL("kernel32",use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes=[wintypes.DWORD,wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype=wintypes.HANDLE
    kernel.Process32FirstW.argtypes=[wintypes.HANDLE,ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes=[wintypes.HANDLE,ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.CreateToolhelp32Snapshot(2,0)
    if handle==ctypes.c_void_p(-1).value:
        return [pid]
    edges=[]
    try:
        entry=Entry()
        entry.size=ctypes.sizeof(entry)
        valid=kernel.Process32FirstW(handle,ctypes.byref(entry))
        while valid:
            edges.append((entry.pid,entry.parent))
            valid=kernel.Process32NextW(handle,ctypes.byref(entry))
    finally:
        kernel.CloseHandle(handle)
    result=[pid]
    seen={pid}
    for parent in result:
        for child,owner in edges:
            if owner==parent and child not in seen:
                result.append(child)
                seen.add(child)
    return result


def memory_snapshot(pid=None):
    """Windows resource telemetry; returns no user or machine identifiers."""
    if os.name != "nt":
        return {}
    import ctypes
    from ctypes import wintypes
    class Memory(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [(n, ctypes.c_ulonglong) for n in
                    ("total", "available", "page_total", "page_available", "virtual_total", "virtual_available", "extended")]
    memory = Memory()
    memory.length = ctypes.sizeof(memory)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
        return {}
    result = {"available_memory_mb": round(memory.available/2**20, 1), "memory_load_pct": memory.load}
    if pid is not None:
        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [(n, ctypes.c_size_t) for n in
                        ("peak_rss", "rss", "peak_paged", "paged", "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile", "private")]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        rss,private=0,0
        measured=[]
        for process_id in process_tree(pid):
            handle = kernel.OpenProcess(0x1000 | 0x0010, False, process_id)
            if not handle:
                continue
            try:
                counters = Counters()
                counters.cb = ctypes.sizeof(counters)
                psapi = ctypes.WinDLL("psapi")
                psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
                if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), ctypes.sizeof(counters)):
                    rss+=counters.rss
                    private+=counters.private
                    measured.append(process_id)
            finally:
                kernel.CloseHandle(handle)
        if measured:
            result.update(worker_rss_mb=round(rss/2**20,1),worker_private_mb=round(private/2**20,1),
                          worker_process_tree=measured,memory_scope="launcher_and_descendants")
    return result


class RequestSlots:
    """Exclusive per-request leases shared by all processes of this batch."""
    def __init__(self, directory, limit, alive=base.process_alive):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.limit, self.alive = limit, alive

    def claim(self):
        for slot in range(self.limit):
            path = self.directory/f"slot_{slot:02d}.json"
            lease = {"pid": os.getpid(), "nonce": uuid4().hex, "created_at": base.stamp()}
            try:
                with path.open("x", encoding="utf-8") as stream:
                    json.dump(lease, stream)
                return path, lease
            except FileExistsError:
                try:
                    old_bytes = path.read_bytes()
                    old = json.loads(old_bytes)
                    if not self.alive(old["pid"]) and path.read_bytes() == old_bytes:
                        path.unlink(missing_ok=True)
                except (OSError, ValueError, KeyError):
                    pass
        return None

    @staticmethod
    def release(token):
        path, lease = token
        if path.exists() and base.read(path) == lease:
            path.unlink()

    async def acquire(self):
        started = time.monotonic()
        while True:
            token = self.claim()
            if token is not None:
                return token, time.monotonic()-started
            await asyncio.sleep(0.2)


def install_request_gate(batch, plan):
    """Wrap only network scheduling, leaving prompts and validators unchanged."""
    from workflow_agents.diagnosis import service
    real_completion = service.configured_completion
    slots = RequestSlots(batch/"runtime/llm_slots", plan["llm_concurrency"])
    async def bounded_completion(prompt, audit=None):
        serialized = service.diagnosis_system_prompt(prompt) + service.serialize_prompt(prompt)
        preflight = {"request_utf8_bytes": len(serialized.encode("utf-8")), "request_characters": len(serialized),
                     "request_body_sha256": __import__("hashlib").sha256(serialized.encode("utf-8")).hexdigest(),
                     "context_preflight_policy": "measure only; never truncate, silently rescope, or reject by approximate tokenizer"}
        try:
            import tiktoken
            preflight["approximate_prompt_tokens"] = len(tiktoken.get_encoding("cl100k_base").encode(serialized, disallowed_special=()))
            preflight["approximate_tokenizer"] = "cl100k_base; not the provider tokenizer"
        except Exception as exc:
            preflight["token_estimate_unavailable"] = type(exc).__name__
        del serialized
        if audit:
            audit(phase="waiting_llm_slot", network_call_started=False, **preflight)
        token, queue_seconds = await slots.acquire()
        started = time.monotonic()
        try:
            if audit:
                audit(phase="completion", network_call_started=True, llm_queue_seconds=round(queue_seconds, 3),
                      api_started_at=base.stamp(), request_slot=token[0].name)
            return await asyncio.wait_for(real_completion(prompt, audit=audit), timeout=plan["request_timeout_seconds"])
        finally:
            slots.release(token)
            if audit:
                audit(api_elapsed_seconds=round(time.monotonic()-started, 3), api_finished_at=base.stamp())
    service.configured_completion = bounded_completion


def observation_end(model, manifest):
    """Keep a manifest's explicit model date; otherwise derive from its input grid."""
    explicit = manifest.get("recommended_simulation_end")
    if explicit:
        value = datetime.fromisoformat(explicit)
        if value.tzinfo is not None:
            raise ValueError("Observation end uses local simulation timestamps")
        return value.isoformat(sep=" ")
    sys.path.insert(0, str(PROJECT))
    from workflow_agents.common.model_files import _parse_datetime_value_file
    timing = base.read(model / "simulation_timing.json")
    endings = [_parse_datetime_value_file(model / event["relative_to_model"])[-1][0]
               for event in manifest["events"]]
    return (max(endings) + timedelta(hours=float(timing["post_rainfall_hours"]))).isoformat(sep=" ")


def initialize(batch, model_name, manifest_relative, llm_concurrency=10, diagnosis_workers=15, simulations_first=False):
    if batch.exists():
        raise FileExistsError(f"Batch already exists; use --run to resume: {batch}")
    if not 1 <= llm_concurrency <= diagnosis_workers <= 15:
        raise ValueError("Require 1 <= LLM concurrency <= diagnosis workers <= 15")
    model = PROJECT/"models"/model_name
    manifest_path = model/manifest_relative
    manifest = base.read(manifest_path)
    assert manifest["event_count"] == len(manifest["events"]) == 15
    fixed_end = observation_end(model, manifest)
    for event in manifest["events"]:
        path = (model/event["relative_to_model"]).resolve()
        path.relative_to((model/"events").resolve())
        assert base.sha(path) == event["sha256"]
    sys.path.insert(0, str(PROJECT))
    from workflow_agents.common.environment import load_dotenv
    load_dotenv(PROJECT/".env")
    if not os.getenv("DEEPSEEK_API_KEY"):
        raise RuntimeError("No configured diagnosis API key")
    batch.mkdir(parents=True)
    paths = list((PROJECT/"workflow_agents").rglob("*.py"))
    paths += [p for p in (PROJECT/"workflow_agents/reporting/prompts").iterdir() if p.is_file()]
    paths += [Path(__file__).resolve(), Path(__file__).with_name("batch_exp15.py"), PROJECT/"requirements.txt"]
    code_hashes = {}
    for source in sorted(set(paths)):
        relative = source.relative_to(PROJECT)
        destination = batch/"code"/relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        code_hashes[str(relative).replace("\\", "/")] = base.sha(destination)
    inputs = list((model/"static").rglob("*")) + list((model/"mapping").rglob("*"))
    inputs += list((model/"boundaries").glob("*.csv"))
    inputs += [model/"swmm/scenarios/baseline/model.inp", model/"model.yaml", model/"simulation_timing.json", manifest_path]
    inputs += [model/e["relative_to_model"] for e in manifest["events"]]
    input_hashes = {}
    for source in inputs:
        if source.is_file():
            destination = batch/"inputs"/source.relative_to(model)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            input_hashes[str(source.resolve())] = base.sha(destination)
    tag = datetime.now(timezone(timedelta(hours=8))).strftime("%m%d_%H%M%S")
    cases = []
    prefix = {"urban_drainage":"ud15pipe", "pystorms_beta":"pb15pipe"}.get(model_name, model_name+"_15pipe")
    for event in manifest["events"]:
        case_id = event.get("id") or Path(event["event_file"]).stem.split("_",1)[0]
        run_id = f"{prefix}_{tag}_{case_id}"
        if (model/"runs"/run_id).exists():
            raise FileExistsError(run_id)
        cases.append({"case_id": case_id, "event_file": event["event_file"], "rainfall_file": event["relative_to_model"],
                      "event_sha256":event["sha256"], "planned_total_mm":event["total_mm"], "run_id":run_id,
                      "simulation":"pending", "diagnosis":"pending"})
    if len({case["case_id"] for case in cases}) != len(cases):
        raise ValueError("Batch case IDs must be unique")
    plan = {"batch_id":batch.name, "created_at":base.stamp(), "project":str(PROJECT), "model_name":model_name,
            "scenario_name":"baseline", "manifest_relative":manifest_relative, "simulation_end_time":fixed_end,
            "simulation_workers":1, "diagnosis_workers":diagnosis_workers, "llm_concurrency":llm_concurrency,
            "simulations_first":simulations_first,
            "request_timeout_seconds":600, "simulation_timeout_seconds":1800, "diagnosis_timeout_seconds":3600,
            "max_supplement_rounds":2, "max_diagnosis_attempts_per_case":9, "min_available_memory_mb":4096,
            "failure_policy":"record and continue; failed cases not automatically restarted; one existing format/validation correction",
            "context_preflight_policy":"approximate count recorded only; actual provider context rejection is terminal",
            "report_policy":"verified structured JSON and deterministic Markdown at every delivered revision; HTML deferred",
            "study_role":"development_batch_not_independent_method_comparison", "whole_run_scope":True,
            "llm_configuration":{"model":os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
                                 "provider":os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                                 "temperature":0, "reasoning_effort":"high"},
            "code_sha256":code_hashes, "input_sha256":input_hashes, "cases":cases}
    base.save(batch/"plan.json", plan)
    base.save(batch/"status.json", {"phase":"prepared", "updated_at":base.stamp(), "cases":cases})
    versions = subprocess.run([sys.executable, "-m", "pip", "freeze"], capture_output=True, text=True)
    (batch/"python_packages.txt").write_text(versions.stdout, encoding="utf-8")
    (batch/"README.md").write_text(
        f"# {model_name}：15场模拟—诊断流水线\n\n"
        f"使用已生成的15场原始输入，为每场创建新的运行及诊断任务。模拟/证据1路；诊断任务上限{diagnosis_workers}；同时LLM请求上限{llm_concurrency}。"
        + ("全部模拟阶段结束后启动诊断。" if simulations_first else "某场证据完成后立即诊断，其余模拟继续。")
        + f"每场内部按初诊、引用核查、补证、修订和Markdown报告顺序执行。统一观察截止{fixed_end}。\n\n"
        "每场最多两轮补证，保留首次输出、全部修订、原始响应、失败及费用审计。请求超时600秒、单场诊断上限3600秒；超时终止对应执行进程，保留已有结果。"
        "上下文估算只记录，不改写或压缩输入；真实超限及其他失败按原原因保存，未自动缩小全场任务范围。\n\n"
        f"输入与代码冻结于inputs/和code/，不包含密钥。实际结果位于模型runs/{prefix}_*；cases/保存阶段结果和检查点；tables/保存逐运行、事件、调用、机制和陈述记录。"
        "PROGRESS.md为阅读入口，status.json为调度状态。支持接管同批仍存活的执行进程，避免恢复时重复调用。\n\n"
        "本批执行既定工作流并收集材料，尚不构成V0—V3正式方法对照。HTML展示报告不占用本轮诊断流程。\n\n"
        f"恢复命令：`{PROJECT}/.venv/Scripts/python.exe {batch}/code/scripts/batch_pipeline.py --run --batch {batch}`。"
        "失败任务保持失败；如需重试，应另行明确选择失败场次。\n", encoding="utf-8")
    base.export_tables(batch, base.read(batch/"status.json"))
    print(json.dumps({"batch":str(batch), "cases":15, "simulation_workers":1,
                      "diagnosis_workers":diagnosis_workers, "llm_concurrency":llm_concurrency}, ensure_ascii=False))


def run_worker(batch, index, stage):
    plan = base.configure_worker(batch)
    os.environ["OPENAI_REQUEST_TIMEOUT_SECONDS"] = str(plan["request_timeout_seconds"])
    if stage == "diagnosis":
        install_request_gate(batch, plan)
    return base.worker(batch, index, stage)


def eligible(cases, active_simulations, active_diagnoses, diagnosis_limit, memory_ok=True, simulations_first=False):
    simulations = [] if active_simulations else [i for i,c in enumerate(cases) if c["simulation"] == "pending"][:1]
    diagnoses = [i for i,c in enumerate(cases) if c["simulation"] == "completed" and c["diagnosis"] == "pending"]
    if simulations_first and any(c["simulation"] not in TERMINAL for c in cases):
        diagnoses = []
    return simulations, diagnoses[:max(0, diagnosis_limit-active_diagnoses)] if memory_ok else []


def kill_owned_worker(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        for process_id in reversed(process_tree(pid)):
            if not base.process_alive(process_id):
                continue
            handle=kernel.OpenProcess(0x0001,False,process_id)
            if not handle:
                if not base.process_alive(process_id):
                    continue
                raise RuntimeError(f"Cannot stop timed-out worker process {process_id}")
            try:
                if not kernel.TerminateProcess(handle,124) and base.process_alive(process_id):
                    raise RuntimeError(f"Cannot terminate timed-out worker process {process_id}")
            finally:
                kernel.CloseHandle(handle)
    else:
        import signal
        os.kill(pid, signal.SIGTERM)


def controller(batch):
    plan = base.read(batch/"plan.json")
    state = base.read(batch/"status.json")
    lock = batch/"pipeline_controller.lock"
    try:
        with lock.open("x") as stream:
            stream.write(str(os.getpid()))
    except FileExistsError:
        old = int(lock.read_text())
        if base.process_alive(old):
            raise RuntimeError(f"Controller already alive: {old}")
        lock.unlink()
        with lock.open("x") as stream:
            stream.write(str(os.getpid()))
    active = {}
    history = batch/"scheduler_events.jsonl"
    peaks = state.get("resource_peaks", {"diagnoses":0,"llm_requests":0,"worker_rss_mb":0})
    def event(action, **items):
        record = dict(at=base.stamp(), action=action, **items)
        with history.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False)+"\n")
        print(json.dumps(record, ensure_ascii=False), flush=True)

    def launch(index, stage):
        case, folder = state["cases"][index], batch/"cases"/f"{index+1:02d}"
        folder.mkdir(parents=True, exist_ok=True)
        log_path = folder/f"{stage}_{datetime.now(timezone.utc):%H%M%S}_{uuid4().hex[:6]}.log"
        log = log_path.open("w", encoding="utf-8")
        command = [sys.executable,"-u",str(batch/"code/scripts/batch_pipeline.py"),"--batch",str(batch),"--worker",stage,"--case",str(index)]
        try:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except Exception:
            log.close()
            raise
        started = base.stamp()
        case.update({stage:"running", f"{stage}_pid":process.pid, f"{stage}_started_at":started})
        active[(index,stage)] = {"process":process,"pid":process.pid,"log":log,"started_at":started}
        base.save(folder/f"{stage}_process.json", {"pid":process.pid,"started_at":started,"command":command})
        event("start", case=case["case_id"], stage=stage, pid=process.pid)
        base.save(batch/"status.json",state)

    def publish(export=False):
        telemetry = memory_snapshot()
        for item in active.values():
            item["memory"] = memory_snapshot(item["pid"])
        slot_dir = batch/"runtime/llm_slots"
        occupied = 0
        for path in slot_dir.glob("slot_*.json"):
            try:
                if base.process_alive(base.read(path)["pid"]):
                    occupied += 1
                else:
                    path.unlink(missing_ok=True)
            except (OSError, ValueError, KeyError):
                pass
        n_diag = sum(s=="diagnosis" for _,s in active)
        peaks["diagnoses"] = max(peaks["diagnoses"],n_diag)
        peaks["llm_requests"] = max(peaks["llm_requests"],occupied)
        peaks["worker_rss_mb"] = max([peaks["worker_rss_mb"]]+[a.get("memory",{}).get("worker_rss_mb",0) for a in active.values()])
        state.update(phase="pipeline_running",updated_at=base.stamp(),resource_peaks=peaks,
                     current_event=", ".join(state["cases"][i]["case_id"]+":"+s for i,s in active),
                     counts={"simulation":dict(Counter(c["simulation"] for c in state["cases"])),
                             "diagnosis":dict(Counter(c["diagnosis"] for c in state["cases"]))},
                     resources={**telemetry,"llm_requests_active":occupied,"diagnosis_tasks_active":n_diag},
                     active_workers=[{"case":state["cases"][i]["case_id"],"stage":s,"pid":a["pid"],**a.get("memory",{})} for (i,s),a in active.items()])
        base.save(batch/"status.json",state)
        if export:
            try:
                base.export_tables(batch,state)
            except (OSError, ValueError) as exc:
                event("export_deferred",error=str(exc))

    try:
        for i,case in enumerate(state["cases"]):
            for stage in ("simulation","diagnosis"):
                if case[stage] != "running":
                    continue
                pid = case.get(f"{stage}_pid")
                if pid and base.process_alive(pid):
                    active[(i,stage)] = {"process":None,"pid":pid,"log":None,"started_at":case[f"{stage}_started_at"]}
                    event("adopt",case=case["case_id"],stage=stage,pid=pid)
                else:
                    result = base.read(batch/"cases"/f"{i+1:02d}"/f"{stage}_result.json",{})
                    case[stage] = result.get("status","failed")
                    event("recover_exited",case=case["case_id"],stage=stage,status=case[stage])
        publish(export=True)
        last_export, last_resources = time.monotonic(),time.monotonic()
        while True:
            changed = False
            for key,item in list(active.items()):
                i,stage = key
                process = item["process"]
                alive = process.poll() is None if process is not None else base.process_alive(item["pid"])
                elapsed = (datetime.now(timezone.utc)-datetime.fromisoformat(item["started_at"])).total_seconds()
                timed_out = alive and elapsed > plan[f"{stage}_timeout_seconds"]
                if timed_out:
                    event("hard_timeout",case=state["cases"][i]["case_id"],stage=stage,pid=item["pid"])
                    kill_owned_worker(item["pid"])
                    if process is not None:
                        process.wait(timeout=15)
                    alive = False
                if alive:
                    continue
                if item["log"]:
                    item["log"].close()
                folder = batch/"cases"/f"{i+1:02d}"
                result_path = folder/f"{stage}_result.json"
                outcome = base.read(result_path,{})
                if timed_out or not outcome or (process is not None and process.returncode not in (0,None)):
                    if timed_out or not outcome:
                        outcome = {"status":"failed","error_type":"StageTimeout" if timed_out else "WorkerExit",
                                   "error":"Worker timed out; saved artifacts retained" if timed_out else "Worker exited without result",
                                   "elapsed_seconds":elapsed,"completed_at":base.stamp()}
                        base.save(result_path,outcome)
                    outcome["status"] = "failed"
                case=state["cases"][i]
                case[stage]=outcome["status"]
                case[f"{stage}_finished_at"]=base.stamp()
                active.pop(key)
                event("end",case=case["case_id"],stage=stage,status=case[stage])
                changed=True
            for case in state["cases"]:
                if case["simulation"]=="failed" and case["diagnosis"]=="pending":
                    case["diagnosis"]="blocked_by_simulation"
            mem=memory_snapshot()
            sim,diag=eligible(state["cases"],sum(s=="simulation" for _,s in active),
                              sum(s=="diagnosis" for _,s in active),plan["diagnosis_workers"],
                              mem.get("available_memory_mb",1e9)>=plan["min_available_memory_mb"],
                              simulations_first=plan.get("simulations_first",False))
            for i in diag:
                launch(i,"diagnosis")
                changed=True
            for i in sim:
                launch(i,"simulation")
                changed=True
            if all(c["simulation"] in TERMINAL and c["diagnosis"] in TERMINAL for c in state["cases"]) and not active:
                break
            now=time.monotonic()
            publish(export=changed or now-last_export>=15)
            if changed or now-last_export>=15:
                last_export=now
            if now-last_resources>=30:
                event("resources",**state["resources"],resource_peaks=peaks)
                last_resources=now
            time.sleep(1)
        publish(export=True)
        failures=sum(c["simulation"]=="failed" or c["diagnosis"]=="failed" for c in state["cases"])
        state.update(phase="finished_with_failures" if failures else "finished",updated_at=base.stamp(),current_event=None,
                     finished_at=base.stamp(),active_workers=[])
        base.save(batch/"status.json",state)
        base.export_tables(batch,state)
        event("finished",counts=state["counts"],resource_peaks=peaks)
    finally:
        if lock.exists() and lock.read_text()==str(os.getpid()):
            lock.unlink()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch",required=True)
    parser.add_argument("--init",action="store_true")
    parser.add_argument("--run",action="store_true")
    parser.add_argument("--model",default="urban_drainage")
    parser.add_argument("--manifest",default="events/exp15_20261103/manifest.json")
    parser.add_argument("--llm-concurrency",type=int,default=10)
    parser.add_argument("--diagnosis-workers",type=int,default=15)
    parser.add_argument("--simulations-first",action="store_true")
    parser.add_argument("--worker",choices=["simulation","diagnosis"])
    parser.add_argument("--case",type=int)
    args=parser.parse_args()
    batch=Path(args.batch).resolve()
    if args.init:
        initialize(batch,args.model,args.manifest,args.llm_concurrency,args.diagnosis_workers,args.simulations_first)
    if args.worker:
        sys.exit(run_worker(batch,args.case,args.worker))
    if args.run:
        controller(batch)


if __name__=="__main__":
    main()
