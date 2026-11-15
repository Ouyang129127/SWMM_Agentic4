"""Recover the saved pystorms_beta narrative through the normal report pipeline."""
import asyncio
import copy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from workflow_agents.common.hashing import sha256_file
from workflow_agents.orchestration.investigation import advance_investigation

MODEL = 'pystorms_beta'
RUN = 'chicago_like_single_4h_40mm_5min__baseline__20261108_105012_449113a3'
TASK = '07532e6972a9424e8cc11b768a606ff9'
RUN_ROOT = ROOT / 'models' / MODEL / 'runs' / RUN
DIRECTORY = RUN_ROOT / 'diagnosis_tasks' / TASK
OLD = DIRECTORY / 'display_reports/r1/20261108_110550_3df2b65a'
OUTPUT = Path(__file__).resolve().parent


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


async def main():
    backup = OUTPUT / 'task_before_recovery.json'
    if backup.exists():
        raise RuntimeError('Recovery already started; inspect the audit before rerunning')
    backup.write_bytes((DIRECTORY / 'task.json').read_bytes())
    files = [DIRECTORY / name for name in ('diagnosis.json', 'verification.json', 'evidence_snapshot.json',
             'report.json', 'preliminary_report_r1.md')]
    files += [RUN_ROOT / 'summary.json', RUN_ROOT / 'evidence/evidence_package.json']
    immutable = {str(path): sha256_file(path) for path in files}
    attempt = OLD / 'narrative_attempt_3.json'
    record = json.loads(attempt.read_text(encoding='utf-8'))
    response = record['raw_response']
    response = json.loads(response) if isinstance(response, str) else response
    old_facts = json.loads((OLD / 'fact_text.json').read_text(encoding='utf-8'))
    audit = dict(source_attempt=str(attempt), source_sha256=sha256_file(attempt),
                 immutable_sources_before=immutable, narrative_edits=[], model_api_calls=0,
                 simulation_rerun=False, diagnosis_rewritten=False)
    save(OUTPUT / 'recovery_audit.json', audit)

    async def replay(prompt):
        assert all(prompt['fact_text'].get(k) == v for k, v in old_facts.items()), 'Fact registry changed'
        return copy.deepcopy(response)

    result = await advance_investigation(MODEL, RUN, 'display_report', TASK, report_complete=replay)
    assert all(sha256_file(Path(name)) == digest for name, digest in immutable.items()), 'Source changed'
    display = result['display_report']
    audit.update(result=display['status'], immutable_sources_unchanged=True, display_report=display)
    save(OUTPUT / 'recovery_audit.json', audit)
    assert display['status'] == 'completed', display
    print(json.dumps(dict(status=display['status'], html_file=display['html_file'],
                          quality_checks=display['quality_checks'], model_api_calls=0),
                     ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
