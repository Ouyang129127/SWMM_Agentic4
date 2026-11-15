"""Check lossless LLM tables and original-evidence citation validation offline."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from workflow_agents.diagnosis.series_transport import TABLE_FORMAT, PROCESS_METRICS, process_evidence_view
from workflow_agents.diagnosis.service import build_diagnosis_prompt, serialize_prompt
from workflow_agents.evidence.evidence_package import write_package
from workflow_agents.orchestration.investigation import advance_investigation
from test_initial_diagnosis import fixture, answer


def restore(series):
    """Independently reconstruct records after a JSON wire round trip."""
    series = json.loads(json.dumps(series, ensure_ascii=False, allow_nan=False))
    if isinstance(series, list):
        return series
    assert series['format'] == TABLE_FORMAT
    restored = []
    for values in series['rows']:
        assert len(values) == len(series['columns'])
        sample = {}
        for path, value in zip(series['columns'], values):
            target = sample
            for key in path[:-1]:
                target = target.setdefault(key, {})
            target[path[-1]] = value
        restored.append(sample)
    return restored


def samples(count=80, width=3):
    return [{'time': f'2024-01-01 00:{i // 12:02}:{i % 12 * 5:02}',
             'nodes': {f'N{j}': {'head_m': 1.0000000000001 + i + j, 'flooding_Ls': 0.0,
                                'head_method': 'native_output', 'missing': None} for j in range(width)},
             'links': {'L1': {'signed_model_flow_Ls': -2.123456789 if i else -0.0}},
             'empty': {}, 'labels': ['native', 'saved'], 'flag': False}
            for i in range(count)]


def record(series, metric='local_process_series'):
    return {'evidence_id': 'P', 'source_model': 'm', 'run_id': 'r', 'event_id': 'N0:E001',
            'object_type': 'node', 'object_id': 'N0', 'metric_name': metric, 'unit': 'structured',
            'source_file': 'evidence/evidence_package.json', 'calculation_method': 'native_output',
            'value': {'series': series, 'status': 'partial', 'missing_values': 'null, not zero'}}


class SeriesTransportTests(unittest.TestCase):
    def test_complete_samples_roundtrip_without_changing_types_or_precision(self):
        original = record(samples())
        frozen = json.dumps(original, sort_keys=True)
        supplied = process_evidence_view([original])[0]
        table = supplied['value']['series']
        self.assertEqual(table['format'], TABLE_FORMAT)
        restored = restore(table)
        self.assertEqual(json.dumps(restored, sort_keys=True), json.dumps(original['value']['series'], sort_keys=True))
        self.assertEqual(len(table['rows']), 80)
        self.assertEqual(table['columns'][0], ['time'])
        self.assertLess(len(serialize_prompt(supplied)), len(serialize_prompt(original)) * .6)
        # The evidence envelope and coverage metadata keep their full meaning.
        restored_record = copy.deepcopy(supplied)
        restored_record['value']['series'] = restored
        self.assertEqual(restored_record, original)
        table['rows'][0][0] = 'edited'
        table['rows'][0][table['columns'].index(['labels'])].append('edited')
        self.assertEqual(json.dumps(original, sort_keys=True), frozen)

    def test_absent_fields_are_not_replaced_with_null_or_zero(self):
        for change in ('absent', 'new_object', 'scalar', 'empty_object'):
            series = samples()
            if change == 'absent':
                del series[5]['nodes']['N0']['missing']
            elif change == 'new_object':
                series[5]['nodes']['additional'] = {'head_m': 2}
            elif change == 'scalar':
                series[5]['nodes']['N0'] = None
            else:
                series[5]['nodes']['N0'] = {}
            with self.subTest(change=change):
                supplied = process_evidence_view([record(series)])[0]['value']['series']
                self.assertIsInstance(supplied, list)
                self.assertEqual(supplied, series)

    def test_empty_short_and_nonrecord_series_keep_original_format(self):
        for series in ([], [{}], [None], [1, 2], [{'time': 't'}], None):
            with self.subTest(series=series):
                self.assertEqual(process_evidence_view([record(series)]), [record(series)])

    def test_key_paths_preserve_punctuation_and_ignore_dictionary_order(self):
        series = [{'time': str(i), 'nodes': {'N.1/a': {'head_m': i, 'empty': {}}},
                   'nodes.N.1/a.head_m': i + 1} for i in range(80)]
        series[5] = dict(reversed(list(series[5].items())))
        table = process_evidence_view([record(series)])[0]['value']['series']
        self.assertEqual(table['format'], TABLE_FORMAT)
        self.assertIn(['nodes', 'N.1/a', 'head_m'], table['columns'])
        self.assertIn(['nodes.N.1/a.head_m'], table['columns'])
        self.assertEqual(restore(table), series)

    def test_expanded_scope_preserves_every_object_and_sample(self):
        series = samples(count=120, width=32)
        table = process_evidence_view([record(series)])[0]['value']['series']
        self.assertEqual(len(table['rows']), 120)
        self.assertEqual(restore(table), series)
        self.assertIn(['nodes', 'N31', 'head_m'], table['columns'])

    def test_all_supplement_process_metrics_use_same_view(self):
        rows = [dict(record(samples(), metric), evidence_id=metric) for metric in sorted(PROCESS_METRICS)]
        for supplied, original in zip(process_evidence_view(rows), rows):
            self.assertEqual(supplied['value']['series']['format'], TABLE_FORMAT)
            self.assertEqual(restore(supplied['value']['series']), original['value']['series'])
            self.assertEqual(supplied['evidence_id'], original['evidence_id'])

    def test_nonprocess_evidence_and_previous_diagnosis_are_unchanged(self):
        process = record(samples())
        other = dict(record(samples(), 'local_structure'), evidence_id='structure')
        state = {'visible_ids': ['P', 'structure'], 'task_context': {'scope': 'global'},
                 'evidence_overview': {}, 'observations': []}
        previous, feedback = {'claims': [{'evidence_ids': ['P']}]}, {'message': 'review'}
        prompt = build_diagnosis_prompt(state, [process, other], previous, feedback)
        self.assertEqual(prompt['visible_evidence'][1], other)
        self.assertEqual(prompt['previous_diagnosis'], previous)
        self.assertEqual(prompt['verification_feedback'], feedback)
        self.assertEqual([r['evidence_id'] for r in prompt['visible_evidence']], state['visible_ids'])
        # Rebuilding the view is stable, including already-tabulated series.
        self.assertEqual(process_evidence_view(prompt['visible_evidence']), prompt['visible_evidence'])


class OutgoingSeriesTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_completion_and_verification_keep_original_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'm/runs/r'
            (root / 'evidence').mkdir(parents=True)
            package = fixture(root)
            for row in package['evidence_rows']:
                if row['metric_name'] == 'local_process_series':
                    row['value']['series'] = samples()
            write_package(root, package)
            source = root / 'evidence/evidence_package.json'
            source_before = source.read_bytes()
            captured = []

            async def create(messages, **kwargs):
                self.assertTrue(kwargs['json_output'])
                self.assertIn('path_table_v1', messages[0].content)
                prompt = json.loads(messages[1].content)
                captured.append(prompt)
                process = next(r for r in prompt['visible_evidence'] if r['metric_name'] == 'local_process_series')
                self.assertEqual(process['value']['series']['format'], TABLE_FORMAT)
                self.assertEqual(restore(process['value']['series']), samples())
                result = answer(prompt)
                # L1 is inside the process evidence, not the target node. Its
                # claim must still bind using the original records at validation.
                result['claims'] = [{'object_type': 'link', 'object_id': 'L1',
                    'event_id': process['event_id'], 'question_id': 'Q1', 'claim_kind': 'fact',
                    'claim_text': 'L1保存了负向流量。', 'engineering_reason': '有符号流量保存值',
                    'alternatives': '作用机制待查', 'scope': '本事件保存时窗',
                    'evidence_ids': [process['evidence_id']]}]
                return SimpleNamespace(content=json.dumps(result), usage=None, finish_reason='stop')

            client = SimpleNamespace(create=AsyncMock(side_effect=create))
            with patch('workflow_agents.orchestration.investigation.resolve_run_root', return_value=root), \
                 patch.dict('sys.modules', {'workflow_agents.common.llm': SimpleNamespace(deepseek_flash=client)}):
                prepared = await advance_investigation('m', 'r')
                task = prepared['task_id']
                snapshot = root / 'diagnosis_tasks' / task / 'evidence_snapshot.json'
                before = snapshot.read_bytes()
                diagnosed = await advance_investigation('m', 'r', 'diagnose', task)
                self.assertEqual(diagnosed['state'], 'ready_for_verification')
                verified = await advance_investigation('m', 'r', 'verify', task)
                self.assertEqual(verified['verification']['claim_checks'][0]['verification_status'], 'references_verified')
                self.assertEqual(snapshot.read_bytes(), before)
                self.assertEqual(source.read_bytes(), source_before)
                self.assertEqual(len(captured), 1)


if __name__ == '__main__':
    unittest.main()
