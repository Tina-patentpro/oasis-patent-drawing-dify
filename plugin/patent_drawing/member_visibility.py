"""Source/view-bound observation requirements; never a source geometry waiver."""
import re
from .expression_contract import validate_expression_contract, expression_context
from .operation_store import digest
from .scene_derivation import physical_scene_identity
from .visibility import build_visibility_contract


def derive_member_visibility_requirements(bundle: dict, plan: dict, view: dict,
                                           contract: dict, scene: dict, *, approved_plan=None) -> dict:
    """Called with the actual privately loaded contract by Workspace.

    Only an assembly observation, not an explicit member/detail observation,
    may retain an unrequested moving rod hidden by its own mounting plate.
    Geometry, all other members and both shared-plate contacts stay mandatory.
    """
    original=plan if approved_plan is None else approved_plan
    if (validate_expression_contract(contract, bundle, original)['status'] != 'PASS'
            or expression_context(original)!=expression_context(plan)):
        raise ValueError('current source expression required for member visibility')
    if view not in plan.get('views', []) or scene != view.get('expression_scene'):
        raise ValueError('member visibility must bind actual plan view and scene')
    features = {f['feature_id'] for f in build_visibility_contract(bundle, plan, view)['features']}
    assembly = (view.get('requested_type') in {'assembly', 'spatial'}
                and {f'spring-support-{i}' for i in range(1, 5)} <= features)
    goal = ' '.join(str(view.get(k, '')) for k in ('title', 'purpose', 'instruction'))
    parts = {p['id']: p for p in plan.get('parts', [])}
    instances = {}
    for row in scene.get('instances', []):
        if not isinstance(row, dict):
            raise ValueError('member observation needs bounded scene instances')
        if row.get('shape') != 'sleeved_support':
            continue
        from .support_assembly import validate_support_assembly
        if validate_support_assembly(row, contract, list(parts))['status'] != 'PASS':
            raise ValueError('member observation needs valid source exterior members')
        members = row.get('members', [])
        rods = [m for m in members if m.get('role') == 'moving_rod']
        optional = []
        if assembly and row.get('part_id') in view.get('parts', []) and len(rods) == 1:
            rod = rods[0]; part = rod.get('part_id'); name = parts.get(part, {}).get('name', '')
            stable_reference=re.search(r'(?<![A-Za-z0-9_-])'+re.escape(part)+r'(?![A-Za-z0-9_-])',goal)
            label=str(parts.get(part,{}).get('label')or'')
            label_reference=False
            if label:
                token=re.escape(label)+r'(?![A-Za-z0-9])'
                prefix=r'(?:编号|标号|部件|零件|构件|显示|展示|看清|辨认|露出|reference|ref|part|label|show|display)\s*[:：#]?\s*[(（\[]?\s*'
                label_reference=bool(re.search(prefix+token,goal,re.IGNORECASE)
                                     or re.search(r'(?<![A-Za-z0-9])'+token+r'\s*号',goal))
            explicit = (part in view.get('parts', []) or (name and name in goal)
                        or stable_reference or label_reference
                        or any(s in goal for s in ('全部成员', '全部外部构件', 'all exterior members')))
            if not explicit:
                optional = [part]
        instances[row['id']] = {
            'required_member_part_ids': sorted(m['part_id'] for m in members if m['part_id'] not in optional),
            'occludable_member_part_ids': optional,
            'source_fact_ids': sorted({m['fact_id'] for m in members}),
            'reason': ('Assembly body and both endpoints required; unrequested rod may be hidden '
                       'only by its own actual mounting plate, with retained occlusion evidence.'
                       if optional else 'All exterior members required for this observation.')}
    return {'source_sha256': plan['source_sha256'], 'plan_context_sha256': expression_context(plan),
            'view_id': view['id'], 'contract_sha256': digest(contract),
            'physical_scene_sha256': physical_scene_identity(scene), 'instances': instances}


def member_context_matches(requirements: dict, scene: dict, contract: dict) -> bool:
    try:
        return (isinstance(requirements, dict)
                and requirements.get('physical_scene_sha256') == physical_scene_identity(scene)
                and requirements.get('contract_sha256') == digest(contract))
    except (ValueError, TypeError, KeyError):
        return False
