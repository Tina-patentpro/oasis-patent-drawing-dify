"""Bounded observations of a source-bound scene, never new physical designs."""
import copy
import math

from .operation_store import canonical, digest


_OBSERVATION = {'canvas', 'projection', 'panels'}
_ENDPOINTS = {
    'supports_at': ('subjects', 'upper', 'lower'),
    'source_identity': ('subjects',),
    'channel_partition': ('body', 'walls', 'partitions', 'wires'),
    'integral_pin': ('body', 'pin'),
    'rib_in_plated_hole': ('pin', 'hole', 'board', 'ribs'),
    'conductive_path': ('path',),
    'trough_shield_contact': ('cable', 'shield', 'troughs'),
    'cable_weld_access': ('body', 'opening', 'target', 'troughs'),
}


def _instances(scene):
    rows = scene.get('instances')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 128:
        raise ValueError('bounded scene instances required')
    if any(not isinstance(r, dict) or not isinstance(r.get('id'), str) or not r['id'] for r in rows):
        raise ValueError('scene instance identity required')
    if len({r['id'] for r in rows}) != len(rows):
        raise ValueError('duplicate scene instance identity')
    return {r['id']: r for r in rows}


def _physical_row(row):
    # source_transform, unknown fields and fact bindings remain physical inputs.
    return {k: v for k, v in row.items() if k != 'display_section'}


def physical_scene_identity(scene: dict) -> str:
    rows = _instances(scene)
    relations = scene.get('relations')
    if not isinstance(relations, list) or not 1 <= len(relations) <= 128:
        raise ValueError('bounded scene relations required')
    physical = {k: copy.deepcopy(v) for k, v in scene.items()
                if k not in _OBSERVATION | {'instances', 'relations'}}
    physical['instances'] = [_physical_row(rows[k]) for k in sorted(rows)]
    physical['relations'] = sorted(relations, key=canonical)
    return digest(physical)


def _section_allowed(row, contract):
    section = row.get('display_section')
    if section is None:
        return True
    if not isinstance(section, dict) or set(section) != {'convention_id', 'plane_y'}:
        return False
    plane = section['plane_y']
    if type(plane) not in (int, float) or not math.isfinite(plane):
        return False
    return any(c.get('id') == section['convention_id'] and c.get('kind') == 'local_section'
               and c.get('not_physical_feature') is True and row.get('part_id') in c.get('part_ids', [])
               for c in contract.get('conventions', []))


def validate_scene_derivation(parent_scene: dict, child_scene: dict, derivation: dict,
                              child_contract: dict) -> dict:
    try:
        if not isinstance(derivation, dict) or set(derivation) != {
                'parent_view_id', 'parent_scene_sha256', 'selected_instance_ids'}:
            raise ValueError('explicit parent scene derivation required')
        if not isinstance(derivation['parent_view_id'], str) or not derivation['parent_view_id']:
            raise ValueError('parent view required')
        parent_sha = physical_scene_identity(parent_scene)
        child_sha = physical_scene_identity(child_scene)
        if derivation['parent_scene_sha256'] != parent_sha:
            raise ValueError('actual parent physical scene identity differs')
        parent, child = _instances(parent_scene), _instances(child_scene)
        selected = derivation['selected_instance_ids']
        if (not isinstance(selected, list) or not selected or any(not isinstance(x, str) for x in selected)
                or len(set(selected)) != len(selected) or set(selected) != set(child) or not set(child) <= set(parent)):
            raise ValueError('child must be the declared subset of actual parent instances')
        globals_for = lambda s: {k: v for k, v in s.items() if k not in _OBSERVATION | {'instances', 'relations'}}
        if globals_for(parent_scene) != globals_for(child_scene):
            raise ValueError('child physical scene conventions differ')
        fact_ids = {f['id'] for f in child_contract.get('facts', []) if f.get('certainty') == 'confirmed'}
        for ident, row in child.items():
            geometry = lambda r: {k: v for k, v in r.items() if k not in {'display_section', 'fact_ids'}}
            if geometry(row) != geometry(parent[ident]):
                raise ValueError('child changes actual parent instance: ' + ident)
            # A local contract need not impose every whole-assembly fact on a
            # selected body. Retain exactly the original bindings in its scope.
            if (not isinstance(row.get('fact_ids'), list)
                    or set(row['fact_ids']) != set(parent[ident].get('fact_ids', [])) & fact_ids):
                raise ValueError('child changes in-scope source fact bindings: ' + ident)
            if not _section_allowed(row, child_contract):
                raise ValueError('child display section lacks bound nonphysical convention')
            if row.get('parent_id') and row['parent_id'] not in child:
                raise ValueError('child instance owner omitted')
        parent_relations = {canonical(r) for r in parent_scene['relations']}
        for relation in child_scene['relations']:
            if canonical(relation) not in parent_relations or relation.get('kind') not in _ENDPOINTS:
                raise ValueError('child relation differs from actual parent')
            for key in _ENDPOINTS[relation['kind']]:
                value = relation.get(key)
                values = value if isinstance(value, list) else [value]
                if not values or any(not isinstance(x, str) or x not in child for x in values):
                    raise ValueError('child relation endpoint omitted: ' + key)
        for row in [*child.values(), *child_scene['relations']]:
            facts = row.get('fact_ids')
            if not isinstance(facts, list) or not facts or not set(facts) <= fact_ids:
                raise ValueError('child source fact binding missing or unconfirmed')
        return {'status': 'PASS', 'parent_scene_sha256': parent_sha, 'child_scene_sha256': child_sha,
                'selected_instance_ids': sorted(selected), 'issues': []}
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        return {'status': 'FAIL', 'issues': [str(error)]}
