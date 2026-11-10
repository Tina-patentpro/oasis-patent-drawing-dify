"""Auditable representation/layout adaptation, never technical-plan repair."""
import copy,json,hashlib,math
from .diagrams import _route,_text_box,_overlap,resolve


def normalize_proposal(raw):
    if not isinstance(raw,dict):raise ValueError('planner must return object')
    p=copy.deepcopy(raw);changes=[]
    for field in ['constraints','unresolved']:
        rows=p.get(field,[])
        if not isinstance(rows,list)or len(rows)>80:raise ValueError('constraint/unknown capacity')
        converted=[]
        for i,row in enumerate(rows):
            if isinstance(row,dict)and isinstance(row.get('text'),str):
                value=json.dumps(row,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
                changes.append({'path':field+'/'+str(i),'action':'lossless_object_to_json_string'})
            elif isinstance(row,str):value=row
            else:raise ValueError('constraint/unknown must be text or text record')
            if len(value)>1000:raise ValueError('constraint/unknown capacity')
            converted.append(value)
        p[field]=converted
    scene=p.get('scene')
    if scene is None and 'scene'in p:
        p.pop('scene');changes.append({'path':'scene','action':'null_to_absence'})
    elif isinstance(scene,dict)and 'instances'not in scene:
        relations=scene.get('relations',[])
        if isinstance(relations,list)and not any(isinstance(r,dict)and set(r)&{'type','subjects','object','base','bounds'}for r in relations):
            notes=p.setdefault('planning_notes',{})
            if not isinstance(notes,dict)or 'legacy_scene'in notes:raise ValueError('planning metadata collision')
            notes['legacy_scene']=p.pop('scene');changes.append({'path':'scene','action':'retain_non_geometric_metadata_in_planning_notes'})
    parts={x['id']:x for x in p.get('parts',[])}
    for v in p.get('views',[]):
        if v.get('engine')=='vector':
            for move in place_labels(v['diagram'],parts):changes.append({'path':'views/'+v['id']+'/diagram/edges/'+str(move.pop('edge')),**move})
    def sha(obj):return hashlib.sha256(json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    return p,{'adapter':'planner-representation-v1','input_sha256':sha(raw),'output_sha256':sha(p),'changes':changes,'semantic_rewrite':False}


def place_labels(diagram,parts):
    """Only move label_at near its actual polyline; nodes/edges unchanged."""
    if not isinstance(diagram,dict)or len(diagram.get('nodes',[]))>48 or len(diagram.get('edges',[]))>96:raise ValueError('diagram layout capacity')
    nodes={n['id']:n for n in diagram.get('nodes',[])};occupied=[];moves=[]
    width,height=diagram['width'],diagram['height']
    for index,e in enumerate(diagram.get('edges',[])):
        a,b=nodes[e['source']],nodes[e['target']]
        label=('是'if e.get('branch')=='yes'else'否')if a['type']=='decision'else resolve(e['label'],parts)if e.get('label')else''
        if not label:continue
        points=e.get('points',_route(a,b));start,end=points[:2]
        old=e.get('label_at')
        if old is not None and (not isinstance(old,list)or len(old)!=2 or any(type(x)not in(int,float)or not math.isfinite(x)for x in old)or not(0<=old[0]<=width and 0<=old[1]<=height)):raise ValueError('label coordinates must be valid finite 2D points before layout')
        at=old or[(start[0]+end[0])/2,(start[1]+end[1])/2-22]
        def safe(point):
            box=_text_box(label,point,24)
            return (0<=box[0]<box[2]<=width and 0<=box[1]<box[3]<=height
                    and not any(_overlap(box,n['box'])for n in nodes.values())
                    and not any(_overlap(box,other)for other in occupied))
        if not safe(at):
            candidates=[]
            # Keep labels local to their first branch segment; do not re-route
            # arrows, swap branch targets or silently remove a difficult label.
            for ratio in [0.5,0.25,0.75]:
                x=start[0]+(end[0]-start[0])*ratio;y=start[1]+(end[1]-start[1])*ratio
                candidates += [[x+dx,y+dy]for dx,dy in [(12,-34),(-36,-34),(12,8),(-36,8),(0,-50),(0,16),(-60,-22),(30,-22)]]
            at=next((candidate for candidate in candidates if safe(candidate)),None)
            if at is None:raise ValueError('edge label has no safe local position; geometry requires review')
            e['label_at']=at;moves.append({'edge':index,'action':'move_label_only','before':old,'after':at})
        occupied.append(_text_box(label,at,24))
    return moves
