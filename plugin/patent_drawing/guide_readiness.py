"""Whole-view obligations, separate from local geometric validity and image QA.

Roles below are an illustrative vocabulary, not new source parts. A local
channel/pin witness cannot qualify as a complete assembly just by being valid.
"""
from .visibility import build_visibility_contract
from .expression_contract import unknown_disposition_issues
from .source_profiles import check_registered_contacts, source_known_parts


_FEATURE_ROLES = {
    'channel-contour': ('external_wall', 'partition_wall', 'signal_wire'),
    'channel-partition-solids': ('external_wall', 'partition_wall', 'signal_wire'),
    'pin-hole-fit': ('busbar_body', 'integral_pin', 'deformable_rib', 'plated_hole', 'pcb'),
    'integral-pin-ownership': ('busbar_body', 'integral_pin'),
    'rib-insertion-contact': ('integral_pin', 'deformable_rib', 'plated_hole', 'pcb'),
    'conductive-path': ('busbar_body', 'integral_pin', 'plated_hole', 'pcb'),
    'cable-side-opening': ('busbar_body', 'rear_trough', 'shielded_cable', 'weld_opening'),
    'board-support-identity': ('supported_plate', 'supporting_plate', 'support_body'),
}

# Most-specific names precede their contained generic words. Only parts the
# actual view requests are used; internal parts and optional drain wires are
# never forced into an external assembly merely because they exist in a plan.
_PART_ROLES = (
    ('滑板安装板', 'supported_plate'), ('托板', 'supporting_plate'),
    ('弓头弹簧组件', 'support_body'), ('竖板', 'vertical_plate'),
    ('电缆侧预留焊接开口', 'weld_opening'), ('焊接开口', 'weld_opening'),
    ('后部电缆槽', 'rear_trough'), ('电缆屏蔽层', 'shield'),
    ('屏蔽电缆', 'shielded_cable'), ('信号导线', 'signal_wire'),
    ('导电接地母线', 'busbar_body'), ('分隔壁', 'partition_wall'),
    ('外壁', 'external_wall'), ('安装销', 'integral_pin'),
    ('可变形肋', 'deformable_rib'), ('镀覆接地孔', 'plated_hole'),
)


def derive_view_obligations(bundle: dict, plan: dict, view: dict) -> dict:
    contract = build_visibility_contract(bundle, plan, view)
    selected = [p for p in plan.get('parts', []) if p['id'] in view.get('parts', [])]
    bindings = []
    for part in selected:
        role = next((role for text, role in _PART_ROLES if text in part['name']), None)
        if role:
            bindings.append({'part_id': part['id'], 'role': role})
    features = []
    for feature in contract['features']:
        ident = feature['feature_id']
        roles = list(_FEATURE_ROLES.get(ident, ()))
        if ident.startswith('spring-support-'):
            roles = ['supported_plate', 'supporting_plate', 'support_body']
        row = {'feature_id': ident, 'source_fact_ids': feature.get('source_fact_ids', []),
               'required_roles': roles, 'critical': True}
        if ident == 'source-fidelity':
            row['required_part_roles'] = bindings
        if ident == 'cable-side-opening':
            row['any_roles'] = ['shield', 'drain_wire']
        features.append(row)
    return {'view_id': view['id'], 'source_sha256': plan['source_sha256'],
            'required_features': features}


def validate_guide_readiness(bundle: dict, plan: dict, view: dict, guide: dict, *, expression_contract=None,
                             member_requirements=None, assembly_registration=None) -> dict:
    obligations = derive_view_obligations(bundle, plan, view)
    checks = check_registered_contacts(assembly_registration, guide, expression_contract=expression_contract)
    missing = []
    projection_features = {
        'STRUCTURE-PROJECTED-CHANNEL': {'channel-contour', 'channel-partition-solids'},
        'STRUCTURE-PROJECTED-PIN-FIT': {'pin-hole-fit', 'rib-insertion-contact'},
        'STRUCTURE-PROJECTED-FIT-CONTACT': {'rib-insertion-contact'},
    }
    scene = view.get('expression_scene', {})
    reported=guide.get('member_visibility_requirements')
    if reported is not None or member_requirements is not None:
        from .member_visibility import derive_member_visibility_requirements
        try:
            if expression_contract is None:
                raise ValueError('private expression context required for member observation')
            expected=member_requirements if member_requirements is not None else derive_member_visibility_requirements(
                bundle,plan,view,expression_contract,view.get('expression_scene',{}))
            if reported!=expected:
                raise ValueError('member visibility context differs from current source/view/contract')
            from .structural_semantics import validate_structure
            verified=validate_structure(view['expression_scene'],guide,expression_contract,member_requirements=expected)
            checks.extend(c for c in verified['checks']if c['verdict']!='PASS')
        except (ValueError, TypeError, KeyError) as error:
            checks.append({'rule':'GUIDE-MEMBER-CONTEXT','verdict':'FAIL','evidence_regions':[], 'message':str(error)})
    elif expression_contract is not None and guide.get('regions') and any(
            rel['kind'] in {'channel_partition', 'rib_in_plated_hole'} for rel in scene.get('relations', [])):
        from .structural_semantics import validate_structure
        verified = validate_structure(scene, guide, expression_contract)
        checks.extend(c for c in verified['checks']
                      if c['rule'] in projection_features and c['verdict']!='PASS')
    if guide.get('status') != 'PASS':
        checks.extend(c for c in guide.get('issues', []) if c['verdict']!='PASS' and c not in checks)
    regions = guide.get('visible_regions', guide.get('regions', []))
    visible = [r for r in regions if r.get('ink_pixels', 0) > 0
               and r.get('visible_fraction', 0) >= .3]
    roles = {r.get('role') for r in visible}
    supports = [r for r in visible if r.get('role') == 'support_body']
    source_parts = source_known_parts(bundle, plan, view)
    region = next((r for r in bundle.get('figure_candidates', []) if r['id']==view.get('source_region_id')), None)
    page = next((p for p in bundle.get('pages', []) if region and p['id']==region['page_id']), {})
    source_image = page.get('image', {}).get('sha256')
    bound_parts = {b.get('part_id') for b in guide.get('source_region_bindings', [])
                   if source_image and b.get('source_image_sha256') == source_image
                   and b.get('source_region_id') == view.get('source_region_id')
                   and any(r.get('instance_id') == b.get('instance_id') for r in visible)}
    if assembly_registration and not checks and guide.get('status') == 'PASS':
        registered_ids = set(assembly_registration['supports'])
        if {r['instance_id'] for r in supports} == registered_ids:
            bound_parts.update(r['part_id'] for r in supports)
    for item in obligations['required_features']:
        ident = item['feature_id']
        absent = sorted(set(item['required_roles']) - roles)
        unbound = [r for r in item.get('required_part_roles', [])
                   if not any(g.get('part_id') == r['part_id'] and g.get('role') == r['role'] for g in visible)]
        alternative_missing = bool(item.get('any_roles')) and not roles.intersection(item['any_roles'])
        missing_support = (ident.startswith('spring-support-')
                           and len({r.get('instance_id') for r in supports}) < 4)
        source_unpreserved = sorted(set(source_parts)-bound_parts) if ident=='source-fidelity' else []
        if absent or unbound or alternative_missing or missing_support or source_unpreserved:
            missing.append(ident)
        descriptions = absent + [r['part_id'] + ':' + r['role'] for r in unbound]
        if alternative_missing:
            descriptions.append('shield OR drain_wire')
        if missing_support:
            descriptions.append('four distinct support_body instances')
        if source_unpreserved:
            descriptions.append('source-segment bindings: ' + ', '.join(source_unpreserved))
        checks.append({'rule': 'GUIDE-COMPLETE-VIEW',
                       'verdict': 'FAIL' if ident in missing else 'PASS',
                       'feature_id': ident,
                       'evidence_regions': [r.get('ink_bbox') for r in visible
                                            if r.get('role') in item['required_roles']],
                       'message': 'Missing visible source roles: ' + ', '.join(descriptions)
                                  if descriptions else 'Required source roles have projected ink; actual image still unreviewed'})
    failed = guide.get('status') == 'FAIL' or bool(missing) or any(c['verdict']=='FAIL'for c in checks)
    contact_features = {'channel-contour', 'pin-hole-fit', 'spring-support-1'}
    if (contact_features.intersection(f['feature_id'] for f in obligations['required_features'])
            and guide.get('visibility_model') != 'opaque_surfaces_with_ink_ownership'):
        checks.append({'rule': 'GUIDE-OCCLUSION', 'verdict': 'UNKNOWN',
                       'evidence_regions': [],
                       'message': 'Transparent wireframe cannot qualify source-required contact visibility'})
    if 'visible_regions' in guide and guide['visible_regions'] != guide.get('regions'):
        failed = True
        checks.append({'rule': 'GUIDE-PROJECTED-IDENTITY', 'verdict': 'FAIL',
                       'evidence_regions': [], 'message': 'Projected and visible region records differ'})
    if guide.get('status') != 'PASS':
        checks.append({'rule': 'GUIDE-LOCAL-GEOMETRY', 'verdict': 'UNKNOWN' if guide.get('status') == 'UNKNOWN' else 'FAIL',
                       'evidence_regions': [], 'message': 'Local geometry is not a passing witness'})
    unknowns = guide.get('source_unknowns', [])
    dispositions = unknown_disposition_issues(unknowns, plan)
    checks.extend({**x, 'evidence_regions': []} for x in dispositions)
    if any(x['verdict'] == 'FAIL' for x in dispositions):
        failed = True
    projection_missing = {feature for c in checks if c['verdict']!='PASS'
                          for feature in projection_features.get(c['rule'], ())}
    requested = {f['feature_id'] for f in obligations['required_features']}
    return {'status': 'FAIL' if failed else 'UNKNOWN' if any(c['verdict']=='UNKNOWN'for c in checks) else 'PASS', 'checks': checks,
            'missing_features': sorted(set(missing) | (projection_missing & requested)), 'unknowns': unknowns,
            'obligations': obligations, 'actual_image_review': 'NOT_RUN'}
