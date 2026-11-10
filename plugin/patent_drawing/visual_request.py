"""Compile only visual semantics; retain every fact, condition and unknown."""
import copy
from .operation_store import canonical,digest
from .source_contract import validate_facts

CONTRACT='visual-request-036-r1'


def compile_visual_request(bundle:dict,plan:dict,view:dict,geometry:dict|None)->dict:
 facts=plan.get('facts',[])
 if any(x['verdict']=='FAIL'for x in validate_facts(bundle,facts)):raise ValueError('visual fact coverage invalid')
 if geometry is not None:
  from .source_geometry import validate_source_geometry
  if validate_source_geometry(geometry,bundle,plan):raise ValueError('visual source geometry invalid')
 part_ids=set(view.get('parts',[]));parts=[p for p in plan.get('parts',[])if p['id']in part_ids]
 # No heuristic fact dropping: legacy facts lack typed part scope. Full source
 # pages/audit/legal metadata are omitted; all explicit constraints stay.
 payload={'contract':CONTRACT,'title':plan.get('title',''),'source_sha256':plan['source_sha256'],
  'view':{k:view[k]for k in ['id','title','purpose','instruction','requested_type','delivered_type','required_facts']if k in view},
  'parts':parts,'facts':facts,'constraints':plan.get('constraints',[]),'unresolved':plan.get('unresolved',[]),
  'source_geometry':geometry,'relations':plan.get('scene',{}).get('relations',[])}
 text='仅画专利技术线稿底图；不画编号或引出线，不补造未知结构。保留来源外包络及内外属性。\n'+canonical(payload)
 return {'prompt':text,'fact_coverage':[f['id']for f in facts], 'unknown_coverage':copy.deepcopy(plan.get('unresolved',[])),
  'request_context_sha':digest(payload),'contract':CONTRACT}
