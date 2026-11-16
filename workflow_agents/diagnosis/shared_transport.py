"""One-hop sharing of exact static definitions in an outgoing diagnosis view.

The original evidence is never rewritten. Context inheritance is restricted to
model/run, and static references replace only known input definitions under an
event's own object map. Event scope, provenance and process evidence stay local.
"""
import copy
import json
from collections import defaultdict

from workflow_agents.diagnosis.series_transport import process_evidence_view
from workflow_agents.diagnosis.prompts import SHARED_VIEW_INSTRUCTIONS


VIEW_FORMAT = 'shallow_shared_v1'
CONTEXT_FIELDS = ('source_model', 'run_id')
NODE_TYPES = frozenset({'JUNCTIONS', 'OUTFALLS', 'STORAGE', 'DIVIDERS'})
LINK_TYPES = frozenset({'CONDUITS', 'PUMPS', 'ORIFICES', 'WEIRS', 'OUTLETS'})
NODE_FIELDS = frozenset({
    'node_type', 'invert_elevation', 'input_fields', 'boundary_type',
    'max_depth', 'initial_depth', 'surcharge_depth', 'ponded_area',
})
LINK_FIELDS = frozenset({
    'link_type', 'from_node', 'to_node', 'input_fields', 'cross_section_fields',
    'offset_convention', 'length', 'roughness', 'inlet_offset', 'outlet_offset', 'cross_section',
})


def _json(value, *, canonical=False):
    # JSON distinguishes bool/int/float, negative zero, strings and null. Plain
    # Python dictionary equality would incorrectly equate some of these values.
    return json.dumps(value, ensure_ascii=False, sort_keys=canonical,
                      separators=(',', ':'), allow_nan=False)


def _has_reference(value):
    if isinstance(value, dict):
        return '$ref' in value or any(_has_reference(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_reference(v) for v in value)
    return False


def _is_definition(kind, value):
    if not isinstance(value, dict) or not value or _has_reference(value):
        return False
    if kind == 'node':
        return (isinstance(value.get('node_type'), str)
                and value['node_type'] in NODE_TYPES and set(value) <= NODE_FIELDS)
    return (isinstance(value.get('link_type'), str) and value['link_type'] in LINK_TYPES
            and all(isinstance(value.get(k), str) and value[k] for k in ('from_node', 'to_node'))
            and set(value) <= LINK_FIELDS)


def _object_slots(records):
    """Yield declared object maps only; never walk arbitrary nested evidence."""
    for record in records:
        value = record.get('value')
        if not isinstance(value, dict):
            continue
        if record.get('metric_name') == 'local_structure':
            groups = (('nodes', 'node'), ('links', 'link'))
        elif record.get('metric_name') == 'facilities_and_storage':
            groups = (('local_facilities', 'link'),)
        else:
            groups = ()
        for field, kind in groups:
            objects = value.get(field)
            if isinstance(objects, dict):
                for object_id, definition in objects.items():
                    yield kind, objects, object_id, definition


def build_evidence_view(records):
    """Apply series tables, then shallow sharing if the entire view is smaller.

    Shared definitions must match object kind, object ID and all JSON values.
    Same-ID variants stay distinct. One request must have one explicit model/run
    binding before sharing can be applied. Single records remain self-contained.
    """
    rows = process_evidence_view(records)
    view = {'visible_evidence': rows}
    if len(rows) < 2:
        return view
    bindings = {(r.get('source_model'), r.get('run_id')) for r in rows}
    if len(bindings) != 1:
        raise ValueError('Visible evidence mixes model/run bindings')
    binding = next(iter(bindings))
    if not all(isinstance(v, str) and v for v in binding):
        return view
    original_size = len(_json(view))
    originals = []
    defaults = dict(zip(CONTEXT_FIELDS, binding))
    for row in rows:
        for field in CONTEXT_FIELDS:
            originals.append((row, field, row.pop(field)))

    groups = defaultdict(list)
    for kind, objects, object_id, definition in _object_slots(rows):
        if _is_definition(kind, definition):
            groups[(kind, object_id, _json(definition, canonical=True))].append((objects, object_id, definition))
    shared = {}
    evidence_ids = {r.get('evidence_id') for r in rows}
    next_ref = 1
    for locations in groups.values():
        if len(locations) < 2:
            continue
        while f'S{next_ref}' in evidence_ids:
            next_ref += 1
        ref = f'S{next_ref}'
        definition = locations[0][2]
        replacement = {'$ref': ref}
        old_size = sum(len(_json(value)) for _, _, value in locations)
        # Include a conservative per-entry allowance for the pool wrapper. The
        # final whole-view comparison also counts format/defaults overhead.
        new_size = len(_json({ref: definition})) + len(locations) * len(_json(replacement)) + 20
        if new_size >= old_size:
            continue
        shared[ref] = definition
        next_ref += 1
        for parent, key, original in locations:
            originals.append((parent, key, original))
            parent[key] = dict(replacement)

    view['evidence_view_format'] = VIEW_FORMAT
    view['record_defaults'] = defaults
    if shared:
        view['shared_static'] = shared
    if len(_json(view)) + len(SHARED_VIEW_INSTRUCTIONS) >= original_size:
        for parent, key, original in originals:
            parent[key] = original
        return {'visible_evidence': rows}
    return view


def restore_shared_evidence(view):
    """Restore sharing for inspection; existing process tables stay as tables.

    Response validation continues to use frozen original evidence, not this
    decoder. Restored records are independent copies, including shared objects.
    """
    rows = copy.deepcopy(view['visible_evidence'])
    if 'evidence_view_format' not in view:
        return rows
    if view['evidence_view_format'] != VIEW_FORMAT:
        raise ValueError('Unsupported evidence view format')
    defaults = view.get('record_defaults', {})
    if not isinstance(defaults, dict) or set(defaults) != set(CONTEXT_FIELDS):
        raise ValueError('Shared evidence requires explicit model/run defaults')
    for row in rows:
        for key, value in defaults.items():
            row.setdefault(key, value)
    pool = view.get('shared_static', {})
    for kind, parent, key, value in _object_slots(rows):
        if not isinstance(value, dict) or set(value) != {'$ref'}:
            continue
        ref = value['$ref']
        if not isinstance(ref, str) or ref not in pool or not _is_definition(kind, pool[ref]):
            raise ValueError(f'Invalid static definition reference: {ref}')
        parent[key] = copy.deepcopy(pool[ref])
    return rows
