"""Exercise scheduling isolation without a solver, credentials or API traffic."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("tested_pipeline", Path(__file__).resolve().parents[1]/"scripts/batch_pipeline.py")
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    def test_beta_observation_date_and_audit_follow_the_batch_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            expected="2016-10-08 07:00:00"
            manifest={"recommended_simulation_end":expected}
            self.assertEqual(pipeline.observation_end(root,manifest),expected)
            (root/"swmm").mkdir()
            (root/"summary.json").write_text(json.dumps({"swmm":{"simulation_end":expected},
                                                       "ca2d":{"source_volume_m3":0}}))
            (root/"swmm/model.rpt").write_text("Continuity Error (%) ..... 0.0\nContinuity Error (%) ..... 0.0\n")
            audit=pipeline.base.run_audit(root,expected)
            self.assertEqual(audit["simulation_end"],expected)
            with self.assertRaises(ValueError):
                pipeline.base.run_audit(root,"2024-01-01 07:00:00")

    def test_simulation_barrier_releases_diagnoses_only_after_all_simulations_end(self):
        cases=[{"simulation":"completed","diagnosis":"pending"},
               {"simulation":"running","diagnosis":"pending"}]
        self.assertEqual(pipeline.eligible(cases,1,0,3,simulations_first=True),([],[]))
        cases[1]["simulation"]="completed"
        self.assertEqual(pipeline.eligible(cases,0,0,3,simulations_first=True),([],[0,1]))

    @unittest.skipUnless(os.name=="nt","Windows venv process-tree behavior")
    def test_timeout_terminates_the_worker_process_tree(self):
        source="import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); print(p.pid,flush=True); time.sleep(60)"
        process=subprocess.Popen([sys.executable,"-u","-c",source],stdout=subprocess.PIPE,text=True)
        try:
            child=int(process.stdout.readline().strip())
            time.sleep(0.1)
            members=pipeline.process_tree(process.pid)
            self.assertIn(child,members)
            telemetry=pipeline.memory_snapshot(process.pid)
            self.assertGreater(telemetry["worker_rss_mb"],15)
            pipeline.kill_owned_worker(process.pid)
            process.wait(timeout=5)
            deadline=time.monotonic()+3
            while any(pipeline.base.process_alive(p) for p in members) and time.monotonic()<deadline:
                time.sleep(0.05)
            self.assertFalse(any(pipeline.base.process_alive(p) for p in members))
        finally:
            if process.poll() is None:
                pipeline.kill_owned_worker(process.pid)
                process.wait(timeout=5)
            process.stdout.close()

    def test_simulation_and_diagnosis_can_start_together(self):
        cases = [{"simulation":"completed","diagnosis":"pending"},
                 {"simulation":"pending","diagnosis":"pending"},
                 {"simulation":"pending","diagnosis":"pending"}]
        self.assertEqual(pipeline.eligible(cases,0,0,15),([1],[0]))

    def test_running_and_failed_cases_are_not_resubmitted(self):
        cases = [{"simulation":"completed","diagnosis":"running"},
                 {"simulation":"completed","diagnosis":"failed"},
                 {"simulation":"running","diagnosis":"pending"},
                 {"simulation":"completed","diagnosis":"pending"}]
        self.assertEqual(pipeline.eligible(cases,1,14,15),([],[3]))
        self.assertEqual(pipeline.eligible(cases,1,15,15),([],[]))

    def test_memory_pressure_delays_new_diagnosis(self):
        cases = [{"simulation":"completed","diagnosis":"pending"}]
        self.assertEqual(pipeline.eligible(cases,0,0,15,memory_ok=False),([],[]))

    async def test_api_leases_bound_concurrency_and_release_after_error(self):
        with tempfile.TemporaryDirectory() as folder:
            slots = pipeline.RequestSlots(folder,2)
            active=0
            peak=0
            async def request(index):
                nonlocal active,peak
                token,_=await slots.acquire()
                active+=1
                peak=max(peak,active)
                try:
                    await asyncio.sleep(0.02)
                    if index==1:
                        raise RuntimeError("simulated API failure")
                finally:
                    active-=1
                    slots.release(token)
            results=await asyncio.gather(*(request(i) for i in range(7)),return_exceptions=True)
            self.assertEqual(peak,2)
            self.assertEqual(sum(isinstance(r,RuntimeError) for r in results),1)
            self.assertEqual(list(Path(folder).glob("slot_*.json")),[])

    def test_existing_process_lease_is_not_stolen(self):
        with tempfile.TemporaryDirectory() as folder:
            slots=pipeline.RequestSlots(folder,1)
            first=slots.claim()
            self.assertIsNotNone(first)
            self.assertIsNone(slots.claim())
            slots.release(first)
            second=slots.claim()
            self.assertIsNotNone(second)
            self.assertNotEqual(first[1]["nonce"],second[1]["nonce"])
            slots.release(second)

    async def test_network_gate_preserves_prompt_and_records_wait_separately(self):
        from workflow_agents.diagnosis import service
        original=service.configured_completion
        prompt={"visible_evidence":[{"value":"unchanged input"}]}
        seen=[]
        audit=[]
        async def complete(value,audit=None):
            self.assertIs(value,prompt)
            seen.append(value)
            return {"result":"unchanged output"}
        tokenizer=types.SimpleNamespace(get_encoding=lambda name: types.SimpleNamespace(encode=lambda text,**kwargs:list(text)))
        try:
            service.configured_completion=complete
            with tempfile.TemporaryDirectory() as folder, patch.dict("sys.modules",{"tiktoken":tokenizer}):
                pipeline.install_request_gate(Path(folder),{"llm_concurrency":1,"request_timeout_seconds":2})
                result=await service.configured_completion(prompt,audit=lambda **v:audit.append(v))
                self.assertEqual(result,{"result":"unchanged output"})
                self.assertEqual(len(seen),1)
                self.assertTrue(any(v.get("phase")=="waiting_llm_slot" for v in audit))
                self.assertTrue(any("api_elapsed_seconds" in v for v in audit))
                self.assertEqual(list((Path(folder)/"runtime/llm_slots").glob("slot_*.json")),[])
        finally:
            service.configured_completion=original


if __name__=="__main__":
    unittest.main()
