"""Rain history, prior-pulse context and mechanism migration without model calls."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from workflow_agents.common.hashing import sha256_file
from workflow_agents.diagnosis.contract import CONTRACT_VERSION, validate_initial
from workflow_agents.diagnosis.service import build_diagnosis_prompt, _validate_response
from workflow_agents.evidence.evidence_precision import round_value
from workflow_agents.evidence.rainfall_context import build_rainfall_context, event_rainfall_position
from workflow_agents.orchestration.tasks import prepare_task
from workflow_agents.orchestration.investigation import advance_investigation
from test_initial_diagnosis import fixture, answer


class RainfallTimelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'rain.txt'

    def tearDown(self):
        self.temp.cleanup()

    def rain(self, values):
        self.path.write_text('timestamp,value\n'+'\n'.join(
            f'2024-01-01 {i//6:02}:{(i%6)*10:02}:00,{v}' for i,v in enumerate(values)), encoding='utf-8')
        return build_rainfall_context(self.path)

    def test_two_separate_pulses_gap_and_exact_input(self):
        c=self.rain([0,12.123456789,0,0,6,0])
        self.assertEqual(len(c['wet_periods']),2)
        self.assertEqual([p['peak_time'][11:16] for p in c['rainfall_peaks']],['00:10','00:40'])
        self.assertEqual(c['dry_intervals'][1]['start'],'2024-01-01 00:20:00')
        self.assertEqual(c['dry_intervals'][1]['end'],'2024-01-01 00:40:00')
        self.assertEqual(round_value(c)['rainfall_series'][1]['intensity_mm_per_h'],12.123456789)
        e=event_rainfall_position(c,{'start':'2024-01-01 00:45:00','end':'2024-01-01 00:46:00'})
        self.assertEqual(e['prior_peak_count'],2)
        self.assertEqual(e['peak_before_latest_prior_peak']['peak_time'],'2024-01-01 00:10:00')
        self.assertEqual(e['minutes_since_latest_prior_peak'],5)
        self.assertEqual(e['zero_rain_interval_before_current_wet_period']['duration_minutes'],20)
        self.assertEqual(e['minutes_since_current_wet_period_start'],5)
        self.assertEqual(e['minutes_since_last_completed_wet_period'],25)
        self.assertAlmostEqual(e['recorded_rain_before_event_mm'],12.123456789/6+.5)
        self.assertEqual(e['history_before_record'],'not_provided')

    def test_two_peaks_without_a_dry_gap_stay_one_wet_period(self):
        c=self.rain([0,10,4,8,0])
        self.assertEqual(len(c['wet_periods']),1)
        self.assertEqual(len(c['rainfall_peaks']),2)
        e=event_rainfall_position(c,{'start':'2024-01-01 00:25:00','end':'2024-01-01 00:26:00'})
        self.assertEqual(e['prior_peak_count'],1)
        self.assertEqual(e['next_recorded_peak']['peak_time'],'2024-01-01 00:30:00')

    def test_peak_plateau_counted_once(self):
        c=self.rain([0,10,10,4,0])
        self.assertEqual(len(c['rainfall_peaks']),1)
        self.assertEqual(c['rainfall_peaks'][0]['plateau_end'],'2024-01-01 00:30:00')

    def test_missing_rain_is_not_a_dry_record(self):
        c=build_rainfall_context(self.path)
        e=event_rainfall_position(c,{'start':'2024-01-01','end':'2024-01-01'})
        self.assertEqual(e['status'],'unavailable')
        c=self.rain([0,0,0])
        self.assertEqual(c['timeline_status'],'available')
        self.assertEqual(c['rainfall_peaks'],[])
        self.assertEqual(c['total_rainfall_mm'],0)

    def test_terminal_positive_is_not_invented_dry_weather(self):
        c=self.rain([0,4,8])
        self.assertTrue(c['last_input_point_positive'])
        e=event_rainfall_position(c,{'start':'2024-01-01 00:20:00','end':'2024-01-01 00:21:00'})
        self.assertEqual(e['phase_at_start'],'at_final_input_point_no_following_interval')
        self.assertEqual(c['wet_periods'][-1]['end'],'2024-01-01 00:20:00')

    def test_invalid_rows_order_units_and_values_fail_instead_of_becoming_dry(self):
        for content in ('timestamp,value\nwrong,5','timestamp,value\n2024-01-01,-1',
                        'timestamp,value\n2024-01-01,inf',
                        '2024-01-01 00:10:00,5\n2024-01-01 00:00:00,0',
                        '2024-01-01,5\n2024-01-01,0','unit: inch/h\n2024-01-01,5'):
            with self.subTest(content=content):
                self.path.write_text(content,encoding='utf-8')
                with self.assertRaises(ValueError):
                    build_rainfall_context(self.path)


class ProtocolMigrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)/'m/runs/r'
        (self.root/'evidence').mkdir(parents=True)
        fixture(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self):
        return prepare_task(self.root,'m','r','')

    def test_task_enrichment_preserves_source_and_event_facts(self):
        path=self.root/'evidence/evidence_package.json'
        original=path.read_bytes()
        _,state,rows=self.prepare()
        self.assertEqual(path.read_bytes(),original)
        original_events={r['event_id']:r['value']['event'] for r in json.loads(original)['evidence_rows'] if r['metric_name']=='event_context'}
        for r in rows:
            if r['metric_name']=='event_context':
                self.assertEqual(r['value']['event'],original_events[r['event_id']])
                self.assertEqual(r['value']['rainfall_timing']['status'],'unavailable')
        prompt=build_diagnosis_prompt(state,rows)
        self.assertEqual(prompt['active_mechanism_ids'],['M1','M2','M3','M5','M6'])
        self.assertEqual(state['diagnosis_contract_version'],CONTRACT_VERSION)
        second=next(r for r in rows if r.get('event_id')=='N1:E002' and r['metric_name']=='event_context')
        self.assertEqual(second['value']['rainfall_timing']['previous_same_node_overflow']['event_id'],'N1:E001')

    def test_whole_rain_clock_survives_followup_scope_and_revision(self):
        path=self.root/'rainfall_event.txt'
        path.write_text('timestamp,value\n2024-01-01 00:00:00,4.123456789\n2024-01-01 00:10:00,0',encoding='utf-8')
        package_path=self.root/'evidence/evidence_package.json'
        package=json.loads(package_path.read_text(encoding='utf-8'))
        package['source_hashes']['rainfall_event.txt']=sha256_file(path)
        package_path.write_text(json.dumps(package),encoding='utf-8')
        _,state,rows=prepare_task(self.root,'m','r','分析N1:E002')
        for previous in (None,{'claims':[]}):
            prompt=build_diagnosis_prompt(state,rows,previous)
            rain=[r for r in prompt['visible_evidence'] if r['metric_name']=='rainfall_timeline']
            self.assertEqual(len(rain),1)
            self.assertEqual(rain[0]['value']['rainfall_series'][0]['intensity_mm_per_h'],4.123456789)

    def test_v2_rejects_m4_at_run_event_and_joint_levels(self):
        _,state,rows=self.prepare()
        response=answer(build_diagnosis_prompt(state,rows))
        _validate_response(response,rows,state)
        for scope in ('run','event','joint'):
            altered=copy.deepcopy(response)
            if scope=='joint':
                altered['event_assessments'][0]['joint_assessment']['mechanism_ids']=['M4']
            else:
                target=altered if scope=='run' else altered['event_assessments'][0]
                target['mechanism_assessments'].append({'mechanism_id':'M4','status':'needs_evidence','reason':'legacy'})
            with self.subTest(scope=scope),self.assertRaises(ValueError):
                _validate_response(altered,rows,state)

    def test_archived_six_mechanism_contract_can_still_be_checked(self):
        _,state,rows=self.prepare()
        response=answer(build_diagnosis_prompt(state,rows))
        response['diagnosis_contract_version']='joint_mechanisms_v1'
        for target in [response]+response['event_assessments']:
            target['mechanism_assessments'].append({'mechanism_id':'M4','status':'needs_evidence','reason':'历史判断'})
        validate_initial(response,state['event_manifest'],rows,require_joint=True)

    async def test_old_task_is_readable_but_cannot_silently_use_new_prompt(self):
        with patch('workflow_agents.orchestration.investigation.resolve_run_root',return_value=self.root):
            state=await advance_investigation('m','r')
            path=self.root/'diagnosis_tasks'/state['task_id']/'task.json'
            old=json.loads(path.read_text(encoding='utf-8'))
            old.pop('diagnosis_contract_version')
            path.write_text(json.dumps(old),encoding='utf-8')
            complete=AsyncMock()
            await advance_investigation('m','r','status',state['task_id'])
            with self.assertRaisesRegex(ValueError,'M4移除'):
                await advance_investigation('m','r','diagnose',state['task_id'],complete=complete)
            complete.assert_not_awaited()


if __name__=='__main__':
    unittest.main()
