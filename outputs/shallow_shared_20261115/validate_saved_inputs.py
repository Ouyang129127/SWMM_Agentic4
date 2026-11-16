"""Read-only view/token comparison on saved inputs, without model calls."""
import copy
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
from workflow_agents.diagnosis.prompts import SYSTEM_PROMPT, SHARED_VIEW_INSTRUCTIONS
from workflow_agents.diagnosis.series_transport import process_evidence_view
from workflow_agents.diagnosis.shared_transport import build_evidence_view, restore_shared_evidence
from workflow_agents.diagnosis.service import diagnosis_system_prompt, serialize_prompt

os.environ['TIKTOKEN_CACHE_DIR'] = str(Path.home()/'.cache/babeldoc/tiktoken')
import tiktoken
ENCODERS = {name:tiktoken.get_encoding(name) for name in ('cl100k_base','o200k_base')}


def measure(prompt, label, system=SYSTEM_PROMPT):
    frozen = serialize_prompt(prompt)
    before = copy.deepcopy(prompt)
    before['visible_evidence'] = process_evidence_view(prompt['visible_evidence'])
    after = copy.deepcopy(before)
    after.update(build_evidence_view(prompt['visible_evidence']))
    restored = restore_shared_evidence(json.loads(serialize_prompt(after)))
    assert json.dumps(restored,sort_keys=True)==json.dumps(before['visible_evidence'],sort_keys=True)
    assert serialize_prompt(prompt)==frozen
    shared = 'evidence_view_format' in after
    system_after = system + (SHARED_VIEW_INSTRUCTIONS if shared else '')
    before_text,after_text=serialize_prompt(before),serialize_prompt(after)
    assert len(system_after)+len(after_text)<=len(system)+len(before_text)
    tokens={}
    for name,encoder in ENCODERS.items():
        count=lambda s:len(encoder.encode(s,disallowed_special=()))
        a,b=count(system)+count(before_text),count(system_after)+count(after_text)
        tokens[name]={'before':a,'after':b,'reduction_pct':round(100*(a-b)/a,3)}
    return {'label':label,'sharing_enabled':shared,'shared_definitions':len(after.get('shared_static',{})),
            'before_characters':len(system)+len(before_text),'after_characters':len(system_after)+len(after_text),
            'tokenizers':tokens,'sharing_roundtrip_equal':True,'input_unchanged':True}


def main():
    # Historical reconstruction helper only reads Agentic3 source data and
    # frozen batch functions. Its collect() method makes no model calls.
    helper=PROJECT.parent/'outputs/token_reduction_estimate_20261115/analyze.py'
    spec=importlib.util.spec_from_file_location('historical_view_inputs',helper)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    calls,runs,hashes=module.collect()
    rows=[]
    for i,call in enumerate(calls):
        rows.append(measure(call['prompt'],call['audit_file'],call['system']))
        if i%20==0:print('historical calls checked',i+1,flush=True)
    current=[]
    for path in sorted((PROJECT/'outputs/m6_antecedent_window_20261115').glob('*_prompt.json')):
        raw=path.read_bytes()
        current.append(measure(json.loads(raw.decode('utf-8-sig')),str(path)))
        assert path.read_bytes()==raw
    aggregate={}
    for name in ENCODERS:
        a=sum(r['tokenizers'][name]['before'] for r in rows)
        b=sum(r['tokenizers'][name]['after'] for r in rows)
        aggregate[name]={'before':a,'after':b,'additional_reduction_vs_series_only_pct':round(100*(a-b)/a,3)}
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in hashes.items())
    result={'kind':'offline_proxy_tokenization_not_model_or_provider_test',
            'comparison':'First-class tables versus first-class tables plus the implemented shallow sharing; extra shared instructions included only when sharing is active.',
            'historical_runs':len(runs),'historical_calls':len(rows),
            'historical_system_note':'Frozen historical system text held fixed between paired views, with only shared-format instructions added to the shared view.',
            'historical_sharing_calls':sum(r['sharing_enabled'] for r in rows),'aggregate':aggregate,
            'current_m6_previews':current,'calls':rows,'all_roundtrips_equal':True,'source_hashes_unchanged':True,
            'source_hashes':hashes}
    path=Path(__file__).with_name('verification.json')
    path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:result[k] for k in ('historical_calls','historical_sharing_calls','aggregate','current_m6_previews','all_roundtrips_equal','source_hashes_unchanged')},ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()
