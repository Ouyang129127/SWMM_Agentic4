import copy
import unittest

from workflow_agents.diagnosis.structure_contract import validate_structure_assessments
from workflow_agents.evidence.structure_audit import AUDIT_VERSION


class StructureContractTests(unittest.TestCase):
    def setUp(self):
        self.rows = [{'metric_name': 'local_structure', 'event_id': 'B:E001', 'evidence_id': 'structure',
                      'value': {'structure_audit': {'version': AUDIT_VERSION, 'findings': [{'check_id': 'joint:B:up:down'}]}}},
                     {'metric_name': 'local_process_series', 'event_id': 'B:E001', 'evidence_id': 'process'},
                     {'metric_name': 'local_process_series', 'event_id': 'C:E001', 'evidence_id': 'other_event'}]
        self.result = {'event_assessments': [{'event_id': 'B:E001', 'structural_assessment': {
            'status': 'reviewed', 'summary': 'The geometric contraction coincides with restricted outflow.',
            'evidence_ids': ['structure', 'process'], 'finding_assessments': [{
                'check_id': 'joint:B:up:down', 'status': 'relevant',
                'reason': 'Smaller outgoing section and observed upstream rise.', 'evidence_ids': ['structure', 'process']}]}}]}

    def test_accepts_a_complete_review_and_a_reasoned_non_relevant_finding(self):
        validate_structure_assessments(self.result, self.rows)
        review = self.result['event_assessments'][0]['structural_assessment']
        review['finding_assessments'][0].update(status='not_relevant', reason='No matching event outflow restriction observed.')
        validate_structure_assessments(self.result, self.rows)

    def test_rejects_missing_review_missing_finding_and_duplicate_finding(self):
        for kind in ('review', 'missing', 'duplicate'):
            result = copy.deepcopy(self.result)
            item = result['event_assessments'][0]
            if kind == 'review':
                del item['structural_assessment']
            elif kind == 'missing':
                item['structural_assessment']['finding_assessments'] = []
            else:
                item['structural_assessment']['finding_assessments'] *= 2
            with self.assertRaises(ValueError):
                validate_structure_assessments(result, self.rows)

    def test_rejects_cross_event_or_unknown_references(self):
        for ref in ('other_event', 'made_up'):
            result = copy.deepcopy(self.result)
            result['event_assessments'][0]['structural_assessment']['evidence_ids'].append(ref)
            with self.assertRaises(ValueError):
                validate_structure_assessments(result, self.rows)

    def test_retains_legacy_reports_without_an_audit(self):
        rows = copy.deepcopy(self.rows)
        del rows[0]['value']['structure_audit']
        validate_structure_assessments({'event_assessments': [{'event_id': 'B:E001'}]}, rows)

    def test_accepts_empty_findings_without_inventing_an_anomaly(self):
        self.rows[0]['value']['structure_audit']['findings'] = []
        self.result['event_assessments'][0]['structural_assessment']['finding_assessments'] = []
        validate_structure_assessments(self.result, self.rows)


if __name__ == '__main__':
    unittest.main()
