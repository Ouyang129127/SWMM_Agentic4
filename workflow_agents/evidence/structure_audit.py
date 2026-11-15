"""Reproducible geometry comparisons within the declared event topology scope.

Findings describe input geometry, not design compliance or hydraulic causes.
The input direction is retained even when event flow reverses.
"""
import math


AUDIT_VERSION = 'event_structure_audit_v1'
EPS = 1e-8  # Numerical equality tolerance, not an engineering acceptance limit.


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def section_geometry(link):
    cross = link.get('cross_section', {})
    shape, fields = cross.get('shape'), cross.get('geometry_fields', [])
    if not shape:
        raw = link.get('cross_section_fields') or []
        shape, fields = (raw[0], raw[1:]) if raw else (None, [])
    values = [number(v) for v in fields]
    height = values[0] if values and values[0] is not None and values[0] > 0 else None
    barrels = values[4] if len(values) > 4 else 1.0
    if barrels is None or barrels <= 0:
        barrels = None
    diameter, area, closed = None, None, False
    if shape in {'CIRCULAR', 'FORCE_MAIN'}:
        diameter, closed = height, True
        area = math.pi * height ** 2 / 4 if height is not None else None
    elif shape in {'RECT_CLOSED', 'RECT_OPEN'}:
        width = values[1] if len(values) > 1 else None
        closed = shape == 'RECT_CLOSED'
        area = height * width if height is not None and width is not None and width > 0 else None
    else:
        # Shape-dependent fields must not be guessed for custom/irregular sections.
        height = None
    return {'shape': shape, 'height': height, 'closed': closed, 'diameter': diameter,
            'barrels': barrels, 'full_area_all_barrels': area * barrels if area is not None and barrels is not None else None}


def event_interface_summary(audit, series, event):
    """Relate saved heads to actual pipe-end levels; never classify backwater."""
    result = {}
    for key, pipe in audit['pipe_geometry'].items():
        samples = []
        for sample in series:
            values = {'time': sample['time'], 'signed_model_flow':
                      sample.get('links', {}).get(key, {}).get('signed_model_flow_Ls')}
            for side, node, invert in [('inlet', pipe['from_node'], pipe['inlet_invert']),
                                       ('outlet', pipe['to_node'], pipe['outlet_invert'])]:
                head = sample.get('nodes', {}).get(node, {}).get('head_m')
                values[side + '_head_minus_invert'] = head - invert if head is not None and invert is not None else None
                values[side + '_head_minus_crown'] = head - invert - pipe['height'] if head is not None and invert is not None and pipe['closed'] and pipe['height'] is not None else None
            samples.append(values)
        ranges = {}
        for field in ['signed_model_flow', 'inlet_head_minus_invert', 'outlet_head_minus_invert',
                      'inlet_head_minus_crown', 'outlet_head_minus_crown']:
            present = [s for s in samples if s[field] is not None]
            if present:
                low, high = min(present, key=lambda s: s[field]), max(present, key=lambda s: s[field])
                ranges[field] = {'min': low[field], 'min_time': low['time'], 'max': high[field],
                                 'max_time': high['time'], 'non_null_samples': len(present)}
            else:
                ranges[field] = None
        opposed = []
        for sample in series:
            link = sample.get('links', {}).get(key, {})
            flow, difference = link.get('signed_model_flow_Ls'), link.get('from_minus_to_head_m')
            if flow is not None and difference is not None and abs(flow) > 1e-6 and abs(difference) > .001 and flow * difference < 0:
                opposed.append({'time': sample['time'], 'signed_model_flow': flow, 'from_minus_to_head': difference})
        result[key] = {'ranges': ranges, 'flow_against_head_difference_samples': len(opposed),
                       'flow_against_head_difference_examples': opposed[:3],
                       'at_event_peak': next((s for s in samples if s['time'] == event['peak_time']), None)}
    return {'window_start': series[0]['time'] if series else None,
            'window_end': series[-1]['time'] if series else None,
            'window_basis': 'Provided saved local_process_series, including its context neighbors.',
            'length_unit': audit['length_unit'], 'flow_unit': 'L/s', 'links': result,
            'interpretation': 'Head above outlet crown establishes outlet submergence only. Neither head ordering nor positive flow alone excludes downstream influence. Flow opposed to the instantaneous head difference requires checking time history, inertia and numerical records; it is not automatically invalid or proof of downstream backwater.'}


def build_structure_audit(model, scope):
    links, nodes = model.get('links', {}), model.get('nodes', {})
    chosen = sorted(scope.get('link_roles', {}))
    selected_nodes = sorted(scope.get('node_roles', {}))
    options = model.get('options', {})
    units = 'm' if options.get('FLOW_UNITS', '').upper() in {'LPS', 'LPM', 'MLD', 'CMS', 'CMD'} else 'ft' if options.get('FLOW_UNITS', '').upper() in {'CFS', 'GPM', 'MGD'} else 'unknown'
    pipes, findings, unavailable = {}, [], []

    def finding(check_id, codes, **facts):
        if codes:
            findings.append({'check_id': check_id, 'features': codes, **facts})

    for key in chosen:
        link = links[key]
        if link['link_type'] != 'CONDUITS':
            facility = {'input_fields': link.get('input_fields', [])}
            if link['link_type'] == 'PUMPS' and link.get('input_fields'):
                curve_id = link['input_fields'][0]
                curve_rows = [p for p in model.get('sections', {}).get('CURVES', []) if p[0] == curve_id]
                curve_type = curve_rows[0][1].upper() if curve_rows and len(curve_rows[0]) >= 4 else None
                facility.update(curve_id=curve_id, curve_type=curve_type, curve_input_rows=curve_rows,
                    independent_variable='inlet_node_volume' if curve_type == 'PUMP1' else 'Consult the definition of the supplied curve type',
                    controls_input_rows=model.get('sections', {}).get('CONTROLS', []))
            finding('facility:' + key, ['facility_interface'], link_id=key,
                    link_type=link['link_type'], from_node=link['from_node'], to_node=link['to_node'], **facility)
            continue
        ends = []
        for node, field in ((link['from_node'], 'inlet_offset'), (link['to_node'], 'outlet_offset')):
            offset = number(link.get(field))
            base = number(nodes.get(node, {}).get('invert_elevation'))
            convention = link.get('offset_convention', options.get('LINK_OFFSETS', 'DEPTH')).upper()
            ends.append(offset if convention == 'ELEVATION' else base + offset if convention == 'DEPTH' and base is not None and offset is not None else None)
        length = number(link.get('length'))
        drop = ends[0] - ends[1] if None not in ends else None
        grade = drop / length if drop is not None and length is not None and length > 0 else None
        geometry = section_geometry(link)
        pipe = {'link_id': key, 'from_node': link['from_node'], 'to_node': link['to_node'],
                'inlet_invert': ends[0], 'outlet_invert': ends[1], 'input_length': length,
                'end_drop': drop, 'end_drop_over_input_length': grade,
                'roughness': number(link.get('roughness')), **geometry}
        pipes[key] = pipe
        codes = []
        if drop is not None:
            if abs(drop) <= EPS:
                codes.append('flat_input_inverts')
            elif drop < -EPS:
                codes.append('adverse_input_grade')
        if grade is None:
            unavailable.append({'check_id': 'pipe:' + key, 'check': 'input_grade', 'reason': 'Missing valid end elevations, offset convention or length'})
        if geometry['height'] is None or geometry['full_area_all_barrels'] is None:
            unavailable.append({'check_id': 'pipe:' + key, 'check': 'section_geometry', 'reason': 'Missing or unsupported section geometry; raw cross-section retained in local_structure'})
        finding('pipe:' + key, codes, **pipe)

    joints, node_connections = [], []
    for node in selected_nodes:
        all_in = sorted(k for k, v in links.items() if v['to_node'] == node)
        all_out = sorted(k for k, v in links.items() if v['from_node'] == node)
        visible_in = [k for k in all_in if k in chosen]
        visible_out = [k for k in all_out if k in chosen]
        node_record = nodes.get(node, {})
        invert, max_depth = number(node_record.get('invert_elevation')), number(node_record.get('max_depth'))
        node_connections.append({'node_id': node, 'node_type': node_record.get('node_type'),
            'visible_incoming_links': visible_in, 'visible_outgoing_links': visible_out,
            'outside_scope_incoming_links': [k for k in all_in if k not in chosen],
            'outside_scope_outgoing_links': [k for k in all_out if k not in chosen],
            'input_rim_elevation': invert + max_depth if invert is not None and max_depth is not None and max_depth > 0 else None,
            'max_depth': max_depth, 'surcharge_depth': node_record.get('surcharge_depth'),
            'ponded_area': node_record.get('ponded_area')})
        if node == scope.get('target_node'):
            codes = []
            if len(all_in) + len(all_out) == 1:
                codes.append('single_connection_target')
            if len(all_in) > 1:
                codes.append('multiple_nominal_inlets')
            if len(all_out) > 1:
                codes.append('multiple_nominal_outlets')
            finding('connection:' + node, codes, node_id=node, incoming_links=all_in, outgoing_links=all_out)
        for incoming in visible_in:
            for outgoing in visible_out:
                if incoming not in pipes or outgoing not in pipes:
                    continue
                a, b = pipes[incoming], pipes[outgoing]
                bottom = b['inlet_invert'] - a['outlet_invert'] if a['outlet_invert'] is not None and b['inlet_invert'] is not None else None
                crown = bottom + b['height'] - a['height'] if bottom is not None and a['closed'] and b['closed'] and a['height'] is not None and b['height'] is not None else None
                ratio = b['full_area_all_barrels'] / a['full_area_all_barrels'] if a['full_area_all_barrels'] and b['full_area_all_barrels'] is not None else None
                diameter_ratio = b['diameter'] / a['diameter'] if a['diameter'] and b['diameter'] is not None else None
                row = {'check_id': f'joint:{node}:{incoming}:{outgoing}', 'node_id': node,
                       'incoming_link_id': incoming, 'outgoing_link_id': outgoing,
                       'incoming_diameter': a['diameter'], 'outgoing_diameter': b['diameter'],
                       'outgoing_over_incoming_diameter': diameter_ratio,
                       'outgoing_over_incoming_full_area': ratio,
                       'outgoing_minus_incoming_invert': bottom,
                       'outgoing_minus_incoming_crown': crown,
                       'incoming_grade': a['end_drop_over_input_length'], 'outgoing_grade': b['end_drop_over_input_length'],
                       'same_invert': abs(bottom) <= EPS if bottom is not None else None,
                       'same_crown': abs(crown) <= EPS if crown is not None else None}
                codes = []
                if diameter_ratio is not None and diameter_ratio < 1 - EPS:
                    codes.append('downstream_diameter_decrease')
                if ratio is not None and ratio < 1 - EPS:
                    codes.append('downstream_full_area_decrease')
                if bottom is not None and bottom > EPS:
                    codes.append('outgoing_invert_raised')
                if bottom is not None and abs(bottom) <= EPS and a['height'] is not None and b['height'] is not None and abs(a['height'] - b['height']) > EPS:
                    codes.append('same_invert_unequal_section_heights')
                if a['end_drop_over_input_length'] is not None and b['end_drop_over_input_length'] is not None and a['end_drop_over_input_length'] > b['end_drop_over_input_length'] + EPS:
                    codes.append('downstream_grade_decrease')
                joints.append({**row, 'features': codes})
                finding(row['check_id'], codes, **{k: v for k, v in row.items() if k != 'check_id'})

    return {'version': AUDIT_VERSION, 'status': 'available' if model.get('available') else 'unavailable',
            'target_node': scope.get('target_node'), 'length_unit': units, 'area_unit': units + '2',
            'direction_basis': 'input from_node/to_node, not event flow direction',
            'scope_policy': scope.get('policy'), 'scope_status': scope.get('status'),
            'pipe_geometry': pipes, 'junction_comparisons': joints, 'node_connections': node_connections,
            'findings': findings, 'unavailable_checks': unavailable,
            'solver_settings': {k: options.get(k) for k in ('FLOW_ROUTING', 'MIN_SLOPE', 'LINK_OFFSETS', 'ALLOW_PONDING')},
            'interpretation': {
                'findings': 'Descriptive screening features; hydraulic relevance must be assessed with event processes.',
                'grade': 'End invert drop / input conduit length; solver-adjusted slope is not calculated.',
                'alignment': 'Invert/crown alignment is geometric. Design-water-surface alignment requires discharge and water depth.',
                'area': 'Full-section geometric area including barrels, not a hydraulic capacity ratio.',
                'node_levels': 'Input rim level only when max_depth is explicit; not a universal effective spill threshold.',
                'scope': 'Outside-scope connections are an inventory only; their geometry/processes are not audited.',
                'equality_tolerance': EPS}}
