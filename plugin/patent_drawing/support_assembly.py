"""Source-bound exterior members of one support; no hidden joint or CAD inference."""
import copy
import math


_ROLES = {'mount_plate': 'solid', 'moving_rod': 'cylinder',
          'closed_housing': 'cylinder', 'lower_stem': 'cylinder'}


def validate_support_assembly(instance: dict, contract: dict, part_ids: list[str]) -> dict:
    def fail(message): return {'status': 'FAIL', 'issues': [message]}
    def bounds(value):
        return (isinstance(value, list) and len(value) == 6
                and all(type(x) in (int, float) and math.isfinite(x) and abs(x) <= 10000 for x in value)
                and all(value[i] < value[i+3] for i in range(3)))
    if instance.get('shape') != 'sleeved_support' or instance.get('role') != 'support_body' or not bounds(instance.get('bounds')):
        return fail('source composite support bounds and identity required')
    members = instance.get('members'); outer = instance['bounds']
    if not isinstance(members, list) or len(members) != 4 or any(not isinstance(m, dict) for m in members):
        return fail('exactly four source-confirmed exterior members required')
    if any(not isinstance(m.get(k), str) or not m[k] for m in members for k in ('part_id', 'role', 'fact_id')):
        return fail('external member source identity must be a nonempty string')
    if {m.get('role') for m in members} != set(_ROLES) or len({m.get('part_id') for m in members}) != 4:
        return fail('external plate, rod, closed housing and lower stem must remain distinct')
    facts = {f['id']: f for f in contract.get('facts', []) if f.get('certainty') == 'confirmed'}
    by_role = {}
    for member in members:
        stepped = member.get('role') == 'lower_stem' and member.get('shape') == 'shouldered_stem'
        fields = {'part_id', 'role', 'shape', 'bounds', 'fact_id'} | ({'stem_radius', 'shoulder_height'} if stepped else set())
        if (set(member) != fields
                or member['part_id'] not in part_ids or (not stepped and member['shape'] != _ROLES[member['role']])
                or not bounds(member['bounds'])):
            return fail('unbound or unsupported external member')
        b = member['bounds']; fact = facts.get(member['fact_id'], {})
        if (fact.get('predicate') != 'assembly_' + member['role'] or fact.get('subject') != instance.get('part_id')
                or fact.get('object') != member['part_id'] or not fact.get('evidence')):
            return fail('member needs quoted source ownership of this aggregate')
        if any(b[i] < outer[i]-1e-7 or b[i+3] > outer[i+3]+1e-7 for i in range(3)):
            return fail('external member outside support envelope')
        if member['shape'] in {'cylinder', 'shouldered_stem'} and abs((b[3]-b[0])-(b[4]-b[1])) > 1e-7:
            return fail('round member extents differ')
        if stepped and any(type(member[k]) not in (int, float) or not math.isfinite(member[k])
                           or not 0 < member[k] < limit for k, limit in (
                               ('stem_radius', (b[3]-b[0])/2), ('shoulder_height', b[5]-b[2]))):
            return fail('source shoulder and stem require bounded connected extents')
        if any(abs((b[i]+b[i+3])-(outer[i]+outer[i+3])) > 1e-7 for i in (0, 1)):
            return fail('external member is disconnected from the common support axis')
        by_role[member['role']] = member
    chain = [by_role[k] for k in ('lower_stem', 'closed_housing', 'moving_rod', 'mount_plate')]
    if (abs(chain[0]['bounds'][2]-outer[2]) > 1e-7 or abs(chain[-1]['bounds'][5]-outer[5]) > 1e-7
            or any(abs(a['bounds'][5]-b['bounds'][2]) > 1e-7 for a, b in zip(chain, chain[1:]))):
        return fail('source exterior chain must meet both surfaces without gaps or overlapping full-span housing')
    return {'status': 'PASS', 'normalized_instance': copy.deepcopy(instance),
            'member_ids': [m['part_id'] for m in members],
            'endpoint_members': {'upper': by_role['mount_plate']['part_id'], 'lower': by_role['lower_stem']['part_id']},
            'issues': []}
