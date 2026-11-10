"""Small isometric drawing vocabulary; illustrative units, no manufacturing CAD."""
import hashlib
import copy
import io
import math
from html import escape
from itertools import combinations

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from .operation_store import digest
from .projected_relations import observe_projected_relations, wall_sections
from .source_geometry import display_contact_box, validate_display_projection
from .structural_semantics import finite, validate_structure


def _contact_ink(owner, indices, box, pair):
    """Both owners must leave adjacent ink at this actual contact, not elsewhere."""
    if (len(box)!=4 or box[0]<0 or box[1]<0 or box[2]>owner.width or box[3]>owner.height
            or box[2]-box[0]<3 or box[3]-box[1]<3 or any(i not in indices for i in pair)):
        return {'visible':False,'ink_pixels':{}}
    tile=owner.crop(box)
    masks=[tile.point([255 if i==indices[ident]else 0 for i in range(256)])for ident in pair]
    return _paired_contact_masks(masks,pair)


def _paired_contact_masks(masks,pair):
    """Both exact final owners must remain adjacent, not just in one broad box."""
    counts={ident:mask.histogram()[255]for ident,mask in zip(pair,masks)}
    adjacent=ImageChops.multiply(masks[0].filter(ImageFilter.MaxFilter(7)),masks[1]).getbbox()is not None
    return {'visible':all(n>=2 for n in counts.values())and adjacent,'ink_pixels':counts}


def _fit_contacts(scene, project, owner, indices):
    rows={r['id']:r for r in scene['instances']};contacts=[]
    for relation in scene['relations']:
        if relation['kind']!='rib_in_plated_hole':continue
        hole=rows[relation['hole']];h=hole['bounds'];cx=(h[0]+h[3])/2;cy=(h[1]+h[4])/2
        radius=hole['inner_radius']
        for ident in relation['ribs']:
            rib=rows[ident];points=[]
            if rib['shape']=='axial_rib':
                polygon=rib['footprint']
                for a,b in zip(polygon,polygon[1:]+polygon[:1]):
                    dx,dy=b[0]-a[0],b[1]-a[1];aa=dx*dx+dy*dy
                    if aa==0:continue
                    bb=2*((a[0]-cx)*dx+(a[1]-cy)*dy);cc=(a[0]-cx)**2+(a[1]-cy)**2-radius**2
                    disc=bb*bb-4*aa*cc
                    if disc < -1e-8:continue
                    for t in ((-bb-math.sqrt(max(0,disc)))/(2*aa),(-bb+math.sqrt(max(0,disc)))/(2*aa)):
                        if -1e-8<=t<=1+1e-8:points.append([a[0]+t*dx,a[1]+t*dy])
            elif rib['shape']=='rib_band':
                points=[[cx+radius*math.cos(i*math.pi/32),cy+radius*math.sin(i*math.pi/32)]for i in range(65)]
            projected=[project([x,y,z])for x,y in points for z in (rib['bounds'][2],rib['bounds'][5])]
            pair=[ident,hole['id']]
            box=([math.floor(min(p[0]for p in projected))-5,math.floor(min(p[1]for p in projected))-5,
                  math.ceil(max(p[0]for p in projected))+6,math.ceil(max(p[1]for p in projected))+6]if projected else[])
            check=_contact_ink(owner,indices,box,pair)if box else{'visible':False,'ink_pixels':{}}
            contacts.append({'kind':'rib_in_plated_hole','instance_ids':pair,'region':box,**check})
    return contacts


def _support_endpoints(row):
    bounds = row['bounds']
    angle = math.atan2(-.7, 1)
    members = {m['role']: m for m in row.get('members', [])}
    points = {}
    for side, z in (('upper', bounds[5]), ('lower', bounds[2])):
        member = members.get('mount_plate' if side == 'upper' else 'lower_stem')
        box = member['bounds'] if member else bounds
        if member and member['shape'] == 'solid':
            points[side] = [box[3], box[4], z]
            continue
        x, y = (box[0] + box[3]) / 2, (box[1] + box[4]) / 2
        radius = member.get('stem_radius', (box[3] - box[0]) / 2) if member else (box[3] - box[0]) / 2
        points[side] = [x + radius * math.cos(angle), y + radius * math.sin(angle), z]
    return points


def _support_display_maps(scene, targets, project):
    rows = {r['id']: r for r in scene['instances'] if r['role'] == 'support_body'}
    if set(rows) != set(targets) or any(r['shape'] != 'sleeved_support' for r in rows.values()):
        raise ValueError('display projection requires the same four composite support bodies')
    mappings, before, after = {}, [], []
    for ident, row in rows.items():
        start, end = (project(p) for p in _support_endpoints(row).values())
        upper, lower = (targets[ident][side] for side in ('upper', 'lower'))
        span = end[1] - start[1]
        if span <= 0:
            raise ValueError('display projection needs positive physical support separation')
        stretch = (lower[1] - upper[1]) / span
        shear = ((lower[0] - upper[0]) - (end[0] - start[0])) / span
        if not .5 <= stretch <= 2.5 or abs(shear) > .5:
            raise ValueError('display projection exceeds bounded schematic deformation')
        mappings[ident] = (start, upper, shear, stretch)
        before.append(tuple((a + b) / 2 for a, b in zip(start, end)))
        after.append(tuple((a + b) / 2 for a, b in zip(upper, lower)))

    def signed_area(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    for i, j, k in combinations(range(len(rows)), 3):
        if signed_area(before[i], before[j], before[k]) * signed_area(after[i], after[j], after[k]) <= 0:
            raise ValueError('display projection cannot rearrange the four corner supports')
    return mappings


def build_expression_guide(contract: dict, scene: dict, *, _evidence=None, _member_requirements=None,
                           _assembly_registration=None) -> dict:
    display = (_assembly_registration or {}).get('display_projection')
    if scene.get('panels'):
        if display:
            return {'status': 'FAIL', 'issues': [{'rule': 'GUIDE-PROJECTION', 'verdict': 'FAIL',
                                                   'message': 'registered display requires a single assembly view'}]}
        from .expression_panels import build_panel_guide
        return build_panel_guide(contract, scene, _member_requirements=_member_requirements)
    pre = validate_structure(scene, {}, contract, member_requirements=_member_requirements)
    if pre['status'] == 'FAIL': return {'status':'FAIL','issues':pre['checks']}
    canvas=scene.get('canvas'); projection=scene.get('projection',{}); origin=projection.get('origin'); scale=projection.get('scale')
    if (not isinstance(canvas,list) or len(canvas)!=2 or any(type(n)is not int or not 400<=n<=2400 for n in canvas)
            or not isinstance(origin,list) or len(origin)!=2 or not all(finite(v)for v in origin)
            or not finite(scale) or not 1<=scale<=100):
        return {'status':'FAIL','issues':[{'rule':'GUIDE-PROJECTION','verdict':'FAIL','message':'bounded canvas/projection required'}]}
    mode=scene.get('render_mode','wireframe'); tilt=projection.get('tilt',.35)
    if mode not in {'wireframe','opaque_surfaces'} or not finite(tilt) or not .15<=tilt<=1.2:
        return {'status':'FAIL','issues':[{'rule':'GUIDE-PROJECTION','verdict':'FAIL','message':'bounded render mode/tilt required'}]}
    opaque=mode=='opaque_surfaces'
    w,h=canvas; image=Image.new('RGB',(w,h),'white')
    svg=[]; regions=[]; layers={}; ownership=Image.new('L',(w,h),0); indices={}; member_layers={}
    profile_break_regions = {}
    opacity_draw=None
    active_map = None
    def project_unregistered(p):
        x,y,z=p
        return (round(origin[0]+scale*(x-.7*y),2),round(origin[1]+scale*(tilt*x+tilt*y-z),2))
    try:
        if display:
            validate_display_projection(_assembly_registration, scene)
        display_maps = _support_display_maps(scene, display['endpoints'], project_unregistered) if display else {}
        from .source_profiles import registered_relation_sections
        sections = {r['part_id']:r for r in registered_relation_sections(_assembly_registration,scene,contract)}
    except (ValueError, KeyError, TypeError) as error:
        return {'status': 'FAIL', 'issues': [{'rule': 'GUIDE-PROJECTION', 'verdict': 'FAIL', 'message': str(error)}]}
    def project(p):
        x, y = project_unregistered(p)
        if active_map is None:
            return x, y
        start, upper, shear, stretch = active_map
        return round(x - start[0] + upper[0] + (y - start[1]) * shear, 2), round(upper[1] + (y - start[1]) * stretch, 2)
    def polyline(points, painter, width=3, fill=None):
        if any(not(3<=x<=w-3 and 3<=y<=h-3)for x,y in points):raise ValueError('projected geometry outside canvas')
        if fill is not None:painter.polygon(points,fill=fill)
        painter.line(points,fill='black',width=width)
        if opacity_draw is not None:
            if fill is not None:opacity_draw.polygon(points,fill=255)
            opacity_draw.line(points,fill=255,width=width)
        svg.append('<polyline points="'+' '.join(f'{x},{y}'for x,y in points)+'" fill="'+('white'if fill is not None else'none')+'" stroke="black" stroke-width="'+str(width)+'"/>')
    def circle(cx,cy,z,r):
        return [project([cx+r*math.cos(i*math.pi/32),cy+r*math.sin(i*math.pi/32),z])for i in range(65)]
    def depth(p):return .7*p[0]+p[1]+1.7*tilt*p[2]
    def omitted_boundary(corners,painter,width=2):
        for start,end in zip(corners,corners[1:]+corners[:1]):
            dx,dy=end[0]-start[0],end[1]-start[1];length=math.hypot(dx,dy)
            nx,ny=-dy/length*.25,dx/length*.25
            def at(t,n=0):return [start[0]+dx*t+nx*n,start[1]+dy*t+ny*n,start[2]]
            polyline([project(p)for p in [start,at(.37),at(.40,1),at(.43,-1),at(.46)]],painter,width)
            polyline([project(p)for p in [at(.54),at(.57,1),at(.60,-1),at(.63),end]],painter,width)
    def white_face(points,painter):
        projected=[project(p)for p in points]
        if any(not(3<=x<=w-3 and 3<=y<=h-3)for x,y in projected):
            raise ValueError('projected geometry outside canvas')
        painter.polygon(projected,fill='white')
        if opacity_draw is not None:opacity_draw.polygon(projected,fill=255)
        svg.append('<polygon points="'+' '.join(f'{x},{y}'for x,y in projected)+'" fill="white" stroke="none"/>')
    def prism(points,z0,z1,painter):
        faces=[]
        for a,b in zip(points,points[1:]+points[:1]):
            face=[[a[0],a[1],z0],[b[0],b[1],z0],[b[0],b[1],z1],[a[0],a[1],z1]]
            faces.append(face)
        for face in sorted(faces,key=lambda f:sum(depth(p)for p in f)/len(f)):
            polyline([project(p)for p in face+[face[0]]],painter,fill='white')
        top=[project([x,y,z1])for x,y in points]
        polyline(top+[top[0]],painter,fill='white')
    def hull(points):
        points=sorted(set(points))
        def cross(a,b,c):return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
        lower=[];upper=[]
        for p in points:
            while len(lower)>=2 and cross(lower[-2],lower[-1],p)<=0:lower.pop()
            lower.append(p)
        for p in reversed(points):
            while len(upper)>=2 and cross(upper[-2],upper[-1],p)<=0:upper.pop()
            upper.append(p)
        return lower[:-1]+upper[:-1]
    rendered_sections = []
    def segments(row, painter):
        b=list(row['bounds']); shape=row['shape']
        if row.get('display_section'):b[4]=row['display_section']['plane_y']
        cx=(b[0]+b[3])/2;cy=(b[1]+b[4])/2
        if shape=='shouldered_stem':
            radius=row['stem_radius'];z=b[5]-row['shoulder_height']
            segments({'shape':'cylinder','bounds':[cx-radius,cy-radius,b[2],cx+radius,cy+radius,z]},painter)
            segments({'shape':'cylinder','bounds':[b[0],b[1],z,b[3],b[4],b[5]]},painter)
            return
        if shape=='source_profile':
            transform=row['source_transform']
            if (set(transform)!={'scale','x','y'} or not all(finite(v)for v in transform.values())
                    or not .05<=transform['scale']<=20):raise ValueError('bounded source scale/translation required')
            paths=row['source_paths']
            if not isinstance(paths,list)or not 1<=len(paths)<=64:raise ValueError('bounded source paths required')
            omission = row.get('display_omission')
            if omission and omission['path_index'] >= len(paths):
                raise ValueError('source profile omission path missing')
            for path_index, path in enumerate(paths):
                if not isinstance(path,list)or not 2<=len(path)<=512:raise ValueError('bounded source path required')
                if any(not isinstance(p,list)or len(p)!=2 or not all(finite(v)for v in p)for p in path):raise ValueError('finite source pixels required')
                points = [(transform['x']+transform['scale']*x, transform['y']+transform['scale']*y) for x,y in path]
                if not omission or path_index != omission['path_index']:
                    polyline(points, painter, 2)
                    continue
                segment = omission['segment_index']
                if segment + 1 >= len(points):
                    raise ValueError('source profile omission segment missing')
                start, end = points[segment:segment+2]
                length = math.dist(start, end)
                fraction = omission['fraction']
                if length < 16 or min(fraction, 1-fraction) * length <= 5:
                    raise ValueError('source profile omission needs a drawable local segment')
                def at(offset):
                    t = fraction + offset / length
                    return tuple(a + t * (b-a) for a,b in zip(start,end))
                left, right = at(-5), at(5)
                polyline(points[:segment+1] + [left], painter, 2)
                polyline([right] + points[segment+1:], painter, 2)
                profile_break_regions[row['id']] = [
                    math.floor(min(left[0], right[0]))-2, math.floor(min(left[1], right[1]))-2,
                    math.ceil(max(left[0], right[0]))+2, math.ceil(max(left[1], right[1]))+2,
                ]
            return
        if shape in {'cable_shell','cable_band'}:
            cy=(b[1]+b[4])/2;cz=(b[2]+b[5])/2;radius=(b[4]-b[1])/2
            if abs(b[5]-b[2]-(b[4]-b[1]))>1e-7:raise ValueError('cable shell needs a bounded circular section')
            caps=[[project([x,cy+radius*math.cos(i*math.pi/32),cz+radius*math.sin(i*math.pi/32)])for i in range(65)]for x in (b[0],b[3])]
            silhouette=hull(caps[0]+caps[1]);polyline(silhouette+[silhouette[0]],painter,fill='white')
            polyline(caps[1],painter,fill='white');return
        if shape=='access_gap':
            # Only the existing wall end-face boundaries identify this void.
            # No positive solid, sleeve, PCB hole or new fastener is introduced.
            for x in (b[0],b[3]):
                points=[[x,b[1],b[2]],[x,b[4],b[2]],[x,b[4],b[5]],[x,b[1],b[5]],[x,b[1],b[2]]]
                polyline([project(p)for p in points],painter,2)
            return
        if shape=='axial_rib':
            fp=row['footprint']
            if opaque:
                prism(fp,b[2],b[5],painter);return
            for z in (b[2],b[5]):polyline([project([x,y,z])for x,y in fp+[fp[0]]],painter)
            for x,y in fp:polyline([project([x,y,b[2]]),project([x,y,b[5]])],painter)
            return
        if shape=='plated_bore':
            # Back half of the bore plus its cut faces, not a freestanding barrel.
            outer=(b[3]-b[0])/2;inner=row['inner_radius']
            for z in (b[2],b[5]):
                for radius in (outer,inner):polyline(circle(cx,cy,z,radius)[:33],painter)
            for sign in (-1,1):
                for radius in (outer,inner):
                    polyline([project([cx+sign*radius,cy,b[2]]),project([cx+sign*radius,cy,b[5]])],painter)
            return
        if shape in {'closed_sleeve','cylinder','rib_band','plated_bore'}:
            radius=(b[3]-b[0])/2
            if opaque:
                silhouette=hull(circle(cx,cy,b[2],radius)+circle(cx,cy,b[5],radius))
                polyline(silhouette+[silhouette[0]],painter,fill='white')
                polyline(circle(cx,cy,b[5],radius),painter,fill='white')
                return
            polyline(circle(cx,cy,b[2],radius),painter)
            polyline(circle(cx,cy,b[5],radius),painter)
            theta=math.atan2(-.7,1)
            for a in (theta,theta+math.pi):
                x,y=cx+radius*math.cos(a),cy+radius*math.sin(a)
                polyline([project([x,y,b[2]]),project([x,y,b[5]])],painter)
            if shape=='plated_bore':
                for z in (b[2],b[5]):polyline(circle(cx,cy,z,row['inner_radius']),painter)
            return
        if shape in {'solid','sectioned_board'}:
            if row.get('display_omission'):
                # Keep the same opaque illustrative envelope and hidden-pin
                # behavior, but do not claim a complete source perimeter.
                xy=[[b[0],b[1]],[b[3],b[1]],[b[3],b[4]],[b[0],b[4]]]
                for a,z in zip(xy,xy[1:]+xy[:1]):
                    white_face([[a[0],a[1],b[2]],[z[0],z[1],b[2]],
                                [z[0],z[1],b[5]],[a[0],a[1],b[5]]],painter)
                white_face([[x,y,b[5]]for x,y in xy],painter)
                for height in (b[2],b[5]):
                    omitted_boundary([[x,y,height]for x,y in xy],painter)
                return
            if shape=='sectioned_board':
                # Open cutaway perimeter; never an invented physical slot.
                pts=[[b[0],b[1],b[5]],[b[3],b[1],b[5]],[b[3],b[4],b[5]]]
                polyline([project(p)for p in pts],painter)
                polyline([project([b[0],b[1],b[2]]),project([b[3],b[1],b[2]]),project([b[3],b[4],b[2]])],painter)
                for y in (b[1],b[4]):polyline([project([b[3],y,b[2]]),project([b[3],y,b[5]])],painter)
                holes=[r for r in scene['instances']if r['role']=='plated_hole'
                       and any(rel.get('kind')=='rib_in_plated_hole'and rel.get('board')==row['id']and rel.get('hole')==r['id']for rel in scene['relations'])]
                for hole in holes:
                    hb=hole['bounds'];hx=(hb[0]+hb[3])/2;hy=(hb[1]+hb[4])/2;radius=hole['inner_radius']
                    for left,right in [(b[0],hx-radius),(hx+radius,b[3])]:
                        points=[[left,hy,b[2]],[right,hy,b[2]],[right,hy,b[5]],[left,hy,b[5]],[left,hy,b[2]]]
                        polyline([project(p)for p in points],painter,2)
                        x=left+.3
                        while x<right:
                            top=min(right,x+(b[5]-b[2])*.6)
                            polyline([project([x,hy,b[2]]),project([top,hy,b[5]])],painter,1)
                            x+=.7
                return
            if opaque:
                prism([[b[0],b[1]],[b[3],b[1]],[b[3],b[4]],[b[0],b[4]]],b[2],b[5],painter)
                return
            for z in (b[2],b[5]):
                pts=[[b[0],b[1],z],[b[3],b[1],z],[b[3],b[4],z],[b[0],b[4],z],[b[0],b[1],z]]
                polyline([project(p)for p in pts],painter)
            for x,y in [(b[0],b[1]),(b[3],b[1]),(b[3],b[4]),(b[0],b[4])]:
                polyline([project([x,y,b[2]]),project([x,y,b[5]])],painter)
            return
        if shape=='omitted_surface':
            # One common plane, with open conventional breaks on every unknown
            # extent. Four independent pads would falsely suggest four plates.
            z=b[2]if row['role']=='supported_plate'else b[5]
            corners=[[b[0],b[1],z],[b[3],b[1],z],[b[3],b[4],z],[b[0],b[4],z]]
            omitted_boundary(corners,painter)
            return
        if shape=='wall':
            height=row['height']
            for corners in wall_sections(row):
                a,bp=corners[:2]
                if opaque:
                    # Current vocabulary supports constant-height wall prisms;
                    # a sloped wall needs an explicit stronger face witness.
                    if abs(a[2]-bp[2])>1e-7:raise ValueError('opaque wall needs constant-height source witness')
                    prism([[x,y]for x,y,z in corners],a[2],a[2]+height,painter)
                    continue
                for dz in (0,height):polyline([project([x,y,z+dz])for x,y,z in corners+[corners[0]]],painter)
                for x,y,z in corners:polyline([project([x,y,z]),project([x,y,z+height])],painter)
            return
        if shape in {'wire','source_segments'}:
            polyline([project(p)for p in row['path']],painter,2 if shape=='wire' else 3)
            return
        raise ValueError('unsupported shape')
    try:
        rows=scene['instances']
        if opaque:
            def layer(row):
                role=row['role'];b=row['bounds'];center=[(b[i]+b[i+3])/2 for i in range(3)]
                # Explicit cutaway -> inserted objects -> opaque mother body ->
                # walls/wires above the floor. No transparent pin through floor.
                level=0 if role in {'pcb','plated_hole'} else 2 if role=='busbar_body' else 3 if role in {'external_wall','partition_wall','signal_wire','rear_trough','shield','drain_wire','shielded_cable','weld_opening'} else 1
                # supports_at proves their z-order. A whole-plate center depth
                # must not put its shared upper edge behind a front support.
                if role=='supporting_plate':level=0
                if role=='supported_plate':level=2
                if row['shape']=='source_profile' and role not in {'supporting_plate', 'supported_plate'}:level=4
                return level,depth(center)
            rows=sorted(rows,key=layer)
        for index,row in enumerate(rows,1):
            active_map = display_maps.get(row['id'])
            mask=Image.new('RGB',(w,h),'white'); painter=ImageDraw.Draw(mask)
            opacity=Image.new('L',(w,h),0);opacity_draw=ImageDraw.Draw(opacity)if opaque else None
            start=len(svg)
            if row['shape']=='sleeved_support':
                if not opaque: raise ValueError('source exterior composite requires opaque member ownership')
                member_owner=Image.new('L',(w,h),0); member_info={}
                members=sorted(row['members'],key=lambda m:depth([(m['bounds'][i]+m['bounds'][i+3])/2 for i in range(3)]))
                for mi,member in enumerate(members,1):
                    tile=Image.new('RGB',(w,h),'white'); occupied=Image.new('L',(w,h),0)
                    opacity_draw=ImageDraw.Draw(occupied)
                    substart=len(svg);segments(member,ImageDraw.Draw(tile))
                    member_svg=svg[substart:];del svg[substart:]
                    svg.append('<g id="'+escape(row['id']+'__'+member['part_id'],quote=True)+'">'+''.join(member_svg)+'</g>')
                    mask.paste(tile,(0,0),occupied);opacity=ImageChops.lighter(opacity,occupied)
                    member_owner.paste(0,(0,0),occupied)
                    member_ink=tile.convert('L').point(lambda p:255 if p<128 else 0)
                    member_owner.paste(mi,(0,0),member_ink)
                    member_info[member['part_id']]={'index':mi,'role':member['role'],
                        'total_ink':member_ink.histogram()[255], 'original_ink':member_ink,'occupied':occupied}
                member_layers[row['id']]={part:{**info,'mask':member_owner.point([255 if n==info['index'] else 0 for n in range(256)])}
                                          for part,info in member_info.items()}
            else:
                segments(row,painter)
                section = sections.get(row['part_id']) if row['shape']=='source_profile' else None
                if section:
                    if mask.crop(section['region']).convert('L').getextrema()[0]<216:
                        raise ValueError('local relation section overlaps traced source ink')
                    polyline(section['stroke'],painter,2)
                    rendered_sections.append({**{k:v for k,v in section.items() if k!='stroke'},
                                              'instance_id':row['id']})
            body_svg=svg[start:];del svg[start:]
            if opaque:
                image.paste(mask,(0,0),opacity);ownership.paste(0,(0,0),opacity)
                ink=mask.convert('L').point(lambda p:255 if p<128 else 0)
                ownership.paste(index,(0,0),ink);indices[row['id']]=index
            else:image=ImageChops.darker(image,mask)
            layers[row['id']]=mask.convert('L')
            svg.append('<g id="'+escape(row['id'],quote=True)+'">'+''.join(body_svg)+'</g>')
            ink=mask.convert('L').point(lambda p:255 if p<128 else 0);box=ink.getbbox()
            r={'instance_id':row['id'],'part_id':row['part_id'],'role':row['role'],
               'ink_bbox':list(box)if box else None,'ink_pixels':ink.histogram()[255],
               'visible_fraction':1.0,'contacts':{},'contacts_visible':{}}
            if row['role']=='support_body':
                end_members={m['role']:m for m in row.get('members',[])}
                if end_members:r['contact_members']={'upper':end_members['mount_plate']['part_id'],'lower':end_members['lower_stem']['part_id']}
                for key, point in _support_endpoints(row).items():
                    box=display_contact_box(project(point))
                    r['contacts'][key]=box;r['contacts_visible'][key]=mask.crop(box).convert('L').getextrema()[0]<128
            regions.append(r)
    except (ValueError,KeyError,TypeError) as e:
        return {'status':'FAIL','issues':[{'rule':'GUIDE-GEOMETRY','verdict':'FAIL','message':str(e)}]}
    active_map = None
    out=io.BytesIO();image.save(out,format='PNG');png=out.getvalue()
    support_ids=[r['instance_id']for r in regions if r['role']=='support_body']
    for r in regions:
        if opaque:
            exclusive=ownership.point([255 if n==indices[r['instance_id']] else 0 for n in range(256)])
            r['visible_fraction']=exclusive.histogram()[255]/max(1,r['ink_pixels'])
            for name,box in r['contacts'].items():r['contacts_visible'][name]=exclusive.crop(box).getextrema()[1]>128
            if r['instance_id'] in member_layers:
                r['member_regions']=[]
                for part,info in member_layers[r['instance_id']].items():
                    retained=ImageChops.multiply(info['mask'],exclusive)
                    pixels=retained.histogram()[255];bbox=retained.getbbox()
                    r['member_regions'].append({'part_id':part,'role':info['role'],'ink_pixels':pixels,
                                               'ink_bbox':list(bbox)if bbox else None,
                                               'visible_fraction':pixels/max(1,info['total_ink']),
                                               'projection_fully_retained':True,
                                               'occluded_fraction_by_part':{
                                                   other:ImageChops.multiply(info['original_ink'],occluder['occupied']).histogram()[255]/max(1,info['total_ink'])
                                                   for other,occluder in member_layers[r['instance_id']].items()
                                                   if occluder['index']>info['index']}})
                    for name,box in r['contacts'].items():
                        if r['contact_members'][name]==part:
                            r['contacts_visible'][name]=retained.crop(box).histogram()[255]>=2
            continue
        if r['instance_id']not in support_ids:continue
        own=layers[r['instance_id']].point(lambda p:255 if p<128 else 0)
        others=Image.new('L',(w,h),0)
        for ident in support_ids:
            if ident!=r['instance_id']:
                other=layers[ident].point(lambda p:255 if p<128 else 0).filter(ImageFilter.MaxFilter(3))
                others=ImageChops.lighter(others,other)
        exclusive=ImageChops.subtract(own,others)
        r['visible_fraction']=exclusive.histogram()[255]/max(1,r['ink_pixels'])
        for name,box in r['contacts'].items():r['contacts_visible'][name]=exclusive.crop(box).getextrema()[1]>128
    xml=('<svg xmlns="http://www.w3.org/2000/svg" width="'+str(w)+'" height="'+str(h)+'"><rect width="100%" height="100%" fill="white"/>'+''.join(svg)+'</svg>').encode()
    result={'status':'UNKNOWN','svg':xml,'png':png,'regions':regions,
            'roles':{r['instance_id']:r['role']for r in regions},'relations':scene['relations'],
            'scene_sha256':digest(scene),'contract_sha256':digest(contract),
            'technical_shape_accuracy':'not_verified','coordinate_space':'illustrative_units'}
    result['source_unknowns'] = copy.deepcopy(contract.get('unknowns', []))
    result['visibility_model']='opaque_surfaces_with_ink_ownership'if opaque else'explicit_wireframe_guide_not_actual_image'
    result['contact_regions']=_fit_contacts(scene,project,ownership,indices)if opaque else[]
    if opaque:
        by_region={r['instance_id']:r for r in regions}
        for relation in scene['relations']:
            if relation['kind']!='supports_at':continue
            for ident in relation['subjects']:
                row=by_region[ident]
                if ident not in member_layers:continue
                aggregate=ownership.point([255 if i==indices[ident]else 0 for i in range(256)])
                for side,box in row['contacts'].items():
                    part=row['contact_members'][side];plate=relation[side]
                    member=ImageChops.multiply(member_layers[ident][part]['mask'],aggregate).crop(box)
                    common=ownership.crop(box).point([255 if i==indices[plate]else 0 for i in range(256)])
                    pair=[ident,plate];check=_paired_contact_masks([member,common],pair)
                    row['contacts_visible'][side]=check['visible']
                    result['contact_regions'].append({'kind':'supports_at','instance_ids':pair,
                        'member_part_id':part,'side':side,'region':box,**check})
    result['visible_regions']=copy.deepcopy(regions)
    from .source_profiles import profile_bindings
    result['source_region_bindings']=profile_bindings(scene)
    if any(r.get('display_omission')for r in scene['instances']):
        result['display_conventions']=[{'instance_id':r['id'],'part_id':r['part_id'],
            'convention_id':r['display_omission']['convention_id'],'kind':'local_omission',
            'not_physical_feature':True,
            **({'omission_region':profile_break_regions[r['id']]} if r['id'] in profile_break_regions else {})}
            for r in scene['instances']if r.get('display_omission')]
    if rendered_sections:
        result.setdefault('display_conventions', []).extend(rendered_sections)
    if opaque:
        channels, fits = observe_projected_relations(scene, project, ownership, indices, image,
                                                     result['contact_regions'])
        if channels: result['projected_channels'] = channels
        if fits: result['projected_pin_fits'] = fits
    result['guide_sha256']=digest({'svg':hashlib.sha256(xml).hexdigest(),'png':hashlib.sha256(png).hexdigest(),
                                  'regions':regions,'contact_regions':result['contact_regions'],
                                  'scene_sha256':result['scene_sha256'],'contract_sha256':result['contract_sha256'],
                                  **{k:result[k] for k in ('projected_channels','projected_pin_fits') if k in result},
                                  **({'relation_sections':rendered_sections} if rendered_sections else {}),
                                  **({'display_projection_sha256': digest(display)} if display else {})})
    if display:
        result['display_projection_sha256'] = digest(display)
    if _member_requirements is not None:
        result['member_visibility_requirements']=copy.deepcopy(_member_requirements)
    checked=validate_structure(scene,result,contract,member_requirements=_member_requirements);result['status']=checked['status'];result['issues']=checked['checks']
    if _evidence is not None:
        _evidence.update(ownership=ownership,layers=layers,indices=indices,member_layers=member_layers,
                         project=project)
    return result
