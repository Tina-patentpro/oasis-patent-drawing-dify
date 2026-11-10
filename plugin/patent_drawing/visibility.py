"""Source-derived visibility obligations and hash-bound technical review."""
import datetime
import re
from .operation_store import digest
from .source_contract import validate_facts
from .spatial_brief import requirements

ACTUAL_CONTRACT = 'visibility-039-r5'
ACTUAL_ROLES = {
    'channel-contour': ['wall_boundary', 'channel_run', 'independent_wire'],
    'channel-partition-solids': ['partition_face', 'partition_extent', 'independent_wire'],
    'pin-hole-fit': ['inserted_pin', 'plated_hole', 'pcb_section'],
    'integral-pin-ownership': ['busbar_pin_union', 'inserted_pin'],
    'rib-insertion-contact': ['rib', 'plated_wall', 'contact'],
    'conductive-path': ['busbar_pin_union', 'plated_wall', 'contact'],
    'cable-side-opening': ['cable_opening', 'rear_trough', 'weld_target', 'trough_shield_relation'],
}
OBSERVATION_SCOPES = {
    'pin-hole-fit': 'local_pin_fit',
    'rib-insertion-contact': 'local_rib_contact',
    'no-unsupported-ear': 'whole_base_inventory',
}
PARENT_FIELDS = ('parent_view_id', 'parent_base_sha256', 'parent_review_sha256')


def build_visibility_contract(bundle: dict, plan: dict, view: dict) -> dict:
    valid = [f for f in plan.get('facts', []) if f.get('status') == 'confirmed'
             and not any(x['verdict'] == 'FAIL' for x in validate_facts(bundle, [f]))]
    quotes = ' '.join(e.get('quote', '') for f in valid for e in f.get('evidence', []))
    features = [{'feature_id': x['id'], 'requirement': x['required_visibility'],
                 'source_fact_ids': x['source_fact_ids']} for x in requirements(bundle, plan, view)['features']]
    goal = view.get('instruction', '') + view.get('purpose', '')
    support_goal = '弹簧' in goal and any(x in goal for x in ['四角', '四个'])
    supported = all(x in quotes for x in ['四', '角', '弹簧', '滑板安装板', '托板', '支承'])
    if supported and view.get('requested_type') in {'assembly', 'spatial', 'detail'}:
        evidence = [{'fact_id': f['id'], 'evidence': f.get('evidence', [])} for f in valid
                    if any('弹簧' in e.get('quote', '') for e in f.get('evidence', []))]
        for i in range(1, 5):
            features.append({'feature_id': 'spring-support-' + str(i), 'instance_id': 'spring-support-' + str(i),
                             'requirement': '独立辨认弹簧支承组件本体及上下支承关系；圆点、螺钉头、引出线不能替代。',
                             'source_evidence': evidence, 'forbidden_substitutes': ['fastener', 'dot']})
        features += [
            {'feature_id': 'omit-occluder', 'requirement': '按本图目标省略上部滑板遮挡，保留原图。'},
            {'feature_id': 'board-support-identity', 'requirement': '滑板安装板与下方托板身份及已披露关系可辨。'},
            {'feature_id': 'no-invented-interfaces', 'requirement': '不增加未知隐藏连接、尺寸、关节和导体。'}]
    features.append({'feature_id': 'source-fidelity', 'requirement': '实际图形和本视图所涉来源身份一致，无未披露结构。'})
    expression = view.get('expression_mode') == 'relational_schematic'
    context = {}
    instances = (view.get('expression_scene') or {}).get('instances', []) if expression else []
    busbar_scene = any(r.get('role') == 'busbar_body' for r in instances)
    if expression:
        context = {'source_sha256':plan['source_sha256'], 'expression_scene_sha256':digest(view.get('expression_scene',{}))}
        features.append({'feature_id':'no-unsupported-ear','requirement':'逐一核对实体清单；无来源侧耳、平台、螺柱、盖板、螺栓及垫片不能并入母排身份隐藏。'})
        if any(f['feature_id']=='channel-contour' for f in features):
            features += [{'feature_id':'channel-partition-solids','requirement':'实际分隔壁有实体厚度和连续边界，导线位于通道内；导线或遮挡线不能代替壁。'}]
        if any(f['feature_id']=='pin-hole-fit' for f in features):
            features += [{'feature_id':'integral-pin-ownership','requirement':'安装销直接从母排延续至PCB孔，不能是独立螺柱或紧固件。'},
                         {'feature_id':'rib-insertion-contact','requirement':'在实际入孔段看清销上可变形肋与镀覆孔壁接触；仅同轴或孔外凸起不能通过。'},
                         {'feature_id':'conductive-path','requirement':'可理解母排—一体销—PCB镀覆接地孔的连续导电关系，不把虚空或独立套筒当作镀覆孔。'}]
    version = 'visibility-038-r1' if expression else 'visibility-035-r1'
    if expression and (any(f['feature_id'] in ACTUAL_ROLES or f['feature_id'].startswith('spring-support-')
                           for f in features)
                       or busbar_scene):
        version = ACTUAL_CONTRACT
        context['requires_parent'] = view.get('reference_selection') == 'guide_and_parent'
        for f in features:
            ident = f['feature_id']
            roles = ACTUAL_ROLES.get(ident)
            if ident.startswith('spring-support-'):
                roles = ['body', 'upper_contact', 'lower_contact']
            elif ident == 'board-support-identity':
                roles = ['upper_plate', 'lower_plate']
                if any('竖板' in p['name'] and p['id'] in view.get('parts', []) for p in plan['parts']):
                    roles.append('vertical_plate')
            elif ident in {'no-unsupported-ear', 'no-invented-interfaces'}:
                roles = ['entity_inventory']
            if roles:
                f['observation_roles'] = list(roles)
            if ident in OBSERVATION_SCOPES:
                f['observation_scope'] = OBSERVATION_SCOPES[ident]
            if busbar_scene and ident == 'no-unsupported-ear':
                f['scene_instances'] = {r['id']: {k: r[k] for k in ('role', 'part_id', 'fact_ids')}
                                        for r in instances}
            if busbar_scene and ident == 'channel-partition-solids':
                f['requires_partition_traces'] = True
            if busbar_scene and ident == 'pin-hole-fit':
                f['requires_insertion_zone'] = True
    return {'contract': version, 'view_id': view['id'], 'features': features,
            **context,
            'drawability': 'needs_source' if support_goal and not supported else 'ready_for_candidate',
            'professional_acceptance': 'not_reviewed'}


def check_visibility_review(review: dict, base_sha256: str, spec_sha256: str, contract: dict,
                            *, guide_sha256=None, request_id=None, base_image_size=None,
                            parent_binding=None) -> list[dict]:
    def fail(message): return [{'rule': 'ACTUAL-VISIBILITY-REVIEW', 'verdict': 'FAIL', 'message': message}]
    if (review.get('view_id') != contract['view_id'] or review.get('base_sha256') != base_sha256
            or review.get('spec_sha256') != spec_sha256 or review.get('contract_sha256') != digest(contract)):
        return fail('visual review base/spec/contract identity differs')
    actual_contract = contract.get('contract') == ACTUAL_CONTRACT
    if contract.get('contract') in {'visibility-038-r1', ACTUAL_CONTRACT}:
        if (not isinstance(guide_sha256,str) or not re.fullmatch(r'[a-f0-9]{64}',guide_sha256)
                or review.get('guide_sha256') != guide_sha256
                or review.get('source_sha256') != contract.get('source_sha256')
                or review.get('scene_sha256') != contract.get('expression_scene_sha256')
                or not isinstance(request_id,str) or not re.fullmatch(r'req_[A-Za-z0-9_-]{1,160}',request_id)
                or review.get('request_id') != request_id):
            return fail('actual review guide/source/scene/request identity differs')
    if actual_contract:
        if (review.get('basis') != 'actual_base' or not isinstance(base_image_size, (list, tuple))
                or len(base_image_size) != 2 or any(type(n) is not int or n <= 0 for n in base_image_size)):
            return fail('actual-base basis and image dimensions required')
        expected_parent = None
        if contract['requires_parent']:
            if not isinstance(parent_binding, dict) or not all(parent_binding.get(k) for k in PARENT_FIELDS):
                return fail('actual child review needs its accepted mother image identity')
            expected_parent = {k: parent_binding[k] for k in PARENT_FIELDS}
        if review.get('parent_binding') != expected_parent:
            return fail('actual review mother-image identity differs')
    if not isinstance(review.get('reviewer'), str) or not review['reviewer'].strip():
        return fail('visual reviewer required')
    try:
        when = datetime.datetime.fromisoformat(review['reviewed_at'])
        if when.tzinfo is None: return fail('review time needs timezone')
    except (KeyError, TypeError, ValueError): return fail('visual review timestamp invalid')
    rows = review.get('features', [])
    if (not isinstance(rows, list) or len(rows) != len(contract['features'])
            or any(not isinstance(row, dict) for row in rows)):
        return fail('review must cover every feature')
    required = {f['feature_id']: f for f in contract['features']}
    if {f.get('feature_id') for f in rows if isinstance(f.get('feature_id'),str)} != set(required):
        return fail('review feature identities differ')
    spring_regions = []
    spring_observations = set()
    role_by_box = {}
    insertion_zone = None
    rib_contact = None
    for row in rows:
        if row.get('verdict') not in {'PASS', 'FAIL', 'UNKNOWN'} or not isinstance(row.get('note'), str) or not row['note'].strip():
            return fail('each feature needs an explicit verdict and evidence note')
        if row['verdict'] == 'PASS':
            region = row.get('region')
            if (not isinstance(region, list) or len(region) != 4 or any(type(x) is not int for x in region)
                    or not 0 <= region[0] < region[2] or not 0 <= region[1] < region[3]):
                return fail('passed visibility needs a concrete image region')
            if row['feature_id'].startswith('spring-support-'):
                if row.get('visible_object') != 'spring_support_body_and_interfaces':
                    return fail('spring body/interfaces must be identified; dots or fasteners are insufficient')
                spring_regions.append(region)
            if actual_contract:
                feature = required[row['feature_id']]
                roles = feature.get('observation_roles', [])
                error = observation_issue(row, roles, base_image_size, feature.get('observation_scope'))
                if error:
                    return fail(row['feature_id'] + ': ' + error)
                if 'scene_instances' in feature:
                    error = instance_inventory_issue(row.get('entities'), feature['scene_instances'], base_image_size)
                    if error:
                        return fail(row['feature_id'] + ': ' + error)
                if feature.get('requires_partition_traces'):
                    error = partition_trace_issue(row)
                    if error:
                        return fail(row['feature_id'] + ': ' + error)
                if feature.get('requires_insertion_zone'):
                    insertion_zone = row.get('insertion_zone')
                    boxes = {item['role']: item['region'] for item in row['observations']}
                    if (not region_within(insertion_zone, boxes['plated_hole'])
                            or not region_within(insertion_zone, boxes['pcb_section'])
                            or not all(max(insertion_zone[i], boxes['inserted_pin'][i])
                                       < min(insertion_zone[i + 2], boxes['inserted_pin'][i + 2])
                                       for i in (0, 1))):
                        return fail('pin-hole-fit: need an insertion zone within the observed plated bore, pin and PCB')
                if feature.get('observation_scope') == 'local_rib_contact':
                    rib_contact = next(item['region'] for item in row['observations'] if item['role'] == 'contact')
                for observation in row.get('observations', []) if roles else []:
                    box = tuple(observation['region'])
                    role = observation['role']
                    if role_by_box.setdefault(box, role) != role:
                        return fail('different actual roles cannot share the same observation region')
                if row['feature_id'].startswith('spring-support-'):
                    boxes = {tuple(x['region']) for x in row['observations']}
                    if boxes & spring_observations:
                        return fail('each support needs its own body and interface observations')
                    spring_observations.update(boxes)
    if len({tuple(r) for r in spring_regions}) != len(spring_regions):
        return fail('each support instance needs a distinct evidence region')
    if insertion_zone is not None and rib_contact is not None and not region_within(rib_contact, insertion_zone):
        return fail('rib-insertion-contact: observed contact is outside the plated bore insertion zone')
    return []


def region_within(region, container):
    return (isinstance(region, list) and len(region) == 4
            and all(type(value) is int for value in region)
            and all(container[i] <= region[i] < region[i + 2] <= container[i + 2] for i in (0, 1)))


def instance_inventory_issue(entities, expected, size):
    """Validate the reviewer inventory against the source-bound illustrative scene."""
    if not isinstance(entities, list) or len(entities) != len(expected):
        return 'instance inventory must account for every source-scene instance and any extra silhouette'
    seen, observed_regions = set(), set()
    for entity in entities:
        if (not isinstance(entity, dict) or
                set(entity) != {'instance_id', 'role', 'part_id', 'fact_ids', 'state', 'region', 'note'}):
            return 'instance inventory row is incomplete'
        ident, state, region = entity['instance_id'], entity['state'], entity['region']
        if not isinstance(ident, str) or ident in seen or ident not in expected:
            return 'instance inventory has a duplicate or unsupported instance'
        seen.add(ident)
        if any(entity[k] != v for k, v in expected[ident].items()):
            return 'instance inventory role, part or source facts differ'
        if not isinstance(entity['note'], str) or not entity['note'].strip():
            return 'instance inventory needs an observation note per instance'
        if state == 'not_visible' and region is None:
            continue
        if (state != 'observed' or not region_within(region, [0, 0, *size])
                or tuple(region) in observed_regions):
            return 'instance inventory needs distinct actual regions and resolved identities'
        observed_regions.add(tuple(region))
    return None if observed_regions else 'instance inventory has no actual visible instances'


def partition_trace_issue(row):
    """Check reviewer-traced wall edges and wires in actual-base coordinates."""
    witness = row.get('partition')
    if not isinstance(witness, dict) or set(witness) != {'boundaries', 'wires', 'opaque'} or witness['opaque'] is not True:
        return 'need actual opaque partition and independent wire trace evidence'
    boundaries, wires = witness['boundaries'], witness['wires']
    if not isinstance(boundaries, list) or len(boundaries) != 2 or not isinstance(wires, list) or not 1 <= len(wires) <= 32:
        return 'need two partition boundaries and at least one independently visible wire'
    boxes = {item['role']: item['region'] for item in row['observations']}

    def valid_trace(points, box):
        return (isinstance(points, list) and 2 <= len(points) <= 64
                and all(isinstance(p, list) and len(p) == 2
                        and all(type(p[i]) is int and box[i] <= p[i] < box[i + 2] for i in (0, 1)) for p in points)
                and len({tuple(p) for p in points}) == len(points))

    if not all(valid_trace(trace, boxes['partition_extent']) for trace in boundaries) or not all(
            valid_trace(trace, boxes['independent_wire']) for trace in wires):
        return 'wall and wire traces must use distinct bounded pixels within their respective observations'

    def touches(a, b, c, d):
        def cross(x, y, z):
            return (y[0] - x[0]) * (z[1] - x[1]) - (y[1] - x[1]) * (z[0] - x[0])
        return (all(max(min(a[i], b[i]), min(c[i], d[i])) <= min(max(a[i], b[i]), max(c[i], d[i]))
                    for i in (0, 1))
                and cross(a, b, c) * cross(a, b, d) <= 0
                and cross(c, d, a) * cross(c, d, b) <= 0)

    # Both visible edges run in the same direction. The short ends close the
    # observation locally, without asserting the wall's full physical extent.
    outline = boundaries[0] + boundaries[1][::-1]
    if len({tuple(point) for point in outline}) != len(outline):
        return 'partition boundaries coincide'
    edges = list(zip(outline, outline[1:] + outline[:1]))
    for i, (a, b) in enumerate(edges):
        end = len(edges) - (i == 0)
        if any(touches(a, b, c, d) for c, d in edges[i + 2:end]):
            return 'partition boundaries intersect or reverse within the observed span'

    def inside(x, y):
        return sum((a[1] > y) != (b[1] > y) and x < a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
                   for a, b in edges) % 2 == 1

    face = boxes['partition_face']
    if not inside((face[0] + face[2]) / 2, (face[1] + face[3]) / 2):
        return 'partition boundaries must bound the observed wall face'
    if any(inside(*wire[0]) or any(touches(a, b, c, d) for a, b in zip(wire, wire[1:]) for c, d in edges)
           for wire in wires):
        return 'visible wire intersects the observed solid partition'
    return None


def observation_issue(row, roles, size, scope=None):
    region = row['region']
    if region[2] > size[0] or region[3] > size[1]:
        return 'feature region outside actual base'
    if not roles:
        return None
    observations = row.get('observations')
    if (not isinstance(observations, list) or len(observations) != len(roles)
            or any(not isinstance(x, dict) or set(x) != {'role', 'region', 'note'}
                   or not isinstance(x['role'], str) for x in observations)
            or {x['role'] for x in observations} != set(roles)):
        return 'feature needs one actual observation for every required role'
    seen = set()
    for entry in observations:
        box = entry['region']
        if (not isinstance(entry['note'], str) or not entry['note'].strip()
                or not isinstance(box, list) or len(box) != 4 or any(type(n) is not int for n in box)
                or not all(region[i] <= box[i] < box[i+2] <= region[i+2] for i in (0, 1))
                or (entry['role'] != 'entity_inventory'
                    and 5 * (box[2]-box[0]) * (box[3]-box[1]) >= 4 * size[0] * size[1])
                or tuple(box) in seen):
            return 'observations need distinct, bounded actual-base regions and notes'
        seen.add(tuple(box))
    regions = {entry['role']: entry['region'] for entry in observations}
    def overlap(*names):
        boxes = [regions[name] for name in names]
        return all(max(b[i] for b in boxes) < min(b[i+2] for b in boxes) for i in (0, 1))
    if 'upper_contact' in roles and 'lower_contact' in roles and overlap('upper_contact', 'lower_contact'):
        return 'upper and lower contact observations must be independently localized'
    if scope == 'whole_base_inventory' and regions['entity_inventory'] != [0, 0, *size]:
        return 'unsupported entities must be checked on the whole actual image'
    if scope in {'local_pin_fit', 'local_rib_contact'} and any(
            2 * (box[i+2] - box[i]) >= size[i] for box in regions.values() for i in (0, 1)):
        return 'local fit observations must be bounded on both image axes'
    if scope == 'local_pin_fit' and not overlap('inserted_pin', 'plated_hole', 'pcb_section'):
        return 'inserted pin, plated hole and PCB must be observed in the same local fit'
    if scope == 'local_rib_contact':
        contact = regions['contact']
        if (any(4 * (contact[i+2] - contact[i]) >= size[i] or contact[i+2] - contact[i] > max(
                regions[name][i+2] - regions[name][i] for name in ('rib', 'plated_wall')) for i in (0, 1))
                or not overlap('rib', 'contact') or not overlap('plated_wall', 'contact')):
            return 'one bounded local contact must identify both the insertion rib and its plated wall'
    return None


def check_record_review(review, record, contract, *, base_image_size=None):
    return check_visibility_review(review, record['base']['sha256'], record['base_spec_sha256'], contract,
        guide_sha256=record.get('expression_guide_sha256'), request_id=record.get('request_id'),
        base_image_size=base_image_size or record.get('base_image_size'),
        parent_binding=(record.get('reference_bundle_receipt') or {}).get('parent_binding'))


def review_passed(review, record, contract):
    return (isinstance(review, dict) and not check_record_review(review, record, contract)
            and all(x['verdict'] == 'PASS' for x in review['features']))


def matching_review(state, fid, record, contract):
    if not contract: return None
    history = [x['review'] for x in state.get('visual_review_history', [])]
    if state.get('visual_reviews', {}).get(fid): history.append(state['visual_reviews'][fid])
    matches = [r for r in history if not check_record_review(r, record, contract)]
    return matches[-1] if matches else None


def review_todos(state: dict, holder: dict, records: dict) -> dict:
    """A rendered file can still need review or correction; missing != failed."""
    failed, unknown, unreviewed = [], [], []
    for view in holder.get('plan', {}).get('views', []):
        fid = view['id']
        if view['engine'] != 'image' or fid not in records:
            continue
        contract = holder['plan'].get('visibility_contract', {}).get(fid)
        review = matching_review(state, fid, records[fid], contract)
        failures = [f for f in (review or {}).get('features', []) if f['verdict'] == 'FAIL']
        failed.extend(fid + '/' + f['feature_id'] for f in failures)
        unknown.extend(fid + '/' + f['feature_id'] for f in (review or {}).get('features', [])
                       if f['verdict'] == 'UNKNOWN')
        if not failures and (not review or not review_passed(review, records[fid], contract)):
            unreviewed.append(fid)
    return {'failed_feature_ids': failed, 'unknown_feature_ids': unknown, 'unreviewed_view_ids': unreviewed}


def review_todo_message(status: dict) -> str:
    parts = []
    for key, label in [('missing_ids', '未生成图件'), ('failed_feature_ids', '待修正特征'),
                       ('unknown_feature_ids', '实际图待确认特征'), ('unreviewed_view_ids', '待实际审阅')]:
        values = status.get(key, [])
        if values:
            parts.append(label + '：' + '、'.join(values))
    return '；'.join(parts) or '完整图集待最终核查'
