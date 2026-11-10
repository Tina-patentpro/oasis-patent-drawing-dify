"""A set-wide display-mark edit is committed against one immutable baseline."""
import copy
from .operation_store import digest


def plan_label_change(plan: dict, records: dict, part_id: str, new_label: str) -> dict:
    if plan.get('part_mark_mode') == 'name_only': raise ValueError('review numbering proposal before renumbering name_only')
    parts = {p['id']: p for p in plan['parts']}
    if part_id not in parts or not isinstance(new_label, str) or not new_label.strip(): raise ValueError('invalid label edit')
    if any(p['label'] == new_label and p['id'] != part_id for p in parts.values()): raise ValueError('duplicate label')
    affected = {v['id'] for v in plan['views'] if part_id in v['parts']}
    while True:
        expanded = affected | {v['id'] for v in plan['views'] if set(v.get('depends_on', [])) & affected
            and not (v['engine']=='image' and v.get('reference_selection')=='guide_and_parent'
                     and part_id not in v['parts'])}
        if affected == expanded: break
        affected = expanded
    ordered = [v['id'] for v in plan['views'] if v['id'] in affected]
    required = [{'view_id': v['id'], 'kind': 'source_roi' if v['engine'] == 'source' else 'image_anchor',
                 'base_sha256': records[v['id']]['base']['sha256']} for v in plan['views']
                if v['id'] in affected and part_id in v['parts'] and v['engine'] in {'source', 'image'}]
    tx = {'source_revision': plan['revision'], 'target_revision': plan['revision'] + 1,
          'source_plan_sha256': digest(plan), 'part_id': part_id, 'old_label': parts[part_id]['label'],
          'new_label': new_label, 'affected_views': ordered,
          'expected_unchanged': [v['id'] for v in plan['views'] if v['id'] not in affected],
          'required_approvals': required, 'state': 'pending',
          'baseline_records': {fid: {'base': r['base']['sha256'], 'png': r['png']['sha256']} for fid, r in records.items()},
          'specification_sync': 'UNKNOWN; original specification is not modified'}
    tx['id'] = digest(tx)
    return tx


def validate_edit_completion(transaction: dict, records: dict, checks: list[dict]) -> list[dict]:
    issues = []
    def fail(message): issues.append({'rule': 'LINKED-EDIT-TRANSACTION', 'verdict': 'FAIL', 'message': message})
    for fid, old in transaction['baseline_records'].items():
        now = records.get(fid)
        if now is None: fail('missing figure ' + fid); continue
        if now['base']['sha256'] != old['base']: fail('label edit changed geometry base ' + fid)
        if fid in transaction['expected_unchanged'] and now['png']['sha256'] != old['png']:
            fail('unaffected figure changed ' + fid)
        if fid in transaction['affected_views'] and now['png']['sha256'] != old['png'] and now.get('annotation_outside_unchanged') is not True:
            fail('pixels outside reviewed display regions not verified ' + fid)
    by_view = {x['view_id']: x for x in checks}
    for requirement in transaction['required_approvals']:
        check = by_view.get(requirement['view_id'], {})
        if check.get('approval_bound') is not True or check.get('base_sha256') != requirement['base_sha256']:
            fail('actual-base approval missing ' + requirement['view_id'])
    return issues


def audit_active_references(plan:dict,records:dict,texts:dict)->dict:
    """Inspect current references only. Missing actual text is UNKNOWN, not absence."""
    import re
    parts={p['id']:p for p in plan['parts']};findings=[];active=[]
    for view in plan['views']:
        fid=view['id'];record=records.get(fid)
        if record is None:findings.append({'view_id':fid,'verdict':'FAIL','reason':'current_figure_missing'});continue
        for a in record.get('annotations',[]):
            part=parts.get(a.get('part_id'))
            if part is None or a.get('label')!=part['label']:findings.append({'view_id':fid,'verdict':'FAIL','reason':'active_annotation_conflict'})
        required=set(view['parts']) if 'engine' not in view else set()
        required.update(a.get('part_id') for a in record.get('annotations',[]))
        diagram=view.get('diagram',{})
        values=[n.get('text','') for n in diagram.get('nodes',[])]+[p.get('text','') for p in diagram.get('primitives',[])]+[e.get('label','') for e in diagram.get('edges',[])]
        for value in values:required.update(re.findall(r'\{\{([A-Za-z0-9_-]+)\.(?:name|label)\}\}',value))
        required.update(x.get('part') for x in diagram.get('primitives',[]) if x.get('type')=='label')
        for pid in view['parts']:
            p=parts[pid];token=p['name'] if plan.get('part_mark_mode')=='name_only' else p['label']
            active.append({'view_id':fid,'part_id':pid,'name':p['name'],'label':p['label']})
            if pid not in required:continue
            if fid not in texts:findings.append({'view_id':fid,'part_id':pid,'verdict':'UNKNOWN','reason':'actual_visible_text_not_supplied'})
            elif token and not re.search(r'(?<![A-Za-z0-9])'+re.escape(token)+r'(?![A-Za-z0-9])',re.sub(r'\s+','',texts[fid]) if plan.get('part_mark_mode')=='name_only' else texts[fid]):
                findings.append({'view_id':fid,'part_id':pid,'verdict':'UNKNOWN' if p['name'] in re.sub(r'\s+','',texts[fid]) else 'FAIL','reason':'numeric_reference_not_verified' if p['name'] in re.sub(r'\s+','',texts[fid]) else 'active_reference_missing'})
    # Active descriptions/purposes are delivery context, unlike source quotes or history.
    valid_labels={p['label'] for p in plan['parts'] if p['label']}
    prefixes=['部件','标号']+[p['name'] for p in plan['parts']]
    pattern=r'(?:'+'|'.join(re.escape(x) for x in prefixes)+r')\s*(\d+[A-Za-z]?)(?![A-Za-z0-9])'
    contexts={**{v['id']+':'+f:v.get(f,'') for v in plan['views'] for f in ['title','purpose','instruction']},**texts}
    for location,value in contexts.items():
        for label in re.findall(pattern,value):
            if label not in valid_labels:findings.append({'location':location,'label':label,'verdict':'FAIL','reason':'stale_active_display_reference'})
    status='FAIL' if any(f['verdict']=='FAIL' for f in findings) else 'UNKNOWN' if findings else 'PASS'
    return {'status':status,'active_references':active,'findings':findings,'specification_sync':'UNKNOWN','scope':'current_only'}


def _display_pattern(part):
    import re
    return r'(?P<prefix>(?:部件|标号|'+re.escape(part['name'])+r')\s*)'+re.escape(part['label'])+r'(?![A-Za-z0-9])'


def _numbered_context(value,parts,replacements=None,stable=False):
    import re
    replacements=replacements or {};by_label={p['label']:p['id'] for p in parts if p['label']}
    seq=r'\d+[A-Za-z]?(?:\s*(?:至|到|-|—|、|，|,)\s*\d+[A-Za-z]?)*'
    def convert(match):
        text=match['sequence'];tokens=[]
        for item in re.split(r'[、，,]',text):
            pair=re.fullmatch(r'\s*(\d+)\s*(?:至|到|-|—)\s*(\d+)\s*',item)
            if pair:
                a,b=map(int,pair.groups())
                if not 0<=b-a<=120:return match[0]
                tokens.extend(str(i)for i in range(a,b+1))
            else:tokens.append(item.strip())
        if not stable and not any(t in replacements for t in tokens):return match[0]
        result='、'.join(('{{display:'+by_label[t]+'}}') if stable and t in by_label else replacements.get(t,t) for t in tokens)
        return (match.groupdict().get('head') or '')+result+(match.groupdict().get('tail') or '')
    value=re.sub(r'(?P<head>(?:编号|标号|部件|标记)\s*)(?P<sequence>'+seq+r')(?![A-Za-z0-9])',convert,value)
    return re.sub(r'(?P<sequence>'+seq+r')(?P<tail>\s*的(?:标记|编号|标号|部件))',convert,value)


def sync_display_references(previous,plan):
    import re
    result=copy.deepcopy(plan);updates=[];old={p['id']:p for p in previous['parts']}
    replacements={old[p['id']]['label']:p['label'] for p in plan['parts'] if p['id'] in old and old[p['id']]['label'] and old[p['id']]['label']!=p['label']}
    for view in result['views']:
        for field in ['title','purpose','instruction']:
            value=view.get(field)
            if isinstance(value,str):
                changed=_numbered_context(value,previous['parts'],replacements)
                if changed!=value:updates.append({'view_id':view['id'],'field':field,'before':value,'after':changed});view[field]=changed
    for part in plan['parts']:
        before=old.get(part['id'])
        if not before or before['label']==part['label'] or not before['label']:continue
        for view in result['views']:
            for field in ['title','purpose','instruction']:
                value=view.get(field)
                if not isinstance(value,str):continue
                changed=re.sub(_display_pattern(before),lambda m:m['prefix']+part['label'],value)
                if changed!=value:
                    updates.append({'view_id':view['id'],'field':field,'part_id':part['id'],'before':value,'after':changed})
                    view[field]=changed
    return result,updates


def stable_display_context(value,parts):
    import re
    value=_numbered_context(value,parts,stable=True)
    for part in parts:
        if part.get('label'):value=re.sub(_display_pattern(part),lambda m:'{{display:'+part['id']+'}}',value)
    return value
