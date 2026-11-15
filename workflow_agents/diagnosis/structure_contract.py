"""Coverage and reference checks for event-local structure review, not causation."""
from workflow_agents.evidence.structure_audit import AUDIT_VERSION


def structure_review_requirements(rows):
    """A reference index of supplied records, not new hydraulic evidence."""
    return [{'event_id': row['event_id'], 'node_id': row['object_id'],
             'structure_evidence_id': row['evidence_id'],
             'finding_check_ids': [f['check_id'] for f in row['value']['structure_audit'].get('findings', [])],
             'visible_same_event_evidence_ids': [r['evidence_id'] for r in rows if r.get('event_id') == row['event_id']]}
            for row in rows if row.get('metric_name') == 'local_structure'
            and row.get('value', {}).get('structure_audit', {}).get('version') == AUDIT_VERSION]


def validate_structure_assessments(result, rows):
    assessments = {e.get('event_id'): e for e in result.get('event_assessments', [])}
    for row in rows:
        if row.get('metric_name') != 'local_structure':
            continue
        audit = row.get('value', {}).get('structure_audit', {})
        if audit.get('version') != AUDIT_VERSION:
            continue
        event_id = row.get('event_id')
        item = assessments.get(event_id)
        # Follow-up tasks without event_assessments retain the original contract.
        if item is None:
            continue
        review = item.get('structural_assessment')
        if not isinstance(review, dict) or review.get('status') not in {'reviewed', 'needs_evidence', 'unavailable'}:
            raise ValueError(f'Structure review required: {event_id}')
        if not isinstance(review.get('summary'), str) or not review['summary'].strip():
            raise ValueError(f'Structure review summary required: {event_id}')
        refs = review.get('evidence_ids', [])
        if not isinstance(refs, list) or row['evidence_id'] not in refs:
            raise ValueError(f'Structure review must cite its local_structure: {event_id}')
        expected = {f['check_id'] for f in audit.get('findings', [])}
        entries = review.get('finding_assessments')
        if (not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries)
                or len(entries) != len(expected) or {e.get('check_id') for e in entries} != expected):
            raise ValueError(f'Structure review must respond to every finding once: {event_id}; expected={sorted(expected)}')
        for entry in entries:
            if entry.get('status') not in {'relevant', 'not_relevant', 'needs_evidence'} or not isinstance(entry.get('reason'), str) or not entry['reason'].strip():
                raise ValueError(f'Invalid structure finding assessment: {event_id}')
            if not isinstance(entry.get('evidence_ids'), list) or row['evidence_id'] not in entry['evidence_ids']:
                raise ValueError(f'Structure finding must cite its structure evidence: {event_id}')
        available = {r['evidence_id']: r for r in rows}
        for refs in [review['evidence_ids']] + [e['evidence_ids'] for e in entries]:
            bad = [{ 'evidence_id': ref, 'bound_event': available.get(ref, {}).get('event_id', 'not_visible')}
                   for ref in refs if ref not in available or available[ref].get('event_id') not in {'', None, event_id}]
            if bad:
                raise ValueError(f'Structure review cites unavailable/another event: {event_id}; invalid={bad}; own_structure={row["evidence_id"]}')
