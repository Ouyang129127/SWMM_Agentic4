"""Resumable Example1 exp15 simulation and diagnosis batch, with paper exports."""

import argparse
import asyncio
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import traceback

PROJECT = Path(__file__).resolve().parents[1]


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read(path, default=None):
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else default


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def sha(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def csv_out(path, rows):
    fields = list(dict.fromkeys(key for row in rows for key in row))
    if not fields:
        fields = ["event_file"]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def process_alive(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def initialize(batch):
    if batch.exists():
        raise FileExistsError(f"Batch already exists: {batch}; use --run to resume")
    model = PROJECT / "models/Example1"
    manifest = read(model / "events/exp15/manifest.json")
    assert manifest["event_count"] == len(manifest["events"]) == 15
    # Load credentials only into process memory; never copy .env into archives.
    sys.path.insert(0, str(PROJECT))
    from workflow_agents.common.environment import load_dotenv
    load_dotenv(PROJECT / ".env")
    if not os.getenv("DEEPSEEK_API_KEY"):
        raise RuntimeError("No configured diagnosis API key")
    batch.mkdir(parents=True)
    code = batch / "code"
    paths = list((PROJECT / "workflow_agents").rglob("*.py"))
    paths += [p for p in (PROJECT / "workflow_agents/reporting/prompts").iterdir() if p.is_file()]
    paths += [Path(__file__).resolve(), PROJECT / "requirements.txt"]
    code_hashes = {}
    for path in sorted(set(paths)):
        relative = path.relative_to(PROJECT)
        destination = code / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        code_hashes[str(relative).replace("\\", "/")] = sha(destination)
    inputs = list((model / "static").rglob("*")) + list((model / "mapping").rglob("*"))
    inputs += [model / "swmm/scenarios/baseline/model.inp", model / "model.yaml", model / "simulation_timing.json"]
    inputs += [model / "events/exp15/manifest.json"]
    inputs += [model / row["relative_to_model"] for row in manifest["events"]]
    input_hashes = {}
    for path in inputs:
        if not path.is_file():
            continue
        relative = path.relative_to(model)
        destination = batch / "inputs" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        input_hashes[str(path.resolve())] = sha(path)
    cases = []
    tag = datetime.now().strftime("%m%d_%H%M%S")
    for event in manifest["events"]:
        path = model / event["relative_to_model"]
        assert sha(path) == event["sha256"]
        cases.append({"event_file": event["event_file"], "rainfall_file": event["relative_to_model"],
                      "event_sha256": event["sha256"], "planned_total_mm": event["total_mm"],
                      "run_id": f"exp15_{tag}_{path.stem}", "simulation": "pending", "diagnosis": "pending"})
    plan = {"batch_id": batch.name, "created_at": stamp(), "project": str(PROJECT), "model_name": "Example1",
            "scenario_name": "baseline", "simulation_end_time": "2024-01-01 07:00:00",
            "max_supplement_rounds": 2, "max_diagnosis_attempts_per_case": 9,
            "simulation_timeout_seconds": 1800, "diagnosis_timeout_seconds": 2700,
            "failure_policy": "record and continue; no automatic rerun of failed cases",
            "report_policy": "verified structured JSON and deterministic Markdown at every revision; HTML deferred",
            "study_role": "development_batch_not_independent_method_comparison",
            "llm_configuration": {"model": os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
                                  "provider": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                                  "temperature": 0, "reasoning_effort": "high"},
            "code_sha256": code_hashes, "input_sha256": input_hashes, "cases": cases}
    save(batch / "plan.json", plan)
    save(batch / "status.json", {"phase": "prepared", "updated_at": stamp(), "cases": cases})
    versions = subprocess.run([sys.executable, "-m", "pip", "freeze"], capture_output=True, text=True)
    (batch / "python_packages.txt").write_text(versions.stdout, encoding="utf-8")
    (batch / "README.md").write_text(
        "# Example1 exp15 批处理\n\n"
        "先顺序完成15场 SWMM→CA2D→证据，再逐场开展全场诊断、引用核查和最多两轮补证。"
        "观察截止统一2024-01-01 07:00:00；原模型时间文件不修改。\n\n"
        "输入与代码冻结在 inputs/、code/；plan.json保存哈希和执行设置，不含API密钥。"
        "原生INP/RPT/OUT、SWMM/CA2D时序、证据、诊断各版、补证、原始模型响应及审计保存在原模型runs/下的独立exp15运行。\n\n"
        "cases/保存阶段日志、任务状态检查点及逐场文件哈希；tables/持续更新运行、事件、机制、陈述、请求及调用表。"
        "status.json为机器进度；PROGRESS.md为阅读入口。失败不伪装成成功，不自动改小任务范围。\n\n"
        "本批是既定工作流的开发运行，尚未开展独立语义评分或V0—V3对照。"
        "引用核查范围为可追溯性，结果表中的supported为诊断输出状态。\n\n"
        "同一批恢复命令：`python scripts/batch_exp15.py --run --batch <本目录绝对路径>`。"
        "已完成阶段跳过；已失败阶段保留，需明确追加--retry-failed才重新尝试。\n",
        encoding="utf-8")
    print(json.dumps({"batch": str(batch), "prepared_cases": len(cases)}, ensure_ascii=False), flush=True)


def configure_worker(batch):
    plan = read(batch / "plan.json")
    original = Path(plan["project"])
    # Select the frozen implementation before importing any project modules.
    sys.path.insert(0, str(batch / "code"))
    os.environ["SWMM_MODELS_DIR"] = str(original / "models")
    os.environ["SWMM_RUNTIME_DIR"] = str(batch / "runtime")
    from workflow_agents.common.environment import load_dotenv
    load_dotenv(original / ".env")
    if os.getenv("DEEPSEEK_MODEL", "deepseek-flash") != plan["llm_configuration"]["model"]:
        raise ValueError("Configured model changed after batch initialization")
    for name, expected in plan["input_sha256"].items():
        if not Path(name).is_file() or sha(Path(name)) != expected:
            raise ValueError(f"Batch input changed: {name}")
    for name, expected in plan["code_sha256"].items():
        if sha(batch / "code" / name) != expected:
            raise ValueError(f"Frozen code changed: {name}")
    return plan


def run_audit(root, expected_end="2024-01-01 07:00:00"):
    summary = read(root / "summary.json")
    report = (root / "swmm/model.rpt").read_text(errors="replace")
    errors = [float(v) for v in re.findall(r"Continuity Error \(%\)\s*\.+\s+([-\d.]+)", report)]
    convergence = re.search(r"% of Steps Not Converging\s*:\s*([\d.]+)", report)
    warnings = [line.strip() for line in report.splitlines() if "WARNING" in line or "ERROR" in line]
    audit = {"runoff_continuity_error_pct": errors[0] if errors else None,
             "routing_continuity_error_pct": errors[1] if len(errors) > 1 else None,
             "nonconverging_steps_pct": float(convergence[1]) if convergence else None,
             "warnings": warnings, "review_status": "recorded_not_independent_quality_acceptance",
             "simulation_end": summary["swmm"]["simulation_end"],
             "source_volume_m3": summary["ca2d"].get("source_volume_m3"),
             "coupling_volume_error_m3": summary["ca2d"].get("coupling_volume_error_m3")}
    if any("ERROR" in line for line in warnings):
        raise ValueError("SWMM report contains an ERROR")
    if audit["simulation_end"] != expected_end:
        raise ValueError("Observation end does not match batch protocol")
    save(root / "batch_hydraulic_audit.json", audit)
    return audit


def artifact_manifest(root, case_dir):
    records = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            records.append({"relative_path": str(path.relative_to(root)).replace("\\", "/"),
                            "bytes": path.stat().st_size, "sha256": sha(path)})
    save(case_dir / "artifacts.json", {"run_root": str(root), "captured_at": stamp(), "files": records})


async def diagnose_case(plan, case, root, case_dir):
    from workflow_agents.orchestration.investigation import advance_investigation
    pointer = read(case_dir / "task_pointer.json", {})
    task_id = pointer.get("task_id", "")
    count = len(list((case_dir / "checkpoints").glob("*.json")))

    async def action(name):
        nonlocal task_id, count
        print(f"{stamp()} {case['event_file']} action={name}", flush=True)
        result = await advance_investigation(plan.get("model_name", "Example1"), case["run_id"], name, task_id,
                                             include_display_report=False)
        task_id = result["task_id"]
        save(case_dir / "task_pointer.json", {"task_id": task_id})
        count += 1
        task_path = root / "diagnosis_tasks" / task_id / "task.json"
        save(case_dir / "checkpoints" / f"{count:03d}_{name}.json", read(task_path))
        return result

    state = await action("status" if task_id else "prepare")
    task_dir = root / "diagnosis_tasks" / task_id
    # Keep exact first-visible state; revisions and supplements have their own immutable files.
    if not (case_dir / "initial_task.json").exists():
        save(case_dir / "initial_task.json", read(task_dir / "task.json"))
    repairs = 0
    reference_repairs = 0
    supplement_rounds = sum(h.get("action") == "evidence" for h in state.get("history", []))
    stop_reason = ""
    for _ in range(35):
        current = state["state"]
        if current in {"ready_for_diagnosis", "revision_requested"}:
            if len(list(task_dir.glob("diagnosis_attempt_*.json"))) >= plan["max_diagnosis_attempts_per_case"]:
                stop_reason = "diagnosis_attempt_budget_reached"
                break
            try:
                state = await action("diagnose")
                repairs = 0
            except Exception:
                state = read(task_dir / "task.json")
                failed = state.get("last_attempt", {})
                if failed.get("phase") in {"parsing", "validation"} and repairs < 1:
                    repairs += 1
                    continue
                raise
        elif current == "ready_for_verification":
            state = await action("verify")
            if state["state"] == "revision_requested":
                reference_repairs += 1
                if reference_repairs > 1:
                    stop_reason = "reference_repair_budget_reached"
                    break
        elif current == "ready_for_report":
            state = await action("report")
        elif current in {"preliminary_delivered", "awaiting_evidence_confirmation"}:
            if not state.get("pending_evidence_requests"):
                stop_reason = "no_executable_pending_requests"
                break
            if supplement_rounds >= plan["max_supplement_rounds"]:
                stop_reason = "supplement_round_budget_reached"
                break
            supplement_rounds += 1
            state = await action("evidence")
            if not state.get("last_evidence_added_ids"):
                stop_reason = "no_new_evidence_available"
                break
        else:
            raise ValueError(f"Unexpected task state: {current}")
    else:
        stop_reason = "action_budget_reached"
    current_report = state.get("report_revision") == state.get("revision") and (task_dir / f"preliminary_report_r{state['revision']}.md").exists()
    complete = current_report and stop_reason in {"no_executable_pending_requests", "no_new_evidence_available"}
    return {"status": "completed" if complete else "partial", "task_id": task_id,
            "task_state": state["state"], "revision": state["revision"],
            "current_verified_report": current_report, "stop_reason": stop_reason,
            "supplement_rounds": supplement_rounds, "pending_evidence_requests": state.get("pending_evidence_requests", False),
            "report_file": state.get("report_file", "")}


def worker(batch, index, stage):
    case_dir = batch / "cases" / f"{index + 1:02d}"
    case_dir.mkdir(parents=True, exist_ok=True)
    result_path = case_dir / f"{stage}_result.json"
    started = time.monotonic()
    try:
        plan = configure_worker(batch)
        case = plan["cases"][index]
        model_name = plan.get("model_name", "Example1")
        root = Path(plan["project"]) / "models" / model_name / "runs" / case["run_id"]
        if stage == "simulation":
            from workflow_agents.scenario.requests import prepare_scenario_request
            from workflow_agents.simulation.runner import run_swmm_2d_project_from_rainfall
            from workflow_agents.common.state import build_state, save_state
            from workflow_agents.common.schemas import RUN_READY
            from workflow_agents.orchestration.run_preparation import run_workflow_stage
            if not (root / "summary.json").exists():
                if (root / "swmm/model.rpt").exists():
                    raise ValueError("Partial simulation retained; refusing to overwrite native results")
                request = prepare_scenario_request(model_name, case["rainfall_file"], Path(case["event_file"]).stem,
                                                   "baseline", case["run_id"])
                payload = dict(request["scenario_request"], simulation_end_time=plan["simulation_end_time"])
                save(root / "scenario_request.json", payload)
                output = run_swmm_2d_project_from_rainfall(model_name, case["rainfall_file"],
                            Path(case["event_file"]).stem, "baseline", case["run_id"],
                            expected_event_sha256=case["event_sha256"], simulation_end_time=plan["simulation_end_time"])
                if not (root / "summary.json").exists():
                    raise RuntimeError(output)
            audit = run_audit(root, plan["simulation_end_time"])
            if not (root / "workflow_state.json").exists() or read(root / "workflow_state.json")["state"] == "SCENARIO_READY":
                save_state(root, build_state(model_name, case["run_id"], root, state=RUN_READY))
            result = run_workflow_stage(model_name, case["run_id"], target_stage="evidence_building")
            if not result["ok"]:
                raise RuntimeError(result["message"])
            from workflow_agents.evidence.evidence_package import load_package
            from workflow_agents.diagnosis.contract import event_manifest
            events = event_manifest(load_package(root, verify_sources=True))
            save(case_dir / "events.json", events)
            outcome = {"status": "completed", "event_count": len(events),
                       "overflow_node_count": len({e["node_id"] for e in events}), "audit": audit}
        else:
            outcome = asyncio.run(diagnose_case(plan, case, root, case_dir))
        outcome.update(elapsed_seconds=round(time.monotonic() - started, 3), completed_at=stamp())
        save(result_path, outcome)
        artifact_manifest(root, case_dir)
        print(json.dumps(outcome, ensure_ascii=False), flush=True)
        return 0
    except Exception as exc:
        traceback.print_exc()
        save(result_path, {"status": "failed", "error_type": type(exc).__name__, "error": str(exc),
                           "elapsed_seconds": round(time.monotonic() - started, 3), "completed_at": stamp()})
        return 1


def export_tables(batch, state):
    tables = batch / "tables"
    tables.mkdir(exist_ok=True)
    plan = read(batch / "plan.json")
    runs, events, calls, mechanisms, claims, requests = [], [], [], [], [], []
    for index, case in enumerate(state["cases"]):
        root = Path(plan["project"]) / "models" / plan.get("model_name", "Example1") / "runs" / case["run_id"]
        case_dir = batch / "cases" / f"{index + 1:02d}"
        row = {k: v for k, v in case.items() if not isinstance(v, (dict, list))}
        simulation = read(case_dir / "simulation_result.json", {})
        diagnosis = read(case_dir / "diagnosis_result.json", {})
        row.update({"simulation_seconds": simulation.get("elapsed_seconds"), "diagnosis_seconds": diagnosis.get("elapsed_seconds"),
                    "event_count": simulation.get("event_count"), "overflow_node_count": simulation.get("overflow_node_count"),
                    "revision": diagnosis.get("revision"), "diagnosis_stop_reason": diagnosis.get("stop_reason"),
                    "run_root": str(root), "task_id": diagnosis.get("task_id", read(case_dir / "task_pointer.json", {}).get("task_id", ""))})
        row.update({k: v for k, v in simulation.get("audit", {}).items() if not isinstance(v, (dict, list))})
        summary = read(root / "summary.json", {})
        effective = root / "swmm/rainfall_for_simulation.txt"
        if effective.exists():
            with effective.open(encoding="utf-8") as stream:
                rainfall = list(csv.DictReader(stream))
            row["applied_rainfall_mm"] = sum(float(v["value"]) for v in rainfall) / 12
        row["surface_max_depth_m"] = summary.get("ca2d", {}).get("max_depth_m")
        runs.append(row)
        identity = {"event_file": case["event_file"], "run_id": case["run_id"]}
        for event in read(case_dir / "events.json", []):
            events.append(dict(identity, **event["facts"]))
        for task_dir in sorted((root / "diagnosis_tasks").glob("*")):
            if not task_dir.is_dir():
                continue
            task_identity = dict(identity, task_id=task_dir.name)
            for path in sorted(task_dir.glob("diagnosis_attempt_*.json")):
                audit = read(path)
                calls.append(dict(task_identity, attempt_id=audit.get("attempt_id"), response_source=audit.get("response_source"),
                                  network_call_started=audit.get("network_call_started"), status=audit.get("status"),
                                  phase=audit.get("phase"), elapsed_seconds=audit.get("elapsed_seconds"),
                                  llm_queue_seconds=audit.get("llm_queue_seconds"), api_elapsed_seconds=audit.get("api_elapsed_seconds"),
                                  request_characters=audit.get("request_characters"), request_utf8_bytes=audit.get("request_utf8_bytes"),
                                  approximate_prompt_tokens=audit.get("approximate_prompt_tokens"),
                                  prompt_tokens=audit.get("usage", {}).get("prompt_tokens"),
                                  completion_tokens=audit.get("usage", {}).get("completion_tokens"),
                                  model=audit.get("model"), error=audit.get("error_message"), audit_file=str(path)))
            for path in sorted(task_dir.glob("diagnosis_r*.json")):
                diagnosis_data = read(path)
                revision_identity = dict(task_identity, revision=diagnosis_data["revision"])
                for claim in diagnosis_data.get("claims", []):
                    claims.append(dict(revision_identity, level="task", claim=claim))
                assessments = [("task", "", "", diagnosis_data.get("mechanism_assessments", []))]
                for event in diagnosis_data.get("event_assessments", []):
                    assessments.append(("event", event.get("event_id", ""), event.get("node_id", ""), event.get("mechanism_assessments", [])))
                    for claim in event.get("claims", []):
                        claims.append(dict(revision_identity, level="event", event_id=event.get("event_id"), claim=claim))
                for level, event_id, node_id, items in assessments:
                    for item in items:
                        mechanisms.append(dict(revision_identity, level=level, event_id=event_id, node_id=node_id,
                                               mechanism_id=item.get("mechanism_id"), status=item.get("status"),
                                               reason=item.get("reason"), evidence_ids=json.dumps(item.get("evidence_ids", []), ensure_ascii=False)))
                for request in diagnosis_data.get("evidence_requests", []):
                    requests.append(dict(revision_identity, request=request))
    csv_out(tables / "runs.csv", runs)
    csv_out(tables / "events.csv", events)
    csv_out(tables / "calls.csv", calls)
    csv_out(tables / "mechanisms.csv", mechanisms)
    for name, rows in (("claims.jsonl", claims), ("requests.jsonl", requests)):
        (tables / name).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    text = ["# 批处理进度", "", f"更新时间：{state['updated_at']}；阶段：{state['phase']}", "",
            "| 降雨 | 模拟与证据 | 诊断 | 事件数 | 修订 |", "|---|---|---|---:|---:|"]
    for row in runs:
        text.append(f"| {row['event_file']} | {row['simulation']} | {row['diagnosis']} | {row.get('event_count', '')} | {row.get('revision', '')} |")
    text += ["", "详细状态见status.json；失败原因见cases/中的阶段结果和日志。", ""]
    (batch / "PROGRESS.md").write_text("\n".join(text), encoding="utf-8")


def controller(batch, retry_failed=False):
    plan = read(batch / "plan.json")
    state = read(batch / "status.json")
    lock = batch / "controller.lock"
    if lock.exists():
        pid = int(lock.read_text())
        if process_alive(pid):
            raise RuntimeError(f"Batch controller already running: {pid}")
    with lock.open("x" if not lock.exists() else "w") as stream:
        stream.write(str(os.getpid()))
    try:
        for stage in ("simulation", "diagnosis"):
            state["phase"] = stage
            for index, case in enumerate(state["cases"]):
                if case[stage] in {"completed", "partial"} or (case[stage] == "failed" and not retry_failed):
                    continue
                if stage == "diagnosis" and case["simulation"] != "completed":
                    case["diagnosis"] = "blocked_by_simulation"
                    continue
                case_dir = batch / "cases" / f"{index + 1:02d}"
                case_dir.mkdir(parents=True, exist_ok=True)
                case[stage] = "running"
                state.update(updated_at=stamp(), current_event=case["event_file"])
                save(batch / "status.json", state)
                export_tables(batch, state)
                print(f"{stamp()} START {stage} {case['event_file']}", flush=True)
                suffix = datetime.now().strftime("%H%M%S")
                command = [sys.executable, "-u", str(batch / "code/scripts/batch_exp15.py"),
                           "--batch", str(batch), "--worker", stage, "--case", str(index)]
                with (case_dir / f"{stage}_{suffix}.log").open("w", encoding="utf-8") as log:
                    try:
                        process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                                 timeout=plan[f"{stage}_timeout_seconds"])
                        outcome = read(case_dir / f"{stage}_result.json", {})
                        case[stage] = outcome.get("status", "failed") if process.returncode == 0 else "failed"
                    except subprocess.TimeoutExpired:
                        case[stage] = "failed"
                        save(case_dir / f"{stage}_result.json", {"status": "failed", "error_type": "StageTimeout",
                                                               "error": "Stage timeout; saved artifacts retained", "completed_at": stamp()})
                state["updated_at"] = stamp()
                save(batch / "status.json", state)
                export_tables(batch, state)
                print(f"{stamp()} END {stage} {case['event_file']} {case[stage]}", flush=True)
        state.update(phase="finished", updated_at=stamp(), current_event=None)
        save(batch / "status.json", state)
        export_tables(batch, state)
    finally:
        if lock.exists() and lock.read_text() == str(os.getpid()):
            lock.unlink()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", required=True)
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--worker", choices=["simulation", "diagnosis"])
    parser.add_argument("--case", type=int)
    args = parser.parse_args()
    destination = Path(args.batch).resolve()
    if args.init:
        initialize(destination)
    if args.worker:
        sys.exit(worker(destination, args.case, args.worker))
    if args.run:
        controller(destination, args.retry_failed)
