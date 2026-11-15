"""Lossless table views of process samples, only for outgoing LLM evidence.

Frozen evidence and evidence IDs keep their original representation. Columns
are arrays of exact dictionary keys, so object IDs containing dots or slashes
are unambiguous. Irregular samples stay as records: absent fields are never
filled with nulls or zeroes to make a rectangular table.
"""
import copy
import json


TABLE_FORMAT = 'path_table_v1'
PROCESS_METRICS = frozenset({
    'local_process_series', 'fullness_time_series', 'max_fullness',
    'storage_volume_process', 'ponding_depth_time_series',
})


def _leaves(value, path=()):
    if isinstance(value, dict) and value:
        for key, child in value.items():
            yield from _leaves(child, path + (key,))
    else:
        # Empty dictionaries and lists are values, not missing fields.
        yield path, value


def _size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False))


def compact_series(series):
    """Return a table only for a uniform record sequence that becomes smaller.

    No timestamps, values, precision, sample order or samples are changed.
    This function reads its input; the caller owns any returned cell values.
    """
    if not isinstance(series, list) or not series or any(not isinstance(s, dict) or not s for s in series):
        return series
    first = dict(_leaves(series[0]))
    columns = list(first)
    rows = [list(first.values())]
    for sample in series[1:]:
        cells = dict(_leaves(sample))
        if cells.keys() != first.keys():
            return series
        rows.append([cells[path] for path in columns])
    table = {'format': TABLE_FORMAT, 'columns': [list(path) for path in columns], 'rows': rows}
    return table if _size(table) < _size(series) else series


def process_evidence_view(records):
    """Copy complete evidence records and compact only known process series."""
    visible = copy.deepcopy(records)
    for record in visible:
        value = record.get('value')
        if record.get('metric_name') in PROCESS_METRICS and isinstance(value, dict) and 'series' in value:
            value['series'] = compact_series(value['series'])
    return visible
