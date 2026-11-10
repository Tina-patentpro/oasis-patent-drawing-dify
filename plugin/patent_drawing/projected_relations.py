"""Measure typed relation evidence on the delivered guide raster and owner map."""
import math

from PIL import ImageChops


def wall_sections(row):
    """Return the same four corners used by the opaque wall renderer."""
    thickness = row['thickness'] / 2
    for a, b in zip(row['path'], row['path'][1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        if not length:
            raise ValueError('wall segment has no length')
        nx, ny = -dy / length * thickness, dx / length * thickness
        yield [[a[0]+nx, a[1]+ny, a[2]], [b[0]+nx, b[1]+ny, b[2]],
               [b[0]-nx, b[1]-ny, b[2]], [a[0]-nx, a[1]-ny, a[2]]]


def _retained_spans(edges, owns_ink):
    lengths = [math.dist(a, b) for a, b in edges]
    steps = math.ceil(max(lengths))
    if min(lengths) < 8 or steps > 4800:
        return []

    def points(step):
        t = step / steps
        return [(round(a[0]+(b[0]-a[0])*t), round(a[1]+(b[1]-a[1])*t)) for a,b in edges]

    spans = []
    start = None
    for step in range(steps + 2):
        if step <= steps and all(owns_ink(x, y) for x, y in points(step)):
            if start is None:
                start = step
            continue
        if start is not None and (step-1-start)*min(lengths)/steps >= 8:
            spans.append((start, step-1))
        start = None
    return [[(a,b) for a,b in zip(points(lo),points(hi))] for lo,hi in spans]


def observe_projected_relations(scene, project, owner, indices, raster, contacts, *, panel_id=None, offset=(0,0)):
    """Keep evidence from one final crop together. Pixel units are display only."""
    channels, fits = [], []
    rows = {r['id']:r for r in scene['instances']}
    relation_kinds = {r['kind'] for r in scene['relations']}
    if not relation_kinds.intersection({'channel_partition','rib_in_plated_hole'}):
        return channels, fits
    gray = raster.convert('L')
    owners, pixels = owner.load(), gray.load()
    width,height = owner.size

    def owned(ident):
        index = indices[ident]
        return lambda x,y: any(owners[a,b]==index and pixels[a,b]<190
                               for a in range(max(0,x-1),min(width,x+2))
                               for b in range(max(0,y-1),min(height,y+2)))

    def absolute(point):
        return [point[0]+offset[0],point[1]+offset[1]]

    def absolute_box(box):
        return [n+offset[i%2] for i,n in enumerate(box)] if box else None

    def ink_region(ident, box):
        mask=owner.crop(box).point([255 if i==indices[ident] else 0 for i in range(256)])
        ink=gray.crop(box).point(lambda n:255 if n<190 else 0)
        visible=ImageChops.multiply(mask,ink)
        if visible.histogram()[255]<3:
            return None
        return [n+box[i%2] for i,n in enumerate(visible.getbbox())]

    for relation in scene['relations']:
        kind=relation['kind']
        if kind=='channel_partition':
            partitions = {}
            for ident in relation['partitions']:
                retained = []
                row = rows[ident]
                for corners in wall_sections(row):
                    a,b,c,d = [project([x,y,z+row['height']]) for x,y,z in corners]
                    length=math.dist(a,b)
                    separation=abs((b[0]-a[0])*(d[1]-a[1])-(b[1]-a[1])*(d[0]-a[0]))/max(length,1e-9)
                    if separation < 3:
                        continue
                    for span in _retained_spans([(a,b),(d,c)], owned(ident)):
                        retained.append((separation,span))
                if retained:
                    separation,span=max(retained,key=lambda x:min(math.dist(*edge) for edge in x[1]))
                    partitions[ident]={'visible_edges':[[absolute(p) for p in edge] for edge in span],
                                       'min_separation_px':round(separation,2)}
            wires={}
            for ident in relation['wires']:
                points=[project(p) for p in rows[ident]['path']]
                spans=[span[0] for a,b in zip(points,points[1:])
                       for span in _retained_spans([(a,b)],owned(ident))]
                wires[ident]=[[absolute(p) for p in span] for span in spans]
            channels.append({'panel_id':panel_id,'partition_ids':relation['partitions'],
                             'partitions':partitions,'wires':wires,
                             'wire_crossings':0, 'basis':'validated_scene_and_final_guide_pixels',
                             'visible':set(partitions)==set(relation['partitions']) and all(wires.values())})
        elif kind=='rib_in_plated_hole':
            pin,hole,board=(relation[key] for key in ('pin','hole','board'))
            hb=rows[hole]['bounds']
            # Bound the local region by the projected plated bore through the PCB.
            points=[project([x,y,z]) for x in (hb[0],hb[3]) for y in (hb[1],hb[4]) for z in (hb[2],hb[5])]
            box=[math.floor(min(p[0] for p in points))-3, math.floor(min(p[1] for p in points))-3,
                 math.ceil(max(p[0] for p in points))+4, math.ceil(max(p[1] for p in points))+4]
            retained=0<=box[0]<box[2]<=width and 0<=box[1]<box[3]<=height
            observations={ident:absolute_box(ink_region(ident,box)) if retained else None
                          for ident in (pin,hole,board,*relation['ribs'])}
            pin_box=rows[pin]['bounds']
            z0,z1=max(pin_box[2],hb[2]),min(pin_box[5],hb[5])
            x,y=(pin_box[0]+pin_box[3])/2,(pin_box[1]+pin_box[4])/2
            radius=(pin_box[3]-pin_box[0])/2
            shaft=[]
            if retained and z0<z1:
                for angle in (math.atan2(-.7,1),math.atan2(-.7,1)+math.pi):
                    xy=[x+radius*math.cos(angle),y+radius*math.sin(angle)]
                    line=tuple(project([*xy,z]) for z in (z0,z1))
                    shaft.extend([absolute(p) for p in span[0]]
                                 for span in _retained_spans([line],owned(pin)))
            # Contact rows were already recomputed on this same final panel.
            insertion_region=absolute_box(box) if retained else None
            visible_ribs=[rib for rib in relation['ribs'] if observations[rib] and any(
                c['kind']==kind and c['instance_ids']==[rib,hole] and c['visible'] is True
                and len(c['region'])==4 and all(insertion_region[i]<=c['region'][i]<c['region'][i+2]<=insertion_region[i+2]
                                               for i in (0,1))
                for c in contacts)]
            fits.append({'panel_id':panel_id,'pin_id':pin,'hole_id':hole,'board_id':board,
                         'insertion_region':insertion_region,'insertion_pin_traces':shaft,
                         'observations':observations,'visible_contact_ribs':visible_ribs,
                         'visible':all(observations[i] for i in (pin,hole,board)) and bool(visible_ribs and shaft)})
    return channels, fits
