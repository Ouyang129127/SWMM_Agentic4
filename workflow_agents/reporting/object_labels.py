"""Keep human-facing object mentions explicit without changing source IDs."""
import re


def labels_from_rows(rows):
    labels, ambiguous = {}, set()
    for row in rows:
        if row.get('metric_name') != 'local_structure':
            continue
        structure = row.get('value', {})
        for key in structure.get('nodes', {}):
            if key in labels and labels[key] != '节点':
                ambiguous.add(key)
            labels[key] = '节点'
        for key, link in structure.get('links', {}).items():
            label = {'CONDUITS': '管段', 'PUMPS': '泵', 'ORIFICES': '孔口',
                     'WEIRS': '堰', 'OUTLETS': '出流设施'}.get(link.get('link_type'), '连接设施')
            if key in labels and labels[key] != label:
                ambiguous.add(key)
            labels[key] = label
    return {key: value for key, value in labels.items() if key not in ambiguous and not key.isdigit()}


def label_report(text, labels):
    if not labels:
        return text
    pattern = re.compile(r'(?<![A-Za-z0-9_])(' + '|'.join(re.escape(k) for k in sorted(labels, key=len, reverse=True)) + r')(?![A-Za-z0-9_]|:E\d)')
    def replace(match):
        key = match.group(0)
        prefix = text[max(0, match.start() - 8):match.start()]
        if any(prefix.endswith(word) for word in ('节点', '管段', '泵', '孔口', '堰', '出流设施', '连接设施', '管道', '子汇水区')):
            return key
        return labels[key] + key
    return pattern.sub(replace, text)
