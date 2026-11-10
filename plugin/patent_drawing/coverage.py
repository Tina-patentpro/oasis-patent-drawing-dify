"""Source existence, drawable geometry and visual proof are distinct states."""
import re
from .source_contract import validate_facts

_UNKNOWN=re.compile(r'待确认|未确定|不确定|未给出|未知|缺少|未规定')
_PHYSICAL=re.compile(r'弹簧|安装销|通道|配合|导电连接|补偿连接|PCB|托板|剖视|接地孔')

def coverage_requirements(bundle,plan):
    rows=[]
    for fact in plan.get('facts',[]):
        text=fact.get('text','');refs=fact.get('evidence',[])
        valid=fact.get('status')=='confirmed'and not any(x['verdict']=='FAIL'for x in validate_facts(bundle,[fact]))
        state='drawable_candidate'if valid else'conflict'if fact.get('status')=='conflict'else'not_confirmed'
        if valid and _UNKNOWN.search(text+' '+' '.join(e.get('quote','')for e in refs)):state='exists_geometry_unknown'
        required=['spatial','structure']if _PHYSICAL.search(text)else['flowchart']if set(plan.get('technical_types',[]))&{'computer_method','communication'}else['source_review']
        covered=[v['id']for v in plan.get('views',[])if fact['id']in v.get('required_facts',[])]
        rows.append({'fact_id':fact['id'],'source_refs':refs,'evidence_state':state,'required_views':required,
            'covered_views':covered,'coverage_proof':'declared_only','visual_verdict':'UNKNOWN',
            'missing_input':'需明确相关接口形式、位置、隐藏几何或原始依据，不能由存在推断可绘。'if state!='drawable_candidate'else'需核对实际图件可见性及图文对应。'if not covered else'实际输出尚待视觉与专业核对。'})
    return rows


def change_impact(old,new):
    prior={p['id']:p for p in old.get('parts',[])};out=[]
    for p in new.get('parts',[]):
        a=prior.get(p['id'])
        if a and (a.get('label')!=p.get('label')or a.get('name')!=p.get('name')):
            out.append({'part_id':p['id'],'old_label':a.get('label'),'new_label':p.get('label'),'old_name':a.get('name'),'new_name':p.get('name'),
                'affected_views':[v['id']for v in new.get('views',[])if p['id']in v.get('parts',[])],
                'specification_sync':'UNKNOWN','source_modified':False,'followup':'同步检查正文、附图说明、标记表与图号引用；原交底不自动改写。'})
    return out
