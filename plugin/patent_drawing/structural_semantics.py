"""Bounded geometry of a proposed schematic; never proof about generated pixels."""
import math

SHAPES = {
    'supported_plate': {'omitted_surface', 'source_segments', 'source_profile'},
    'supporting_plate': {'omitted_surface', 'source_segments', 'source_profile'},
    'support_body': {'closed_sleeve','sleeved_support'}, 'busbar_body': {'solid'},
    'pcb': {'sectioned_board'}, 'integral_pin': {'cylinder'},
    'deformable_rib': {'rib_band', 'axial_rib'}, 'plated_hole': {'plated_bore'},
    'external_wall': {'wall'}, 'partition_wall': {'wall'},
    'signal_wire': {'wire'}, 'shield': {'wire','cable_band'}, 'drain_wire': {'wire'},
    'rear_trough': {'wall'},
    'shielded_cable': {'cable_shell'}, 'weld_opening': {'access_gap'},
    'source_outline': {'source_profile'}, 'vertical_plate': {'source_profile'},
}
KINDS = {'supports_at', 'channel_partition', 'integral_pin', 'rib_in_plated_hole', 'conductive_path',
         'source_identity','cable_weld_access','trough_shield_contact'}


def finite(value):
    return type(value) in (int, float) and math.isfinite(value) and abs(value) <= 10000


def validate_structure(scene: dict, guide: dict, contract: dict, *, member_requirements=None) -> dict:
    checks = []
    def check(rule, condition, message, ids=(), verdict='FAIL'):
        checks.append({'rule': rule, 'verdict': 'PASS' if condition else verdict,
                       'message': message, 'evidence_regions': list(ids)})
    def finish():
        status = 'FAIL' if any(c['verdict'] == 'FAIL' for c in checks) else 'UNKNOWN' if any(c['verdict'] == 'UNKNOWN' for c in checks) else 'PASS'
        return {'status': status, 'checks': checks}
    if member_requirements is not None:
        from .member_visibility import member_context_matches
        if not member_context_matches(member_requirements, scene, contract):
            check('STRUCTURE-MEMBER-CONTEXT', False, 'member observation context differs from actual geometry/contract')
            return finish()
    rows = scene.get('instances', []); relations = scene.get('relations', [])
    if (scene.get('schema_version') != 1 or scene.get('coordinate_space') != 'illustrative_units'
            or not isinstance(rows, list) or not 1 <= len(rows) <= 120
            or not isinstance(relations, list) or len(relations) > 160):
        check('STRUCTURE-SCHEMA', False, 'bounded illustrative scene required'); return finish()
    facts = {f['id']: f for f in contract.get('facts', []) if f.get('certainty') == 'confirmed'}
    instances = {}
    for row in rows:
        if not isinstance(row, dict): check('STRUCTURE-INSTANCE', False, 'instance object required'); continue
        ident = row.get('id'); b = row.get('bounds'); role = row.get('role')
        valid = (isinstance(ident, str) and ident and ident not in instances and isinstance(b, list)
                 and len(b) == 6 and all(finite(x) for x in b) and all(b[i] < b[i+3] for i in range(3)))
        check('STRUCTURE-INSTANCE', valid, 'distinct finite spatial instance', [ident] if isinstance(ident, str) else [])
        if not valid: continue
        instances[ident] = row
        check('STRUCTURE-ROLE', row.get('shape') in SHAPES.get(role, set()), 'role requires actual allowed geometry', [ident])
        ids = row.get('fact_ids', [])
        check('STRUCTURE-SOURCE', isinstance(ids, list) and bool(ids) and not set(ids)-facts.keys(), 'instance has reviewed source obligations', [ident])
        check('STRUCTURE-CONTACT', not row.get('omit_contacts'), 'critical contacts cannot be omitted', [ident])
        if row.get('shape') == 'sleeved_support':
            from .support_assembly import validate_support_assembly
            parts = {v for f in facts.values() for v in (f.get('subject'), f.get('object'))} | {row['part_id']}
            composite = validate_support_assembly(row, contract, list(parts))
            check('STRUCTURE-SUPPORT-COMPOSITE', composite['status'] == 'PASS',
                  'source exterior members and connected endpoints required: '+str(composite.get('issues', [])), [ident])
        section=row.get('display_section')
        if section is not None:
            valid_section=(isinstance(section,dict)and set(section)=={'plane_y','convention_id'}
                           and row['shape']=='solid'and finite(section.get('plane_y'))
                           and b[1]<section['plane_y']<b[4]
                           and any(c.get('id')==section['convention_id']and c.get('kind')=='local_section'
                                   and c.get('not_physical_feature')is True and row['part_id']in c.get('part_ids',[])
                                   for c in contract.get('conventions',[])))
            check('STRUCTURE-DISPLAY-SECTION',valid_section,'local display cut requires an explicit bound nonphysical convention',[ident])
        omission=row.get('display_omission')
        if omission is not None:
            convention=next((c for c in contract.get('conventions',[])
                             if isinstance(omission,dict)and c.get('id')==omission.get('convention_id')),None)
            body_break = (isinstance(omission, dict) and set(omission) == {'convention_id'}
                          and role == 'busbar_body' and row['shape'] == 'solid')
            profile_break = (isinstance(omission, dict)
                             and set(omission) == {'convention_id', 'path_index', 'segment_index', 'fraction'}
                             and role in {'supported_plate', 'supporting_plate'} and row['shape'] == 'source_profile'
                             and type(omission['path_index']) is int and omission['path_index'] >= 0
                             and type(omission['segment_index']) is int and omission['segment_index'] >= 0
                             and finite(omission['fraction']) and .05 <= omission['fraction'] <= .95)
            valid_omission=((body_break or profile_break)
                            and convention is not None and convention.get('kind')=='local_omission'
                            and convention.get('not_physical_feature')is True
                            and row['part_id']in convention.get('part_ids',[]))
            check('STRUCTURE-DISPLAY-OMISSION',valid_omission,
                  'unknown body extent uses an approved nonphysical boundary, never a new physical gap',[ident])
        if row.get('shape') in {'closed_sleeve','cylinder','rib_band','plated_bore'}:
            check('STRUCTURE-CROSS-SECTION',abs((b[3]-b[0])-(b[4]-b[1]))<1e-7,
                  'circular rendering must match both geometric extents',[ident])
        if row.get('shape') == 'omitted_surface':
            permitted = any(c.get('kind') == 'local_omission' and c.get('not_physical_feature') is True
                            and row.get('part_id') in c.get('part_ids', []) for c in contract.get('conventions', []))
            check('STRUCTURE-OMISSION', permitted and row.get('outline_complete') is False,
                  'unknown perimeter uses explicit nonphysical omission, never closed outline', [ident])
        if row.get('shape') in {'wall', 'wire', 'source_segments'}:
            path = row.get('path')
            valid_path = (isinstance(path, list) and 2 <= len(path) <= 120
                          and all(isinstance(p, list) and len(p) == 3 and all(finite(v) for v in p) for p in path))
            check('STRUCTURE-PATH', valid_path, 'actual continuous 3D path required', [ident])
            if row.get('shape') == 'wall':
                check('STRUCTURE-WALL', finite(row.get('thickness')) and row['thickness'] > 0
                      and finite(row.get('height')) and row['height'] > 0, 'wall has material thickness and height', [ident])
    if any(c['verdict'] == 'FAIL' for c in checks): return finish()
    obligations = {f.get('predicate') for f in facts.values()}
    present = {r.get('kind') for r in relations if isinstance(r, dict)}
    member_facts = {m['fact_id'] for row in rows if row.get('shape') == 'sleeved_support' for m in row['members']}
    member_kinds={'assembly_mount_plate','assembly_moving_rod','assembly_closed_housing','assembly_lower_stem'}
    present.update(facts[fid]['predicate'] for fid in member_facts if fid in facts)
    for kind in obligations:
        check('STRUCTURE-OBLIGATION', (kind in KINDS or kind in member_kinds) and kind in present,
              'source relation must have a geometric witness: '+str(kind), verdict='UNKNOWN')
    for fact in facts.values():
        if fact.get('predicate') in member_kinds:
            check('STRUCTURE-SOURCE-MEMBER', fact['id'] in member_facts,
                  'source assembly fact has an actual external member', verdict='UNKNOWN')
    if any(u.get('critical') for u in contract.get('unknowns', [])):
        check('STRUCTURE-UNKNOWN', False, 'critical source unknown remains', verdict='UNKNOWN')
    def touch(a, b):
        return all(min(a[i+3], b[i+3]) >= max(a[i], b[i])-1e-7 for i in range(3))
    def center(b, i): return (b[i]+b[i+3])/2
    participated={ident:set()for ident in instances}
    for rel in relations:
        if not isinstance(rel, dict): check('STRUCTURE-RELATION', False, 'relation required'); continue
        kind = rel.get('kind'); refs = []
        for k in ('subjects', 'walls', 'partitions', 'wires', 'ribs', 'path','troughs'):
            refs += rel.get(k, [])
        refs += [rel[k] for k in ('upper', 'lower', 'body', 'pin', 'hole', 'board','cable','shield','opening','target') if k in rel]
        if any(i not in instances for i in refs):
            check('STRUCTURE-REFERENCE', False, 'relation references absent instance', refs); continue
        check('STRUCTURE-RELATION-SOURCE', bool(rel.get('fact_ids')) and not set(rel.get('fact_ids', []))-facts.keys(), 'relation source references', refs)
        typed=[facts[fid]for fid in rel.get('fact_ids',[])if fid in facts and facts[fid].get('predicate')==kind]
        check('STRUCTURE-TYPED-RELATION',bool(typed),'relation must match its source predicate',refs)
        for ident in refs:participated[ident].update(f['id']for f in typed)
        if kind == 'supports_at':
            bodies = rel.get('subjects', []); upper = instances[rel['upper']]; lower = instances[rel['lower']]
            ub, lb = upper['bounds'], lower['bounds']; corners = set()
            check('STRUCTURE-COUNT', len(bodies) == len(set(bodies)) == rel.get('count'), 'source-confirmed count', bodies)
            check('STRUCTURE-PLATES', upper['role'] == 'supported_plate' and lower['role'] == 'supporting_plate', 'upper/lower ownership', [rel['upper'], rel['lower']])
            check('STRUCTURE-OWNERSHIP',any(f.get('subject')==upper['part_id'] and f.get('object')==lower['part_id']for f in typed),
                  'source identifies the actual upper and lower parts',[rel['upper'],rel['lower']])
            for ident in bodies:
                row = instances[ident]; b = row['bounds']; x, y = center(b, 0), center(b, 1)
                corners.add((x > center(ub, 0), y > center(ub, 1)))
                check('STRUCTURE-SUPPORTS', row['role'] == 'support_body' and abs(b[2]-lb[5]) < 1e-7
                      and abs(b[5]-ub[2]) < 1e-7
                      and all(surface[i] < center(b, i) < surface[i+3] for surface in (ub, lb) for i in (0, 1)),
                      'closed body reaches both specified support surfaces', [ident, rel['upper'], rel['lower']])
                if rel.get('four_corners'):
                    check('STRUCTURE-CORNER-ZONE',all(abs(center(b,i)-center(ub,i))>=(ub[i+3]-ub[i])*.25 for i in (0,1)),
                          'four-corner witness occupies separated outer corner zones, not central quadrants',[ident])
            if rel.get('four_corners'): check('STRUCTURE-CORNERS', len(corners) == 4, 'four independent corner zones', bodies)
            for i,left in enumerate(bodies):
                a=instances[left]['bounds']
                for right in bodies[i+1:]:
                    b=instances[right]['bounds'];distance=math.hypot(center(a,0)-center(b,0),center(a,1)-center(b,1))
                    check('STRUCTURE-SUPPORT-SEPARATION',distance>=(a[3]-a[0]+b[3]-b[0])/2,
                          'independent support bodies cannot interpenetrate',[left,right])
        elif kind == 'channel_partition':
            wall_ids = rel.get('walls', []) + rel.get('partitions', []); wires = rel.get('wires', [])
            walls = [instances[i] for i in wall_ids]
            check('STRUCTURE-CHANNEL', len(rel.get('walls', [])) >= 2 and bool(rel.get('partitions')) and bool(wires)
                  and all(r['shape'] == 'wall' for r in walls)
                  and all(instances[i]['role'] == 'partition_wall' for i in rel.get('partitions', []))
                  and all(instances[i]['role'] == 'external_wall' for i in rel.get('walls', [])), 'solid outside and partition walls', wall_ids)
            body=instances.get(rel.get('body'))
            check('STRUCTURE-CHANNEL-OWNER',body is not None and body['role']=='busbar_body'
                  and any(f.get('subject')in {r['part_id']for r in walls}|{body['part_id']}for f in typed),
                  'walls belong to the source-identified body',wall_ids)
            monotone=all(all(b[0]>a[0]for a,b in zip(w['path'],w['path'][1:]))
                         or all(b[0]<a[0]for a,b in zip(w['path'],w['path'][1:]))for w in walls)
            check('STRUCTURE-CHANNEL-PROOF',monotone,'folded or ambiguous wall path needs a stronger geometric witness',wall_ids,verdict='UNKNOWN')
            def edge_at(wall, x):
                for a, b in zip(wall['path'], wall['path'][1:]):
                    if min(a[0], b[0]) <= x <= max(a[0], b[0]) and a[0] != b[0]:
                        t=(x-a[0])/(b[0]-a[0])
                        y_half=wall['thickness']/2*math.hypot(b[0]-a[0],b[1]-a[1])/abs(b[0]-a[0])
                        return (a[1]+(b[1]-a[1])*t,a[2]+(b[2]-a[2])*t,y_half,wall['height'],wall['id'])
                return None
            for ident in wires:
                wire=instances[ident];inside=wire['role']=='signal_wire';cell=None
                for a,b in zip(wire.get('path',[]),wire.get('path',[])[1:]):
                    cuts={0.,1.}
                    if b[0]!=a[0]:
                        cuts.update((p[0]-a[0])/(b[0]-a[0])for wall in walls for p in wall['path']if 0<(p[0]-a[0])/(b[0]-a[0])<1)
                    for t in sorted(cuts):
                        p=[a[i]+(b[i]-a[i])*t for i in range(3)]
                        edges=sorted(x for wall in walls if (x:=edge_at(wall,p[0]))is not None)
                        found=[(l[4],r[4])for l,r in zip(edges,edges[1:])
                               if l[0]+l[2]<p[1]<r[0]-r[2] and max(l[1],r[1])<=p[2]<=min(l[1]+l[3],r[1]+r[3])]
                        if not found:inside=False;continue
                        if cell is None:cell=found[0]
                        if cell not in found:inside=False
                check('STRUCTURE-CHANNEL-WIRE',inside and monotone,'entire continuous wire stays in one three-dimensional channel', [ident]+wall_ids)
        elif kind == 'integral_pin':
            body, pin = instances[rel['body']], instances[rel['pin']]
            b, p = body['bounds'], pin['bounds']
            check('STRUCTURE-INTEGRAL-PIN', body['role'] == 'busbar_body' and pin['role'] == 'integral_pin'
                  and touch(b,p) and p[2] < b[2] and p[5] >= b[2], 'pin extends directly from busbar body', [rel['body'], rel['pin']])
            check('STRUCTURE-OWNERSHIP',any(f.get('subject')==pin['part_id'] and f.get('object')==body['part_id']for f in typed),
                  'source pin identity cannot be replaced by the body identity',[rel['body'],rel['pin']])
        elif kind == 'rib_in_plated_hole':
            pin, hole, board = (instances[rel[k]] for k in ('pin','hole','board'))
            p, h, bd = pin['bounds'], hole['bounds'], board['bounds']; inner = hole.get('inner_radius')
            valid = (pin['role']=='integral_pin' and hole['role']=='plated_hole' and board['role']=='pcb'
                     and finite(inner) and 0 < inner < (h[3]-h[0])/2
                     and abs(h[2]-bd[2])<1e-7 and abs(h[5]-bd[5])<1e-7
                     and all(bd[i]<=h[i]<h[i+3]<=bd[i+3]for i in (0,1))
                     and all(abs(center(p,i)-center(h,i))<1e-7 for i in (0,1)))
            check('STRUCTURE-PLATED-HOLE', valid, 'actual plated bore in PCB, pin on bore axis', [rel['pin'],rel['hole'],rel['board']])
            check('STRUCTURE-RIBS', bool(rel.get('ribs')), 'insertion ribs required', rel.get('ribs', []))
            check('STRUCTURE-OWNERSHIP',all(any(f.get('subject')==instances[ident]['part_id'] and f.get('object')==hole['part_id']for f in typed)
                  for ident in rel.get('ribs',[])), 'source identifies rib and plated-hole parts',rel.get('ribs',[])+[rel['hole']])
            for ident in rel.get('ribs', []):
                row=instances[ident]; r=row['bounds']; radius=(r[3]-r[0])/2
                if row['shape']=='axial_rib':
                    fp=row.get('footprint',[])
                    geometry=(row.get('parent_id')==rel['pin'] and isinstance(fp,list)and 3<=len(fp)<=12
                              and all(isinstance(point,list)and len(point)==2 and all(finite(v)for v in point)
                                      and r[0]<=point[0]<=r[3] and r[1]<=point[1]<=r[4]for point in fp))
                    radii=[math.hypot(x-center(p,0),y-center(p,1))for x,y in fp]if geometry else[]
                    connected=geometry and min(radii)<=(p[3]-p[0])/2 and max(radii)>=inner if valid else False
                    # All vertices occupy one side of the axis: this is a ridge,
                    # not an annular washer disguised under another role.
                    one_side=geometry and (all(x>center(p,0)for x,y in fp)or all(x<center(p,0)for x,y in fp)
                                           or all(y>center(p,1)for x,y in fp)or all(y<center(p,1)for x,y in fp))
                    check('STRUCTURE-RIB-CONTACT',valid and connected and one_side
                          and max(radii)<(h[3]-h[0])/2 and h[2]<=r[2]<r[5]<=h[5]
                          and p[2]<=r[2]<r[5]<=p[5], 'axial ridge joins pin and contacts in-hole plating', [ident,rel['pin'],rel['hole']])
                    continue
                check('STRUCTURE-RIB-CONTACT', valid and row['role']=='deformable_rib'
                      and h[2] <= r[2] < r[5] <= h[5] and p[2] <= r[2] < r[5] <= p[5]
                      and all(abs(center(r,i)-center(h,i))<1e-7 for i in (0,1))
                      and inner <= radius < (h[3]-h[0])/2,
                      'rib is on pin insertion segment and meets plated inner wall', [ident,rel['pin'],rel['hole']])
        elif kind == 'trough_shield_contact':
            cable,shield=instances[rel['cable']],instances[rel['shield']];troughs=[instances[i]for i in rel.get('troughs',[])]
            cb,sb=cable['bounds'],shield['bounds']
            valid=(cable['role']=='shielded_cable'and shield['role']=='shield'and bool(troughs)
                   and all(t['role']=='rear_trough'for t in troughs)
                   and all(abs(center(cb,i)-center(sb,i))<1e-7 for i in (1,2))and touch(cb,sb)
                   and any(touch(sb,t['bounds'])for t in troughs)
                   and any(f.get('subject')==shield['part_id']and all(f.get('object')==t['part_id']for t in troughs)for f in typed))
            check('STRUCTURE-SHIELD-TROUGH',valid,'source shield layer contacts its own rear trough; optional welding target does not remove shield',refs)
        elif kind == 'cable_weld_access':
            body,opening,target=(instances[rel[k]]for k in ('body','opening','target'))
            ob,bb=opening['bounds'],body['bounds'];walls=[instances[i]for i in rel.get('troughs',[])]
            path=rel.get('access_path',[])
            valid_path=(isinstance(path,list)and 2<=len(path)<=12 and all(isinstance(p,list)and len(p)==3 and all(finite(v)for v in p)for p in path))
            def in_box(point,b):return all(b[i]-1e-7<=point[i]<=b[i+3]+1e-7 for i in range(3))
            gap=(opening['role']=='weld_opening'and opening['shape']=='access_gap'and body['role']=='busbar_body'
                 and target['role']in {'shield','drain_wire'}and bool(walls)
                 and all(w['role']=='rear_trough'for w in walls)
                 and bb[0]<=ob[0]<ob[3]<=bb[3]and bb[1]<=ob[1]<ob[4]<=bb[4]and ob[2]>=bb[5]
                 and any(abs(w['path'][-1][0]-ob[0])<1e-7 and ob[1]<=w['path'][-1][1]<=ob[4]for w in walls)
                 and any(abs(w['path'][0][0]-ob[3])<1e-7 and ob[1]<=w['path'][0][1]<=ob[4]for w in walls))
            def segment_hits(a,b,box):
                lo,hi=0.,1.
                for i in range(3):
                    d=b[i]-a[i]
                    if abs(d)<1e-10:
                        if a[i]<box[i]or a[i]>box[i+3]:return False
                    else:
                        left,right=sorted(((box[i]-a[i])/d,(box[i+3]-a[i])/d));lo=max(lo,left);hi=min(hi,right)
                        if lo>hi:return False
                return True
            clear=valid_path
            if valid_path:
                uncertain=[]
                for obstacle in rows:
                    shape=obstacle['shape']
                    if shape in {'access_gap','source_profile'}:continue  # Void/display evidence, no physical blocker.
                    segments=list(zip(path,path[1:]))
                    if obstacle['id']==target['id']:segments=segments[:-1]  # Explicit terminal weld contact only.
                    if shape in {'solid','sectioned_board'}:
                        if any(segment_hits(x,y,obstacle['bounds'])for x,y in segments):clear=False
                    elif shape=='wall':
                        for a,b in zip(obstacle['path'],obstacle['path'][1:]):
                            dx,dy=b[0]-a[0],b[1]-a[1];length=math.hypot(dx,dy)
                            if length==0 or abs(a[2]-b[2])>1e-7:
                                uncertain.append(obstacle['id']);continue
                            def local(p):return [(dx*(p[0]-a[0])+dy*(p[1]-a[1]))/length,
                                                (-dy*(p[0]-a[0])+dx*(p[1]-a[1]))/length,p[2]-a[2]]
                            box=[0,-obstacle['thickness']/2,0,length,obstacle['thickness']/2,obstacle['height']]
                            if any(segment_hits(local(x),local(y),box)for x,y in segments):clear=False
                    else:
                        # An unsupported curved object is uncertain only if the
                        # approach enters its interior envelope. A shared final
                        # shield/drain boundary is an allowed terminal contact.
                        b=obstacle['bounds'];interior=[b[i]+1e-7 for i in range(3)]+[b[i]-1e-7 for i in range(3,6)]
                        if any(segment_hits(x,y,interior)for x,y in segments):uncertain.append(obstacle['id'])
                check('STRUCTURE-CABLE-ACCESS-CLEARANCE',not uncertain,
                      'unresolved obstacle geometry cannot prove access clearance',uncertain,verdict='UNKNOWN')
            check('STRUCTURE-CABLE-ACCESS',gap and valid_path and in_box(path[-1],target['bounds'])
                  and not in_box(path[0],bb)and any(in_box(p,ob)for p in path[1:-1])and clear
                  and any(f.get('subject')==target['part_id']and f.get('object')==body['part_id']for f in typed),
                  'actual cable-side gap permits a clear approach to the source-selected weld object',refs)
        elif kind == 'source_identity':
            check('STRUCTURE-SOURCE-IDENTITY', bool(refs) and all(
                instances[i]['shape'] == 'source_profile'
                and any(f.get('subject') == f.get('object') == instances[i]['part_id'] for f in typed)
                for i in refs), 'source profile represents its own stable part only; no new attachment claim', refs)
        elif kind == 'conductive_path':
            path=rel.get('path', []); roles=[instances[i]['role'] for i in path]
            check('STRUCTURE-CONDUCTIVE-PATH', roles==['busbar_body','integral_pin','plated_hole']
                  and all(touch(instances[a]['bounds'],instances[b]['bounds']) for a,b in zip(path,path[1:])),
                  'busbar/pin/plated-hole continuity requires separate rib contact check', path)
            check('STRUCTURE-OWNERSHIP',bool(path)and any(f.get('subject')==instances[path[0]]['part_id'] and f.get('object')==instances[path[-1]]['part_id']for f in typed),
                  'source identifies both conductive path endpoints',path)
        else:
            check('STRUCTURE-RELATION', False, 'unsupported geometric relation '+str(kind), refs, verdict='UNKNOWN')
    check('STRUCTURE-EXTRA-ENTITY', sum(r['role']=='busbar_body' for r in rows) <= 1,
          'extra body/ear cannot hide behind repeated busbar identity')
    for ident,row in instances.items():
        check('STRUCTURE-ENTITY-CLOSURE',bool(set(row['fact_ids'])&participated[ident]),
              'every rendered entity participates in its typed source/ownership relations',[ident])
    regions = {r['instance_id']:r for r in guide.get('regions', [])}
    if scene.get('render_mode')=='opaque_surfaces'and regions:
        panel_ids=set(guide.get('panel_ids', [None]))
        for rel in relations:
            if rel['kind']=='channel_partition':
                check('STRUCTURE-PROJECTED-CHANNEL',any(
                    row.get('visible') is True and row.get('panel_id') in panel_ids
                    and row.get('wire_crossings')==0
                    and set(row.get('partitions', {}))==set(rel['partitions'])
                    and set(row.get('wires', {}))==set(rel['wires'])
                    and all(row['wires'].values())
                    and all(len(x.get('visible_edges', []))==2 and x.get('min_separation_px', 0)>=3
                            for x in row['partitions'].values())
                    for row in guide.get('projected_channels', [])),
                    'one final observation must retain two distinct solid partition edges and each channel wire',
                    rel['partitions']+rel['wires'],verdict='UNKNOWN')
            if rel.get('kind')!='rib_in_plated_hole':continue
            fit_ids=[rel['pin'],rel['hole'],rel['board']]
            check('STRUCTURE-PROJECTED-PIN-FIT',any(
                row.get('visible') is True and row.get('panel_id') in panel_ids
                and [row.get(k) for k in ('pin_id','hole_id','board_id')]==fit_ids
                and row.get('insertion_region')
                and row.get('insertion_pin_traces')
                and all(row.get('observations', {}).get(k) for k in fit_ids)
                and any(rib in rel['ribs'] and row['observations'].get(rib)
                        for rib in row.get('visible_contact_ribs', []))
                for row in guide.get('projected_pin_fits', [])),
                'one final bore window must retain the insertion pin, PCB, plating and visible rib contact',
                fit_ids+rel['ribs'],verdict='UNKNOWN')
            for rib in rel['ribs']:
                pair=[rib,rel['hole']]
                check('STRUCTURE-PROJECTED-FIT-CONTACT',any(
                    r.get('kind')=='rib_in_plated_hole'and r.get('instance_ids')==pair
                    and r.get('visible')is True for r in guide.get('contact_regions',[])),
                    'rib and plated wall must retain adjacent actual ink at their contact in one delivered observation',
                    pair,verdict='UNKNOWN')
    for ident, row in instances.items():
        r = regions.get(ident, {})
        if row.get('display_omission')and r.get('ink_bbox'):
            convention=next(c for c in contract['conventions']if c.get('id')==row['display_omission']['convention_id'])
            area=convention.get('region',[]);box=r['ink_bbox']
            if row['shape'] == 'source_profile':
                matches = [c for c in guide.get('display_conventions', []) if c['instance_id'] == ident]
                box = matches[0].get('omission_region', []) if len(matches) == 1 else []
            check('STRUCTURE-DISPLAY-OMISSION',len(area)==len(box)==4 and all(area[i]<=box[i]for i in (0,1))
                  and all(box[i]<=area[i]for i in (2,3)), 'actual omitted body observation must stay within approved region',[ident])
        check('STRUCTURE-PROJECTED-WITNESS', r.get('role')==row['role'] and r.get('ink_pixels',0)>0
              and r.get('visible_fraction',0)>=.3, 'actual projected component ink required', [ident], verdict='UNKNOWN')
        if row['role']=='support_body':
            if row['shape']=='sleeved_support':
                required={m['part_id'] for m in row['members']}
                policy=(member_requirements or {}).get('instances',{}).get(ident,{})
                optional=set(policy.get('occludable_member_part_ids',[]))
                # This remains fail-closed even for an internal malformed profile.
                rods={m['part_id']for m in row['members']if m['role']=='moving_rod'}
                valid_policy=(optional<=rods and (not policy or
                    set(policy.get('required_member_part_ids',[]))==required-optional))
                check('STRUCTURE-MEMBER-POLICY',valid_policy,'only unrequested moving rod may have an observation exception',[ident])
                if not valid_policy:optional=set()
                retained={m.get('part_id') for m in r.get('member_regions', [])
                          if m.get('ink_pixels', 0)>0 and m.get('visible_fraction', 0)>=.3}
                mount=next(m['part_id']for m in row['members']if m['role']=='mount_plate')
                occluded={m.get('part_id')for m in r.get('member_regions',[])
                          if m.get('part_id')in optional and m.get('projection_fully_retained')is True
                          and m.get('occluded_fraction_by_part',{}).get(mount,0)>=.7}
                check('STRUCTURE-PROJECTED-MEMBERS', required<=retained|occluded,
                      'required members need final ink; optional rod needs own mounting-plate occlusion proof', [ident], verdict='UNKNOWN')
                endpoints={m['role']:m['part_id'] for m in row['members']}
                check('STRUCTURE-PROJECTED-END-MEMBERS',r.get('contact_members')=={
                    'upper':endpoints['mount_plate'],'lower':endpoints['lower_stem']},
                    'interfaces belong to actual plate and lower stem, not housing envelope',[ident],verdict='UNKNOWN')
            check('STRUCTURE-PROJECTED-CONTACT', set(r.get('contacts', {}))=={'upper','lower'}
                  and all(r.get('contacts_visible', {}).get(k) is True for k in ('upper','lower')),
                  'both contact regions must contain component ink', [ident], verdict='UNKNOWN')
            contacts=r.get('contacts',{});a=contacts.get('upper',[]);b=contacts.get('lower',[])
            separated=(len(a)==len(b)==4 and (a[2]<=b[0]or b[2]<=a[0]or a[3]<=b[1]or b[3]<=a[1]))
            check('STRUCTURE-DISTINCT-INTERFACES',separated,'upper and lower interfaces need independently visible regions, not one dot',
                  [ident],verdict='UNKNOWN')
    supports=[r for r in regions.values()if r.get('role')=='support_body'and r.get('ink_bbox')]
    for i,a in enumerate(supports):
        for b in supports[i+1:]:
            ab,bb=a['ink_bbox'],b['ink_bbox']
            overlap=min(ab[2],bb[2])>max(ab[0],bb[0])and min(ab[3],bb[3])>max(ab[1],bb[1])
            check('STRUCTURE-PROJECTED-SEPARATION',not overlap,'support bodies/contacts must be independently readable in final projection',
                  [a['instance_id'],b['instance_id']],verdict='UNKNOWN')
    return finish()
