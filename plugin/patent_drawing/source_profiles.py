"""Preserve privately traced source pixels without reconstructing hidden CAD.

These bindings describe visible source segments. They do not authorize their
own use or assert new attachment geometry, dimensions, or complete outlines.
"""
import copy
from datetime import datetime
import json
import math
import io

from PIL import Image, ImageFilter

from .operation_store import digest
from .source_geometry import display_contact_box, validate_display_projection, validate_source_geometry
from .source_contract import compact


def registered_relation_convention_ids(registration):
    return sorted(row['convention_id'] for sides in (registration or {}).get('supports', {}).values()
                  for row in sides.values() if row.get('basis') == 'source_relation')


def relation_section_review_binding(guide, guide_id, expression, registration):
    convention_ids = registered_relation_convention_ids(registration)
    if not convention_ids:
        return None
    return {'source_sha256':guide['source_sha256'], 'plan_sha256':expression['required_plan_sha256'],
            'guide_id':guide_id, 'guide_png_sha256':guide['guide']['sha256'],
            'convention_ids':convention_ids, 'review_scope':'source_relation_guide_only'}


def validate_relation_section_review(raw, expected):
    message = 'professional relation guide review must match the exact source, plan and guide'
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 65536:
        raise ValueError(message)
    def unique_fields(pairs):
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise ValueError(message)
        return fields

    try:
        receipt = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_fields)
        if not isinstance(receipt, dict) or set(receipt) != set(expected) | {
                'decision', 'reviewer', 'reviewed_at', 'note'}:
            raise ValueError(message)
        if any(receipt[key] != value for key, value in expected.items()):
            raise ValueError(message)
        if receipt['decision'] != 'approved_for_reference' or not all(
                isinstance(receipt[key], str) and receipt[key].strip()
                for key in ('reviewer', 'reviewed_at', 'note')):
            raise ValueError(message)
        if datetime.fromisoformat(receipt['reviewed_at']).utcoffset() is None:
            raise ValueError(message)
    except (ValueError, OverflowError, RecursionError):
        raise ValueError(message) from None


def source_known_parts(bundle: dict, plan: dict, view: dict) -> list:
    region = next((r for r in bundle.get('figure_candidates', [])
                   if r['id'] == view.get('source_region_id')), None)
    page = next((p for p in bundle.get('pages', []) if region and p['id'] == region['page_id']), None)
    if not page:
        return []
    text = page.get('text', '')
    return [p['id'] for p in plan.get('parts', [])
            if p['id'] in view.get('parts', []) and p.get('name') and p['name'] in text]


def profile_bindings(scene: dict) -> list:
    return [{'instance_id': r['id'], 'part_id': r['part_id'],
             'source_region_id': r.get('source_region_id'),
             'source_image_sha256': r.get('source_image_sha256'),
             'source_asset_sha256': r.get('source_asset_sha256'),
             'source_paths_sha256': digest(r.get('source_paths')),
             'source_transform': copy.deepcopy(r.get('source_transform')),
             'approval_status': 'not_verified_here'}
            for r in scene.get('instances', []) if r.get('shape') == 'source_profile']


def validate_source_profiles(scene: dict, geometry: dict | None, bundle: dict, plan: dict, *, view=None,
                             expression_contract=None) -> dict:
    rows = [r for r in scene.get('instances', []) if r.get('shape') == 'source_profile']
    issues, bindings = [], []
    def fail(message):
        issues.append({'rule': 'SOURCE-PROFILE', 'verdict': 'FAIL', 'message': message})
    if rows and (not geometry or validate_source_geometry(geometry, bundle, plan)):
        fail('private current source geometry required for source profiles')
    if not issues:
        assets = {a['part_id']: a for a in (geometry or {}).get('assets', [])}
        for row in rows:
            asset = assets.get(row.get('part_id'))
            if (not asset or row.get('source_asset_sha256') != digest(asset)
                    or row.get('source_region_id') != asset['source_region_id']
                    or row.get('source_image_sha256') != asset['source_image_sha256']
                    or row.get('source_paths') != asset['visible_segments']):
                fail('source profile identity or partial paths changed: ' + str(row.get('id')))
                continue
            transform = row.get('source_transform')
            if (not isinstance(transform, dict) or set(transform) != {'scale', 'x', 'y'}
                    or any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 10000
                           for v in transform.values()) or not .05 <= transform['scale'] <= 20):
                fail('source profile permits positive scale/translation only: ' + str(row.get('id')))
                continue
            bindings.append({**profile_bindings({'instances': [row]})[0],
                             'outline_complete': asset['outline_complete']})
    registration = (geometry or {}).get('assembly_registration')
    needs_registration = any(r.get('role') in {'supported_plate', 'supporting_plate'} for r in rows)
    if needs_registration and registration is None:
        fail('shared source plates need private assembly registration')
    if registration is not None and not issues:
        try:
            verify_registered_scene(scene, rows, registration, view, plan, expression_contract)
        except (ValueError, KeyError, TypeError) as error:
            fail(str(error))
    return {'status': 'FAIL' if issues else 'PASS', 'bindings': bindings, 'issues': issues,
            'assembly_registration': copy.deepcopy(registration) if not issues else None}


def verify_registered_scene(scene, rows, registration, view, plan, expression_contract):
    if (view is None or registration['view_id'] != view['id']
            or registration['source_region_id'] != view.get('source_region_id')):
        raise ValueError('assembly registration does not belong to this view')
    if scene.get('panels') or scene.get('render_mode') != 'opaque_surfaces':
        raise ValueError('assembly registration requires a single opaque assembly projection')
    validate_display_projection(registration, scene)
    part_ids = registration['plate_part_ids']
    roles = {'upper': 'supported_plate', 'lower': 'supporting_plate', 'vertical': 'vertical_plate'}
    by_side = {}
    for side, role in roles.items():
        matches = [r for r in scene['instances'] if r.get('role') == role]
        if len(matches) != 1 or matches[0] not in rows or matches[0]['part_id'] != part_ids[side]:
            raise ValueError('assembly registration requires the same three source plate instances')
        row = by_side[side] = matches[0]
        if (row['source_transform'] != registration['transform']
                or row['source_region_id'] != registration['source_region_id']
                or row['source_image_sha256'] != registration['source_image_sha256']):
            raise ValueError('registered plates must share one source crop and transform')
    relations = [r for r in scene['relations'] if r.get('kind') == 'supports_at']
    if len(relations) != 1:
        raise ValueError('registered supports require one pair of common source plates')
    relation = relations[0]
    bodies = relation['subjects']
    if (any(relation[s] != by_side[s]['id'] for s in ('upper', 'lower'))
            or len(bodies) != 4 or set(bodies) != set(registration['supports'])
            or relation.get('count') != 4 or relation.get('four_corners') is not True):
        raise ValueError('registered supports must reach the same upper and lower plate at four corners')
    supports = [r for r in scene['instances'] if r.get('role') == 'support_body']
    if ({r['id'] for r in supports} != set(bodies)
            or any(r.get('shape') != 'sleeved_support' or r['part_id'] not in view['parts'] for r in supports)
            or len({r['part_id'] for r in supports}) != 1):
        raise ValueError('registered supports must retain the four source composite bodies')
    typed_evidence = [
        (e['page_id'], compact(e['quote']))
        for fact in (expression_contract or {}).get('facts', [])
        if fact['id'] in relation['fact_ids'] and fact.get('predicate') == 'supports_at'
        and fact.get('certainty') == 'confirmed' and fact['subject'] == part_ids['upper']
        and fact['object'] == part_ids['lower']
        for e in fact['evidence'] if compact(e['quote'])
    ]
    facts = {f['id']: f for f in plan['facts']}
    display = registration.get('display_projection')
    for support_id, sides in registration['supports'].items():
        for side, contact in sides.items():
            for ident in contact['fact_ids']:
                if not any(page == e['page_id'] and quote in compact(e['quote'])
                           for page, quote in typed_evidence for e in facts[ident]['evidence']):
                    raise ValueError('registered contact lacks evidence of the approved support relation')
            if contact.get('basis') == 'source_relation':
                convention = _registered_contact_convention(contact, part_ids[side], expression_contract)
                box = convention['region'] if convention else None
                width, height = scene['canvas']
                if (not box or not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height)
                        or 2 * (box[2] - box[0]) >= width or 2 * (box[3] - box[1]) >= height):
                    raise ValueError('relation-only support requires its own local nonphysical plate convention')
                if display:
                    window = display_contact_box(display['endpoints'][support_id][side])
                    if not all(box[i] <= window[i] < window[i+2] <= box[i+2] for i in (0, 1)):
                        raise ValueError('relation-only display endpoint must be inside its plate convention')
    registered_relation_sections(registration, scene, expression_contract)


def _registered_contact_convention(contact, plate_id, expression_contract):
    rows = [c for c in (expression_contract or {}).get('conventions', [])
            if c.get('id') == contact['convention_id']]
    if len(rows) != 1:
        return None
    row = rows[0]
    box = row.get('region')
    if (row.get('kind') in {'local_omission', 'local_relation_section'}
            and row.get('not_physical_feature') is True and plate_id in row.get('part_ids', [])
            and isinstance(box, list) and len(box) == 4
            and all(type(n) in (int, float) and math.isfinite(n) for n in box)):
        return row
    return None


def registered_relation_sections(registration, scene, expression_contract):
    """Compile short open break strokes in guide coordinates only."""
    if not registration:
        return []
    plates = registration['plate_part_ids']
    layout = registration.get('display_projection')
    cues = []
    for ident, sides in registration['supports'].items():
        for side, evidence in sides.items():
            if evidence.get('basis') != 'source_relation':
                continue
            part = plates[side]
            convention = _registered_contact_convention(evidence, part, expression_contract)
            if not convention or convention['kind'] != 'local_relation_section':
                continue
            box = convention['region']
            width, height = scene['canvas']
            if (not layout or convention['part_ids'] != [part]
                    or not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height)
                    or box[2]-box[0]>80 or box[3]-box[1]>36):
                raise ValueError('local relation section needs one registered plate and a bounded guide window')
            x,y = layout['endpoints'][ident][side]
            region = [math.floor(x-25),math.floor(y-5),math.ceil(x+26),math.ceil(y+6)]
            if not all(box[i] <= region[i] < region[i+2] <= box[i+2] for i in (0,1)):
                raise ValueError('local relation section strokes leave their registered guide window')
            for other, ends in layout['endpoints'].items():
                for end,point in ends.items():
                    if (other,end)==(ident,side):
                        continue
                    contact_box=display_contact_box(point)
                    if all(max(contact_box[i],region[i])<min(contact_box[i+2],region[i+2]) for i in (0,1)):
                        raise ValueError('local relation section overlaps another contact')
            cues.append({'part_id':part, 'support_id':ident, 'side':side,
                         'convention_id':convention['id'], 'kind':'local_relation_section',
                         'not_physical_feature':True, 'source_box':None, 'region':region,
                         'stroke':[(x+a,y+b) for a,b in ((-24,0),(-18,0),(-15,-4),(-12,4),(-9,0),
                                                          (9,0),(12,-4),(15,4),(18,0),(24,0))]})
    if cues and (len(cues)!=2 or {c['side'] for c in cues}!={'upper','lower'}
                 or len({c['support_id'] for c in cues})!=1
                 or len({c['convention_id'] for c in cues})!=2
                 or all(max(cues[0]['region'][i],cues[1]['region'][i])<
                        min(cues[0]['region'][i+2],cues[1]['region'][i+2]) for i in (0,1))):
        raise ValueError('local relation section requires two distinct cues for the same support')
    return cues


def check_registered_contacts(registration, guide, *, expression_contract=None):
    """Qualify eight independently visible contacts, keeping source and guide coordinates separate."""
    if registration is None:
        return []
    display = registration.get('display_projection')
    if display and (guide.get('display_projection_sha256') != digest(display)
                    or guide.get('scene_sha256') != display['scene_sha256']):
        return [{'rule': 'GUIDE-REGISTERED-CONTACT', 'verdict': 'FAIL', 'evidence_regions': [],
                 'message': 'display projection receipt differs from the registered scene and endpoints'}]
    transform = registration['transform']
    source_parts = registration['plate_part_ids']
    plate_ids = {r['part_id']: r['instance_id'] for r in guide.get('regions', [])
                 if r.get('part_id') in source_parts.values()}
    contacts = [r for r in guide.get('contact_regions', []) if r.get('kind') == 'supports_at']
    issues = []
    seen = set()
    for ident, sides in registration['supports'].items():
        for side, ref in sides.items():
            common = plate_ids.get(source_parts[side])
            pair = [ident, common]
            rows = [r for r in contacts if r.get('instance_ids') == pair and r.get('side') == side]
            region = rows[0].get('region', []) if len(rows) == 1 else []
            if ref.get('basis') == 'source_relation':
                convention = _registered_contact_convention(ref, source_parts[side], expression_contract)
                box = convention['region'] if convention else None
                kind = convention['kind'] if convention else None
                rendered = next((c for c in guide.get('display_conventions', [])
                                 if c.get('convention_id') == ref['convention_id']
                                 and c.get('instance_id') == common and c.get('part_id') == source_parts[side]
                                 and c.get('kind') == kind and c.get('not_physical_feature') is True), {})
                if kind == 'local_relation_section':
                    omission = rendered.get('region', []) if (
                        rendered.get('support_id') == ident and rendered.get('side') == side
                        and 'source_box' in rendered and rendered['source_box'] is None) else []
                else:
                    omission = rendered.get('omission_region', [])
                if not (box and len(omission) == 4 and all(
                        box[i] <= omission[i] < omission[i+2] <= box[i+2] for i in (0, 1))):
                    box = None
            else:
                box = [transform[axis] + transform['scale'] * coordinate
                       for axis, coordinate in zip(('x', 'y', 'x', 'y'), ref['source_box'])]
            if (common and box and len(region) == 4 and tuple(region) not in seen
                    and (not display or region == display_contact_box(display['endpoints'][ident][side]))
                    and rows[0].get('visible') is True
                    and set(rows[0].get('ink_pixels', {})) == set(pair)
                    and all(n > 0 for n in rows[0]['ink_pixels'].values())
                    and all(box[i] <= region[i] < region[i+2] <= box[i+2] for i in (0, 1))):
                seen.add(tuple(region))
                continue
            issues.append({'rule': 'GUIDE-REGISTERED-CONTACT', 'verdict': 'UNKNOWN',
                           'evidence_regions': [region] if region else [],
                           'message': f'{ident}/{side} lacks distinct paired final ink in its approved local region'})
    return issues


def trace_ink_check(blob: bytes, paths: list, *, registered=False) -> dict:
    """Pixel proximity only, not semantic or professional contour acceptance."""
    sampled_paths = []
    total = 0
    try:
        for path in paths:
            samples = []
            for a, b in zip(path, path[1:]):
                count = max(1, math.ceil(math.dist(a, b) / (1 if registered else 2)))
                total += count + 1
                if total > 20000:
                    return {'status': 'FAIL', 'matched_fraction': 0, 'reason': 'trace sampling capacity'}
                samples.extend((round(a[0]+(b[0]-a[0])*i/count), round(a[1]+(b[1]-a[1])*i/count))
                               for i in range(count+1))
            sampled_paths.append(samples)
        with Image.open(io.BytesIO(blob)) as im:
            if im.width * im.height > 16_000_000:
                return {'status': 'FAIL', 'matched_fraction': 0, 'reason': 'source image capacity'}
            ink = im.convert('L').filter(ImageFilter.MinFilter(3 if registered else 7))
            cutoff = 145 if registered else 216
            matches = [sum(0 <= x < ink.width and 0 <= y < ink.height and ink.getpixel((x, y)) < cutoff
                           for x, y in samples) for samples in sampled_paths]
    except (ValueError, TypeError, KeyError, OSError):
        return {'status': 'FAIL', 'matched_fraction': 0, 'reason': 'source trace invalid'}
    fraction = sum(matches) / max(1, total)
    result = {'status': 'PASS' if total and fraction >= .8 else 'FAIL',
              'matched_fraction': fraction, 'samples': total,
              'scope': 'source crop pixel proximity; source shape/identity still requires private review'}
    if registered:
        fractions = [matched / max(1, len(samples)) for matched, samples in zip(matches, sampled_paths)]
        result.update(status='PASS' if fractions and min(fractions) >= .96 else 'FAIL',
                      path_match_fractions=fractions)
    return result
