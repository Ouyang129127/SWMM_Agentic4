"""Lossless shallow sharing, event isolation and the actual completion path."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from workflow_agents.diagnosis.series_transport import process_evidence_view
from workflow_agents.diagnosis.shared_transport import VIEW_FORMAT, build_evidence_view, restore_shared_evidence
from workflow_agents.diagnosis.service import diagnosis_system_prompt, serialize_prompt, _validate_response
from workflow_agents.diagnosis.prompts import SYSTEM_PROMPT, SHARED_VIEW_INSTRUCTIONS
from workflow_agents.evidence.evidence_package import write_package
from workflow_agents.orchestration.investigation import advance_investigation
from test_initial_diagnosis import fixture, answer
from test_series_transport import samples


NODE = {'node_type': 'JUNCTIONS', 'invert_elevation': 10.123456789,
        'input_fields': ['10.123456789', '2', '0', '0', '0'], 'max_depth': 2,
        'initial_depth': 0, 'surcharge_depth': None, 'ponded_area': 0.0}
LINK = {'link_type': 'CONDUITS', 'from_node': 'N1', 'to_node': 'N2',
        'input_fields': ['50', '.013', '0', '0'], 'length': 50.0, 'roughness': .013,
        'cross_section_fields': ['CIRCULAR', '.4', '0', '0', '0', '1'],
        'cross_section': {'shape': 'CIRCULAR', 'geometry_fields': ['.4', '0', '0', '0', '1']},
        'inlet_offset': -0.0, 'outlet_offset': None, 'offset_convention': 'DEPTH'}
PUMP = {'link_type': 'PUMPS', 'from_node': 'N1', 'to_node': 'N2',
        'input_fields': ['PUMP_CURVE_A', 'ON', '.5', '.1'],
        'cross_section_fields': None, 'offset_convention': 'DEPTH'}


def structure(event, **changes):
    row = {'evidence_id': event + ':structure', 'event_id': event, 'object_type': 'node', 'object_id': 'N1',
           'source_model': 'Example model', 'run_id': 'recorded_run_20261115', 'metric_name': 'local_structure',
           'source_file': 'evidence/evidence_package.json', 'unit': 'structured', 'calculation_method': 'saved_input',
           'value': {'nodes': {'N1': copy.deepcopy(NODE), 'N2': copy.deepcopy(NODE)},
                     'links': {'L1': copy.deepcopy(LINK), 'P0': copy.deepcopy(PUMP)},
                     'topology_scope': {'target_node': 'N1', 'event_test': event},
                     'structure_audit': {'event_interfaces': {'event_id': event, 'head_m': 2 if event.endswith('1') else 3}},
                     'options': {'FLOW_UNITS': 'LPS'}, 'direct_subcatchments': []}}
    row.update(changes)
    return row


def wire(value):
    return json.loads(serialize_prompt(value))


class SharedTransportTests(unittest.TestCase):
    def test_wire_roundtrip_exact_metadata_values_and_isolated_copies(self):
        rows = [structure('N1:E001'), structure('N1:E002')]
        original = serialize_prompt(rows)
        view = build_evidence_view(rows)
        self.assertEqual(view['evidence_view_format'], VIEW_FORMAT)
        self.assertEqual(view['record_defaults'], {'source_model': 'Example model', 'run_id': 'recorded_run_20261115'})
        self.assertTrue(all('source_model' not in r and 'run_id' not in r for r in view['visible_evidence']))
        self.assertLess(len(serialize_prompt(view)), len(serialize_prompt({'visible_evidence': rows})))
        self.assertLess(len(serialize_prompt(view)) + len(SHARED_VIEW_INSTRUCTIONS),
                        len(serialize_prompt({'visible_evidence': rows})))
        restored = restore_shared_evidence(wire(view))
        self.assertEqual(json.dumps(restored, sort_keys=True), json.dumps(rows, sort_keys=True))
        self.assertEqual(serialize_prompt(rows), original)
        self.assertIsNot(restored[0]['value']['nodes']['N1'], restored[1]['value']['nodes']['N1'])
        restored[0]['value']['nodes']['N1']['input_fields'].append('edited')
        self.assertEqual(restored[1]['value']['nodes']['N1'], NODE)
        next(iter(view['shared_static'].values()))['input_fields'].append('edited')
        self.assertEqual(serialize_prompt(rows), original)

    def test_scope_event_interfaces_and_record_provenance_are_not_shared(self):
        rows = [structure('N1:E001'), structure('N1:E002', source_file=None, unit='ft', calculation_method='other')]
        rows[0]['time_start'] = 't1'
        rows[1]['time_start'] = 't2'
        view = build_evidence_view(rows)
        self.assertEqual(set(view['record_defaults']), {'source_model', 'run_id'})
        for original, supplied in zip(rows, view['visible_evidence']):
            for field in ('evidence_id', 'event_id', 'object_id', 'object_type', 'source_file', 'unit', 'calculation_method', 'time_start'):
                self.assertEqual(supplied[field], original[field])
            for field in ('topology_scope', 'structure_audit', 'options', 'direct_subcatchments'):
                self.assertEqual(supplied['value'][field], original['value'][field])
        self.assertEqual(restore_shared_evidence(wire(view)), rows)

    def test_same_id_variants_and_same_definition_different_ids_do_not_merge(self):
        rows = [structure('N1:E001'), structure('N1:E002'), structure('N1:E003')]
        rows[2]['value']['links']['L1']['length'] = 51.0
        rows[2]['value']['nodes']['N1']['max_depth'] = 3
        view = build_evidence_view(rows)
        a,b,c = view['visible_evidence']
        self.assertEqual(a['value']['links']['L1'], b['value']['links']['L1'])
        self.assertEqual(c['value']['links']['L1']['length'], 51.0)
        self.assertNotEqual(a['value']['nodes']['N1'], a['value']['nodes']['N2'])
        self.assertEqual(restore_shared_evidence(wire(view)), rows)

    def test_json_types_missing_null_and_negative_zero_remain_distinct(self):
        rows = [structure(f'N1:E{i:03}') for i in range(6)]
        for row, value in zip(rows, (0, 0.0, -0.0, False, '0', None)):
            row['value']['nodes']['N1']['initial_depth'] = value
        del rows[0]['value']['nodes']['N1']['surcharge_depth']
        rows[1]['value']['nodes']['N1']['ponded_area'] = {}
        restored = restore_shared_evidence(wire(build_evidence_view(rows)))
        self.assertEqual(json.dumps(restored, sort_keys=True), json.dumps(rows, sort_keys=True))

    def test_dynamic_unknown_and_empty_definitions_are_kept_inline(self):
        rows = [structure('N1:E001'), structure('N1:E002')]
        for row in rows:
            row['value']['nodes']['N1']['head_m'] = 9
            row['value']['nodes']['N2'] = None
            row['value']['nodes']['empty'] = {}
            row['value']['links']['L1']['status'] = 'OFF'
        view = build_evidence_view(rows)
        for original, supplied in zip(rows, view['visible_evidence']):
            self.assertEqual(supplied['value']['nodes'], original['value']['nodes'])
            self.assertEqual(supplied['value']['links']['L1'], original['value']['links']['L1'])
        self.assertEqual(restore_shared_evidence(wire(view)), rows)

    def test_facility_input_shared_with_link_but_actions_stay_in_event(self):
        s = structure('N1:E001')
        f = structure('N1:E001', metric_name='facilities_and_storage', evidence_id='facility')
        f['value'] = {'local_facilities': {'P0': copy.deepcopy(PUMP)}, 'controls_present': False,
                      'facility_actions': 'not_extracted', 'storage_volume_process': None}
        view = build_evidence_view([s,f])
        self.assertEqual(view['visible_evidence'][0]['value']['links']['P0'],
                         view['visible_evidence'][1]['value']['local_facilities']['P0'])
        self.assertEqual(view['visible_evidence'][1]['value']['facility_actions'], 'not_extracted')
        self.assertEqual(restore_shared_evidence(wire(view)), [s,f])

    def test_small_single_and_missing_bindings_stay_self_contained(self):
        for rows in ([], [structure('N1:E001')],
                     [{'source_model': 'm', 'run_id': 'r'}, {'source_model': 'm', 'run_id': 'r'}],
                     [{'value': {}}, {'value': None}]):
            with self.subTest(rows=rows):
                self.assertEqual(build_evidence_view(rows), {'visible_evidence': rows})
        with self.assertRaisesRegex(ValueError, 'mixes model/run'):
            build_evidence_view([structure('N1:E001'), structure('N1:E002', run_id='another')])

    def test_first_class_tables_combine_with_sharing_without_losing_values(self):
        rows = [structure('N1:E001'), structure('N1:E002')]
        process = structure('N1:E001', metric_name='local_process_series', evidence_id='process')
        process['value'] = {'series': samples()}
        rows.append(process)
        view = build_evidence_view(rows)
        self.assertEqual(view['visible_evidence'][-1]['value']['series']['format'], 'path_table_v1')
        self.assertEqual(restore_shared_evidence(wire(view)), process_evidence_view(rows))
        self.assertEqual(view, build_evidence_view(rows))

    def test_shared_names_do_not_collide_with_real_evidence_ids(self):
        rows = [structure('N1:E001', evidence_id='S1'), structure('N1:E002', evidence_id='S2')]
        view = build_evidence_view(rows)
        self.assertTrue(view['shared_static'])
        self.assertFalse({'S1','S2'} & set(view['shared_static']))
        self.assertEqual(restore_shared_evidence(wire(view)), rows)

    def test_inline_view_does_not_pay_for_shared_format_instructions(self):
        inline = build_evidence_view([structure('N1:E001')])
        self.assertEqual(diagnosis_system_prompt(inline), SYSTEM_PROMPT)
        shared = build_evidence_view([structure('N1:E001'), structure('N1:E002')])
        self.assertEqual(diagnosis_system_prompt(shared), SYSTEM_PROMPT + SHARED_VIEW_INSTRUCTIONS)

    def test_unknown_dangling_or_nested_references_are_not_accepted(self):
        view = build_evidence_view([structure('N1:E001'), structure('N1:E002')])
        ref = view['visible_evidence'][0]['value']['nodes']['N1']['$ref']
        for definition in (None, {'$ref': 'S999'}):
            changed = copy.deepcopy(view)
            changed['shared_static'][ref] = definition
            with self.assertRaisesRegex(ValueError, 'Invalid static'):
                restore_shared_evidence(changed)
        changed = copy.deepcopy(view)
        del changed['shared_static'][ref]
        with self.assertRaisesRegex(ValueError, 'Invalid static'):
            restore_shared_evidence(changed)
        changed = copy.deepcopy(view)
        changed['evidence_view_format'] = 'unknown'
        with self.assertRaisesRegex(ValueError, 'Unsupported'):
            restore_shared_evidence(changed)


class SharedCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_path_review_and_verification_preserve_original_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'m/runs/r'
            (root/'evidence').mkdir(parents=True)
            package = fixture(root)
            for row in package['evidence_rows']:
                if row['metric_name'] == 'local_structure':
                    row['value'].update(nodes={'N1':copy.deepcopy(NODE), 'N2':copy.deepcopy(NODE)},
                                        links={'L1':copy.deepcopy(LINK), 'P0':copy.deepcopy(PUMP)})
            write_package(root, package)
            source = root/'evidence/evidence_package.json'
            source_before = source.read_bytes()
            captured=[]
            sent=[]

            async def create(messages, **kwargs):
                prompt = json.loads(messages[1].content)
                captured.append(prompt)
                sent.append((messages[0].content,messages[1].content))
                self.assertIn(VIEW_FORMAT, messages[0].content)
                self.assertEqual(prompt['evidence_view_format'], VIEW_FORMAT)
                rows = restore_shared_evidence(prompt)
                s = next(r for r in rows if r['metric_name']=='local_structure')
                self.assertEqual(s['value']['links']['P0'], PUMP)
                response=answer(prompt)
                response['claims']=[{'object_type':'link','object_id':'P0','event_id':s['event_id'],
                    'question_id':'Q1','claim_kind':'fact','claim_text':'本事件调查范围有泵P0。',
                    'engineering_reason':'保存输入定义','alternatives':'运行影响待查','scope':'本事件',
                    'evidence_ids':[s['evidence_id']]}]
                return SimpleNamespace(content=json.dumps(response),usage=None,finish_reason='stop')

            client=SimpleNamespace(create=AsyncMock(side_effect=create))
            with patch('workflow_agents.orchestration.investigation.resolve_run_root',return_value=root), \
                 patch.dict('sys.modules',{'workflow_agents.common.llm':SimpleNamespace(deepseek_flash=client)}):
                prepared=await advance_investigation('m','r')
                task=prepared['task_id'];folder=root/'diagnosis_tasks'/task
                snapshot=(folder/'evidence_snapshot.json').read_bytes()
                await advance_investigation('m','r','diagnose',task)
                verified=await advance_investigation('m','r','verify',task)
                self.assertEqual(verified['verification']['claim_checks'][0]['verification_status'],'references_verified')
                await advance_investigation('m','r','report',task,include_display_report=False)
                previous=(folder/'diagnosis_r1.json').read_bytes()
                review={'issues':[{'message':'复核本事件泵的配置。','evidence_ids':[captured[0]['event_manifest'][0]['evidence_ids'][1]]}]}
                await advance_investigation('m','r','review',task,review_feedback=review)
                revised=await advance_investigation('m','r','diagnose',task)
                self.assertEqual(revised['revision'],2)
                self.assertEqual(captured[1]['previous_diagnosis'],json.loads(previous))
                self.assertEqual((folder/'diagnosis_r1.json').read_bytes(),previous)
                self.assertEqual((folder/'evidence_snapshot.json').read_bytes(),snapshot)
                self.assertEqual(source.read_bytes(),source_before)
                audits=[json.loads(p.read_text(encoding='utf-8')) for p in folder.glob('diagnosis_attempt_*.json')]
                for system,body in sent:
                    audit=next(a for a in audits if a['request_payload_sha256']==hashlib.sha256(body.encode('utf-8')).hexdigest())
                    self.assertEqual(audit['request_characters'],len(system)+len(body))
                    self.assertEqual(audit['system_prompt_sha256'],hashlib.sha256(system.encode('utf-8')).hexdigest())
                # Sharing does not license shared IDs or another event's ID.
                original_rows=json.loads(snapshot)
                for bad_ref in (next(iter(captured[0]['shared_static'])), 'N1:E002:local_structure'):
                    bad=answer(captured[0])
                    bad['event_assessments'][0]['mechanism_assessments'][0].update(status='supported',evidence_ids=[bad_ref])
                    with self.assertRaises(ValueError):
                        _validate_response(bad,original_rows,prepared)


if __name__=='__main__':
    unittest.main()
