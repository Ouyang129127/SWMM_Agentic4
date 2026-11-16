"""Pre-event history coverage, sharing, sampling, provenance and wire format."""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from test_initial_diagnosis import fixture
from test_series_transport import restore
from workflow_agents.common.hashing import sha256_file
from workflow_agents.diagnosis.contract import check_claim_binding
from workflow_agents.diagnosis.service import build_diagnosis_prompt
from workflow_agents.evidence.antecedent_context import context_times, METRIC
from workflow_agents.evidence.evidence_package import write_package
from workflow_agents.orchestration.tasks import prepare_task


class AntecedentContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)/'m/runs/r'
        (self.root/'evidence').mkdir(parents=True)
        self.package = fixture(self.root)
        # The two overflow events lie well after a first rain pulse and gap.
        for row in self.package['evidence_rows']:
            if row['metric_name']=='event_context':
                f=row['value']['event']
                minute=20 if row['event_id']=='N1:E001' else 40
                f.update(start=f'2024-01-01 00:{minute:02}:00',end=f'2024-01-01 00:{minute+1:02}:00',
                         peak_time=f'2024-01-01 00:{minute:02}:00',last_positive_time=f'2024-01-01 00:{minute:02}:00')
        (self.root/'swmm').mkdir()
        self.input=self.root/'swmm/model_with_event.inp'
        self.input.write_text('[OPTIONS]\nFLOW_UNITS LPS\nREPORT_STEP 00:00:10\n'
            '[JUNCTIONS]\nN1 0 1 0\nN2 0 1 0\n[CONDUITS]\nL1 N1 N2 10 .01 0 0\n'
            '[XSECTIONS]\nL1 CIRCULAR 1 0 0 0\n',encoding='utf-8')
        self.output=self.root/'swmm/model.out'
        self.output.write_bytes(b'native fixture read through injected reader')
        self.rain=self.root/'rainfall_event.txt'
        self.rain.write_text('timestamp,value\n2024-01-01 00:00:00,0\n2024-01-01 00:05:00,10\n'
            '2024-01-01 00:10:00,0\n2024-01-01 00:30:00,8\n2024-01-01 00:45:00,0',encoding='utf-8')
        self.package['source_hashes']={str(p.relative_to(self.root)):sha256_file(p)
                                       for p in (self.input,self.output,self.rain)}
        write_package(self.root,self.package)
        origin=datetime(2024,1,1)
        self.times=[(origin+timedelta(seconds=i*10)).isoformat(sep=' ') for i in range(301)]
        self.native={'nodes':{node:{t:{'head_m':.2,'depth_m':.2,'node_volume_m3':.4,
                            'total_inflow_Ls':2.,'lateral_inflow_Ls':.5,'flooding_Ls':0.}
                            for t in self.times} for node in ('N1','N2')},
                     'links':{'L1':{t:{'depth_m':.1,'link_volume_m3':1.23456789,'signed_model_flow_Ls':-2.}
                                    for t in self.times}},'metadata':{'volume_semantics':'native stored water'}}

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, question=''):
        with patch('workflow_agents.evidence.antecedent_context._native',return_value=self.native) as reader:
            result=prepare_task(self.root,'m','r',question)
        return result,reader

    def test_shared_history_precedes_both_events_and_keeps_cutoffs(self):
        before=(self.root/'evidence/evidence_package.json').read_bytes()
        (directory,state,rows),reader=self.prepare()
        histories=[r for r in rows if r['metric_name']==METRIC]
        self.assertEqual(len(histories),1)
        reader.assert_called_once()
        history=histories[0]
        self.assertEqual(history['value']['actual_time_start'],'2024-01-01 00:00:00')
        self.assertEqual(history['value']['actual_time_end'],'2024-01-01 00:40:00')
        self.assertIn(history['evidence_id'],state['visible_ids'])
        contexts=[r for r in rows if r['metric_name']=='event_context']
        self.assertEqual({r['value']['antecedent_hydraulics']['evidence_id'] for r in contexts},{history['evidence_id']})
        self.assertEqual(contexts[0]['value']['antecedent_hydraulics']['event_cutoff'],'2024-01-01 00:20:00')
        self.assertEqual(contexts[1]['value']['antecedent_hydraulics']['event_cutoff'],'2024-01-01 00:40:00')
        self.assertEqual((self.root/'evidence/evidence_package.json').read_bytes(),before)
        original={r['evidence_id']:r['value'] for r in self.package['evidence_rows'] if r['metric_name']=='local_process_series'}
        self.assertTrue(all(r['value']==original[r['evidence_id']] for r in rows if r['metric_name']=='local_process_series'))
        self.assertEqual(len(state['event_manifest'][0]['evidence_ids']),6)

    def test_cadence_keeps_actual_samples_and_brackets_unsaved_anchor(self):
        selected=context_times(self.times,{'2024-01-01 00:07:05'})
        self.assertIn('2024-01-01 00:07:00',selected)
        self.assertIn('2024-01-01 00:07:10',selected)
        self.assertNotIn('2024-01-01 00:07:05',selected)
        self.assertTrue(set(selected)<=set(self.times))
        self.assertLess(len(selected),len(self.times)/5)
        self.assertEqual(context_times([],{'2024-01-01'}),[])

    def test_sparse_native_clock_is_not_upsampled(self):
        times=['2024-01-01 00:00:00','2024-01-01 00:20:00']
        self.assertEqual(context_times(times,{'2024-01-01 00:05:00'}),times)

    def test_history_uses_lossless_table_and_retains_negative_flow_and_precision(self):
        (_,state,rows),_=self.prepare()
        original=copy.deepcopy(rows)
        prompt=build_diagnosis_prompt(state,rows)
        history=next(r for r in prompt['visible_evidence'] if r['metric_name']==METRIC)
        self.assertEqual(history['value']['series']['format'],'path_table_v1')
        plain=restore(history['value']['series'])
        self.assertEqual(plain,next(r['value']['series'] for r in rows if r['metric_name']==METRIC))
        self.assertEqual(plain[0]['links']['L1']['signed_model_flow_Ls'],-2.)
        self.assertEqual(plain[0]['links']['L1']['link_volume_m3'],1.23456789)
        self.assertEqual(rows,original)

    def test_event_scope_has_one_relevant_history_without_other_event_binding(self):
        (_,state,rows),reader=self.prepare('分析N1:E002')
        history=next(r for r in rows if r['metric_name']==METRIC)
        self.assertEqual(history['value']['related_event_ids'],['N1:E002'])
        self.assertIn(history['evidence_id'],state['visible_ids'])
        self.assertNotIn('N1:E001:event_context',state['visible_ids'])
        reader.assert_called_once()

    def test_missing_or_unbound_native_output_remains_unknown(self):
        for missing in (False,True):
            package=copy.deepcopy(self.package)
            package['source_hashes'].pop(str(self.output.relative_to(self.root)))
            write_package(self.root,package)
            if missing:
                self.output.unlink()
            (_,state,rows),reader=self.prepare()
            reader.assert_not_called()
            self.assertFalse(any(r['metric_name']==METRIC for r in rows))
            for row in rows:
                if row['metric_name']=='event_context':
                    self.assertEqual(row['value']['antecedent_hydraulics']['status'],'unavailable')

    def test_missing_object_sample_is_not_filled_with_zero(self):
        del self.native['links']['L1'][self.times[0]]
        (_,state,rows),_=self.prepare()
        history=next(r for r in rows if r['metric_name']==METRIC)
        self.assertEqual(history['value']['status'],'partial')
        self.assertEqual(history['value']['series'][0]['links']['L1'],{})

    def test_source_change_during_native_read_is_rejected(self):
        def changed(*args):
            self.output.write_bytes(b'changed')
            return self.native
        with patch('workflow_agents.evidence.antecedent_context._native',side_effect=changed):
            with self.assertRaisesRegex(ValueError,'changed during extraction'):
                prepare_task(self.root,'m','r','')

    def test_history_supports_objects_but_never_licenses_an_unrelated_event(self):
        (_,state,rows),_=self.prepare()
        history=next(r for r in rows if r['metric_name']==METRIC)
        claim={'object_type':'link','object_id':'L1','event_id':'N1:E001',
               'evidence_ids':[history['evidence_id'],'N1:E001:local_structure']}
        check_claim_binding(claim,rows,'test')
        claim['event_id']='OTHER:E001'
        with self.assertRaises(ValueError):
            check_claim_binding(claim,rows,'test')


if __name__=='__main__':
    unittest.main()
