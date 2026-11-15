"""Change scheduling only; execute the batch's immutable original workers."""
import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time


def select_jobs(state, adopted):
    jobs = []
    for index, case in enumerate(state["cases"]):
        if case["simulation"] != "completed" or case["diagnosis"] in {"completed", "failed", "partial"}:
            continue
        if case["diagnosis"] == "running" and index not in adopted:
            raise RuntimeError(f"Running case requires explicit process adoption: {index}")
        jobs.append(index)
    return jobs


def main(batch, workers, adoption_path, dry_run=False):
    spec = importlib.util.spec_from_file_location("frozen_batch", batch / "code/scripts/batch_exp15.py")
    frozen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(frozen)
    state = frozen.read(batch / "status.json")
    plan = frozen.read(batch / "plan.json")
    transition = frozen.read(adoption_path)
    adopted = {int(key): value for key, value in transition["adopted_workers"].items()}
    jobs = select_jobs(state, adopted)
    if dry_run:
        print(json.dumps({"concurrency": workers, "jobs": jobs, "adopted": adopted,
                          "completed_and_failed_preserved": len(state["cases"]) - len(jobs)}, ensure_ascii=False))
        return
    previous_pid = transition["previous_controller_pid"]
    if frozen.process_alive(previous_pid):
        raise RuntimeError("Previous serial controller must exit before parallel scheduling")
    lock = batch / "parallel_controller.lock"
    if lock.exists() and frozen.process_alive(int(lock.read_text())):
        raise RuntimeError("Parallel controller already running")
    import os
    lock.write_text(str(os.getpid()))
    state.update(phase="diagnosis_parallel", concurrency=workers, scheduler_revision="parallel_v1")
    frozen.save(batch / "parallel_scheduler.json", {"started_at": frozen.stamp(), "concurrency": workers,
                "worker_script": str(batch / "code/scripts/batch_exp15.py"), "transition": str(adoption_path),
                "workflow_change": False, "retry_failed": False, "worker_sha256": frozen.sha(batch / "code/scripts/batch_exp15.py")})

    def run_one(index):
        folder = batch / "cases" / f"{index + 1:02d}"
        result_path = folder / "diagnosis_result.json"
        if index in adopted:
            pid = adopted[index]
            while frozen.process_alive(pid):
                time.sleep(2)
            result = frozen.read(result_path, {})
            if not result:
                result = {"status": "failed", "error_type": "AdoptedWorkerExited",
                          "error": "Existing worker exited without a stage result; artifacts retained"}
                frozen.save(result_path, result)
            return result
        logfile = folder / f"diagnosis_parallel_{datetime.now():%H%M%S}.log"
        python = Path(plan["project"]) / ".venv/Scripts/python.exe"
        command = [str(python), "-u", str(batch / "code/scripts/batch_exp15.py"),
                   "--batch", str(batch), "--worker", "diagnosis", "--case", str(index)]
        with logfile.open("w", encoding="utf-8") as log:
            try:
                result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                        timeout=plan["diagnosis_timeout_seconds"])
                outcome = frozen.read(result_path, {})
                if result.returncode or not outcome:
                    return outcome or {"status": "failed", "error_type": "WorkerExit", "error": str(result.returncode)}
                return outcome
            except subprocess.TimeoutExpired:
                outcome = {"status": "failed", "error_type": "StageTimeout",
                           "error": "Original stage timeout reached; native artifacts retained", "completed_at": frozen.stamp()}
                frozen.save(result_path, outcome)
                return outcome

    active = {}
    queued = iter(jobs)

    def publish():
        state.update(updated_at=frozen.stamp(), current_event=", ".join(state["cases"][i]["event_file"] for i in active.values()))
        frozen.save(batch / "status.json", state)
        frozen.export_tables(batch, state)

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            def launch():
                index = next(queued, None)
                if index is None:
                    return False
                state["cases"][index]["diagnosis"] = "running"
                future = pool.submit(run_one, index)
                active[future] = index
                print(f"{frozen.stamp()} {'ADOPT' if index in adopted else 'START'} diagnosis {state['cases'][index]['event_file']}", flush=True)
                return True
            for _ in range(workers):
                launch()
            publish()
            while active:
                done, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in done:
                    index = active.pop(future)
                    try:
                        outcome = future.result()
                    except Exception as exc:
                        outcome = {"status": "failed", "error_type": type(exc).__name__, "error": str(exc)}
                        frozen.save(batch / "cases" / f"{index + 1:02d}" / "diagnosis_result.json", outcome)
                    state["cases"][index]["diagnosis"] = outcome.get("status", "failed")
                    print(f"{frozen.stamp()} END diagnosis {state['cases'][index]['event_file']} {state['cases'][index]['diagnosis']}", flush=True)
                    launch()
                publish()
        state.update(phase="finished", updated_at=frozen.stamp(), current_event=None)
        frozen.save(batch / "status.json", state)
        frozen.export_tables(batch, state)
    finally:
        if lock.exists() and lock.read_text() == str(os.getpid()):
            lock.unlink()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", required=True)
    parser.add_argument("--workers", type=int, default=3, choices=[1, 2, 3])
    parser.add_argument("--adoption", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(Path(args.batch).resolve(), args.workers, Path(args.adoption).resolve(), args.dry_run)
