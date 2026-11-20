"""Compile only visual semantics; retain every fact, condition and unknown."""
import copy
import re
from .operation_store import canonical,digest
from .source_contract import validate_facts

CONTRACT='visual-request-0372'

_WORKFLOW_NOTE = re.compile(
    r'^(?:本轮仅形成|本规划(?:不构成|仅)|不启动(?:图像生成|自动重试)|'
    r'已阅读唯一来源|依据中国专利法|遵循中国专利法|数字标号及引出线须待)'
)


def _drawing_notes(rows):
 return [clause for row in rows for sentence in re.split(r'(?<=[。！？])|\n', row)
         if (clause := sentence.strip()) and not _WORKFLOW_NOTE.match(clause)]


def expression_view_instructions(expression, view_id, view):
 requirement = next(row for row in expression['view_requirements'] if row['view_id'] == view_id)
 allowed = set(requirement['fact_ids'])
 relations = [{k: v for k, v in row.items() if k not in {'evidence', 'certainty'}}
              for row in expression['facts'] if row['id'] in allowed and row['certainty'] == 'confirmed']
 unknowns = [row['reason'] for row in expression['unknowns'] if view_id in row['view_ids']]
 scene = view.get('expression_scene', {})
 context = ' '.join([view.get('title', ''), view.get('purpose', ''),
                     *(str(row.get('role', '')) for row in scene.get('instances', []))])
 guidance = ['当前视图是无比例技术示意。按已核对的空间参考表达已知关系，省略线只表示绘图省略。']
 if 'weld_opening' in context or 'partition_wall' in context or 'plated_hole' in context:
  guidance.append('设备用途是屏蔽电缆到印制电路板（PCB）的接地装配。'
                  '仅画本视图有依据的通道、壁、导线、安装销、镀覆孔或开口。')
 guidance.extend(['本图已确认的关系：' + canonical(relations),
                  '本图不得补定的范围：' + canonical(unknowns)])
 return '\n'.join(guidance)


def compile_visual_request(bundle:dict,plan:dict,view:dict,geometry:dict|None,*,base_instruction:str|None=None)->dict:
 facts=plan.get('facts',[])
 if any(x['verdict']=='FAIL'for x in validate_facts(bundle,facts)):raise ValueError('visual fact coverage invalid')
 if geometry is not None:
  from .source_geometry import validate_source_geometry
  if validate_source_geometry(geometry,bundle,plan):raise ValueError('visual source geometry invalid')
 part_ids=set(view.get('parts',[]));parts=[p for p in plan.get('parts',[])if p['id']in part_ids]
 # Keep global constraints and unknowns. A typed fact list limits the view only
 # when the plan explicitly names the applicable facts.
 required=set(view.get('required_facts') or [])
 scoped=[f for f in facts if not required or f['id'] in required]
 view_payload={k:view[k]for k in ['id','title','purpose','instruction','requested_type','delivered_type','required_facts']if k in view}
 for key in ('purpose','instruction'):
  if key in view_payload:view_payload[key]=' '.join(_drawing_notes([view_payload[key]]))
 if base_instruction is not None:
  view_payload['instruction']=base_instruction
 payload={'contract':CONTRACT,'title':plan.get('title',''),'source_sha256':plan['source_sha256'],
  'view':view_payload,
  'parts':parts,'facts':[{k:f[k] for k in ('id','status','text')} for f in scoped],
  'constraints':_drawing_notes(plan.get('constraints',[])),'unresolved':plan.get('unresolved',[]),
  'source_geometry':geometry,'relations':plan.get('scene',{}).get('relations',[])}
 text='仅画专利技术线稿底图；不画编号或引出线，不补造未知结构。保留来源外包络及内外属性。\n'+canonical(payload)
 return {'prompt':text,'fact_coverage':[f['id']for f in scoped], 'unknown_coverage':copy.deepcopy(plan.get('unresolved',[])),
  'request_context_sha':digest({'visual':payload,'source_facts':facts,'constraints':plan.get('constraints',[])}),
  'contract':CONTRACT}
