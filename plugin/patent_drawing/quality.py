"""Quality dimensions remain separate from artifact and schema success."""
from .cn_policy import check_policy
from .scene import validate_scene
from .source_contract import validate_facts, validate_view_plan


def check_set(bundle, plan, records):
    findings = validate_facts(bundle, plan.get('facts', []))
    findings += validate_view_plan(plan.get('facts', []), plan.get('scene', {}).get('instances', []), plan['views'])
    from .flow_fidelity import check_logic
    findings += check_logic(bundle,plan)
    findings += check_policy(plan)
    from .spatial_brief import requirements
    for v in plan['views']:
        if v['engine']=='image':
            brief=requirements(bundle,plan,v)
            findings.append({'rule':'SPATIAL-VISIBILITY','verdict':'UNKNOWN','message':brief['limitations'],'view_id':v['id'],'requirements':brief['features']})
    geometry = validate_scene(plan['scene']) if plan.get('scene') else []
    findings += geometry
    findings.append({'rule': 'SOURCE-SEMANTICS', 'verdict': 'UNKNOWN',
        'message': '原文位置/文件身份检查不等于图像或逻辑技术内容正确；真实图文、跨图和专业审阅仍需取得。'})
    for v in plan['views']:
        if v['id'] not in records:
            findings.append({'rule': 'SET-COMPLETE', 'verdict': 'FAIL', 'message': v['id'] + '尚无当前规格图件'})
        elif v['engine'] in {'source', 'image'}:
            findings.append({'rule': 'FIGURE-VISUAL', 'verdict': 'UNKNOWN', 'message': v['id'] + '实际图形关系及内嵌标记需审阅'})
    from .coverage import coverage_requirements
    return {'coverage':coverage_requirements(bundle,plan),'findings': findings, 'file_identity': 'PASS' if len(records) == len(plan['views']) else 'FAIL',
        'geometry_constraints': 'FAIL' if any(x['verdict'] == 'FAIL' for x in geometry) else 'UNKNOWN' if any(x['verdict']=='UNKNOWN'for x in geometry) or not plan.get('scene',{}).get('relations') else 'PASS',
        'source_fidelity': 'UNKNOWN', 'technical_status': 'FAIL' if any(x['verdict'] == 'FAIL' for x in findings) else 'UNKNOWN',
        'professional_acceptance': 'not_reviewed', 'user_acceptance': False, 'filing_ready': False}
