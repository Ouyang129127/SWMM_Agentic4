import copy
import unittest

from workflow_agents.evidence.structure_audit import build_structure_audit, event_interface_summary


def fixture():
    model = {'available': True, 'options': {'FLOW_UNITS': 'LPS'},
             'nodes': {n: {'node_type': 'JUNCTIONS', 'invert_elevation': z, 'max_depth': 2}
                       for n, z in [('A', 12), ('B', 10), ('C', 9)]}, 'links': {}}
    for key, a, b, diameter in [('up', 'A', 'B', .6), ('down', 'B', 'C', .4)]:
        model['links'][key] = {'link_type': 'CONDUITS', 'from_node': a, 'to_node': b,
            'length': 100, 'inlet_offset': 0, 'outlet_offset': 0, 'offset_convention': 'DEPTH',
            'cross_section': {'shape': 'CIRCULAR', 'geometry_fields': [diameter, 0, 0, 0, 1]}}
    scope = {'target_node': 'B', 'node_roles': {n: 'target' for n in model['nodes']},
             'link_roles': {k: 'corridor' for k in model['links']}, 'status': 'complete_for_policy'}
    return model, scope


class StructureAuditTests(unittest.TestCase):
    def test_contraction_area_crown_and_grade_comparison(self):
        model, scope = fixture()
        row = build_structure_audit(model, scope)['junction_comparisons'][0]
        self.assertIn('downstream_diameter_decrease', row['features'])
        self.assertIn('downstream_grade_decrease', row['features'])
        self.assertAlmostEqual(row['outgoing_over_incoming_full_area'], 4 / 9)
        self.assertAlmostEqual(row['outgoing_minus_incoming_crown'], -.2)
        self.assertTrue(row['same_invert'])
        self.assertFalse(row['same_crown'])

    def test_absolute_and_relative_offsets_produce_identical_geometry(self):
        model, scope = fixture()
        relative = build_structure_audit(model, scope)
        for link in model['links'].values():
            link['offset_convention'] = 'ELEVATION'
            link['inlet_offset'] = model['nodes'][link['from_node']]['invert_elevation']
            link['outlet_offset'] = model['nodes'][link['to_node']]['invert_elevation']
        self.assertEqual(relative['pipe_geometry'], build_structure_audit(model, scope)['pipe_geometry'])
        self.assertEqual(relative['junction_comparisons'], build_structure_audit(model, scope)['junction_comparisons'])

    def test_equal_diameter_equal_grade_does_not_create_a_problem(self):
        model, scope = fixture()
        model['links']['down']['cross_section']['geometry_fields'][0] = .6
        model['links']['down']['length'] = 50
        self.assertEqual(build_structure_audit(model, scope)['findings'], [])

    def test_missing_offset_is_unknown_and_barrels_change_area(self):
        model, scope = fixture()
        model['links']['down']['inlet_offset'] = None
        model['links']['down']['cross_section']['geometry_fields'][4] = 3
        audit = build_structure_audit(model, scope)
        self.assertIsNone(audit['pipe_geometry']['down']['inlet_invert'])
        self.assertNotIn('downstream_full_area_decrease', audit['junction_comparisons'][0]['features'])
        self.assertTrue(audit['unavailable_checks'])

    def test_adverse_grade_and_facility_are_separate_and_inputs_unchanged(self):
        model, scope = fixture()
        model['links']['down']['outlet_offset'] = 2
        model['links']['pump'] = {'link_type': 'PUMPS', 'from_node': 'B', 'to_node': 'C'}
        scope['link_roles']['pump'] = 'facility_boundary'
        before = copy.deepcopy(model)
        audit = build_structure_audit(model, scope)
        self.assertTrue(any('adverse_input_grade' in f['features'] for f in audit['findings']))
        self.assertTrue(any('facility_interface' in f['features'] for f in audit['findings']))
        self.assertNotIn('pump', audit['pipe_geometry'])
        self.assertEqual(model, before)

    def test_scope_boundary_does_not_silently_audit_external_pipes(self):
        model, scope = fixture()
        scope['link_roles'].pop('down')
        audit = build_structure_audit(model, scope)
        self.assertEqual(audit['junction_comparisons'], [])
        self.assertEqual(next(n for n in audit['node_connections'] if n['node_id'] == 'B')['outside_scope_outgoing_links'], ['down'])

    def test_lower_downstream_head_can_still_submerge_the_outlet(self):
        model, scope = fixture()
        audit = build_structure_audit(model, scope)
        series = [{'time': 't', 'nodes': {'A': {'head_m': 13}, 'B': {'head_m': 11}},
                   'links': {'up': {'signed_model_flow_Ls': 5}}}]
        observed = event_interface_summary(audit, series, {'peak_time': 't'})['links']['up']['at_event_peak']
        self.assertGreater(13, 11)
        self.assertAlmostEqual(observed['outlet_head_minus_crown'], .4)
        self.assertEqual(observed['signed_model_flow'], 5)

    def test_pump_curve_type_and_head_opposed_flow_remain_explicit_facts(self):
        model, scope = fixture()
        model['links']['pump'] = {'link_type': 'PUMPS', 'from_node': 'B', 'to_node': 'C', 'input_fields': ['curve', 'OFF', '.3', '.1']}
        model['sections'] = {'CURVES': [['curve', 'Pump1', '1', '2'], ['curve', '2', '0']]}
        scope['link_roles']['pump'] = 'facility_boundary'
        audit = build_structure_audit(model, scope)
        facility = next(f for f in audit['findings'] if f['check_id'] == 'facility:pump')
        self.assertEqual(facility['independent_variable'], 'inlet_node_volume')
        series = [{'time': 't', 'nodes': {}, 'links': {'up': {'signed_model_flow_Ls': -4, 'from_minus_to_head_m': .2}}}]
        values = event_interface_summary(audit, series, {'peak_time': 't'})
        self.assertEqual(values['links']['up']['flow_against_head_difference_samples'], 1)


if __name__ == '__main__':
    unittest.main()
