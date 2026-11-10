"""Source-bound exterior evidence. Construction never grants operator approval."""
import copy,math,re
from .operation_store import digest,canonical
from .source_contract import validate_facts

CONTRACT='source-geometry-036-r1'


def context(bundle,plan):
 source=digest([x['sha256']for x in bundle['sources']])
 if source!=plan['source_sha256']:raise ValueError('source geometry source identity differs')
 return digest({'source':source,'facts':plan.get('facts',[]),'constraints':plan.get('constraints',[]),'unresolved':plan.get('unresolved',[])})


def validate_assets(assets,bundle,plan):
 if not isinstance(assets,list)or not 1<=len(assets)<=120:raise ValueError('source geometry assets required')
 pages={p['id']:p for p in bundle['pages']};regions={r['id']:r for r in bundle['figure_candidates']};parts={p['id']for p in plan['parts']};seen=set()
 for a in assets:
  required={'part_id','source_region_id','source_image_sha256','visible_segments','outline_complete','properties','cardinality','evidence'}
  if not isinstance(a,dict)or set(a)!=required or a['part_id']not in parts or a['part_id']in seen:raise ValueError('source asset identity/schema invalid')
  seen.add(a['part_id']);region=regions.get(a['source_region_id']);page=pages.get(region['page_id'])if region else None
  if not page or not page.get('image')or a['source_image_sha256']!=page['image']['sha256']:raise ValueError('source asset image/region identity differs')
  props=a['properties']
  if (not isinstance(props,dict)or set(props)!={'external_form','internal_parts','allowed_exterior_details'}or props['external_form']not in {'closed_sleeve','source_contour','open_spring','unspecified'}or any(not isinstance(props[k],list)or len(props[k])>40 or any(not isinstance(v,str)or not 1<=len(v)<=160 for v in props[k])for k in ['internal_parts','allowed_exterior_details'])):raise ValueError('unsupported exterior property; no invented interfaces/dimensions')
  c=a['cardinality']
  if not isinstance(c,dict)or len(c)!=1 or not set(c)<={'exact','minimum'}or any(type(v)is not int or not 1<=v<=24 for v in c.values()):raise ValueError('source cardinality invalid')
  f={'id':'evidence','status':'confirmed','text':'source exterior','evidence':a['evidence']}
  if any(x['verdict']=='FAIL'for x in validate_facts(bundle,[f])):raise ValueError('source exterior evidence missing')
  paths=a['visible_segments'];box=region['bbox'];width,height=box[2]-box[0],box[3]-box[1]
  if not isinstance(paths,list)or not 1<=len(paths)<=64 or type(a['outline_complete'])is not bool:raise ValueError('source contour invalid')
  for path in paths:
   if not isinstance(path,list)or not 2<=len(path)<=512:raise ValueError('source contour capacity')
   for xy in path:
    if not isinstance(xy,list)or len(xy)!=2 or any(type(v)not in(int,float)or not math.isfinite(v)for v in xy)or not 0<=xy[0]<=width or not 0<=xy[1]<=height:raise ValueError('source contour outside actual crop')
  if a['outline_complete']and not all(p[0]==p[-1]for p in paths):raise ValueError('complete outline must be explicitly traced')


def contour_has_cycle(paths):
 parents={}
 def root(point):
  while point in parents:point=parents[point]
  return point
 for path in paths:
  for a,b in zip(path,path[1:]):
   left,right=root(tuple(a)),root(tuple(b))
   if left==right:return True
   parents[left]=right
 return False


def display_contact_box(point):
    x, y = map(int, point)
    return [x - 6, y - 6, x + 7, y + 7]


def validate_display_projection(registration, scene):
    """Check a privately registered image-plane layout without changing the scene."""
    if 'display_projection' not in registration:
        return
    layout = registration['display_projection']
    if (not isinstance(layout, dict) or set(layout) != {'version', 'scene_sha256', 'endpoints'}
            or layout['version'] != 'registered-support-display-1' or not isinstance(scene, dict)
            or layout['scene_sha256'] != digest(scene) or scene.get('panels')
            or scene.get('render_mode') != 'opaque_surfaces'):
        raise ValueError('display projection requires one exact opaque assembly scene')
    supports = registration['supports']
    targets = layout['endpoints']
    canvas = scene.get('canvas')
    if (not isinstance(targets, dict) or set(targets) != set(supports)
            or not isinstance(canvas, list) or len(canvas) != 2
            or any(type(n) is not int or not 400 <= n <= 2400 for n in canvas)):
        raise ValueError('display projection must cover every registered support on one bounded canvas')
    transform = registration['transform']
    windows = []
    for ident, sides in supports.items():
        pair = targets[ident]
        if not isinstance(pair, dict) or set(pair) != {'upper', 'lower'}:
            raise ValueError('display projection needs both endpoints for every support')
        if len({side.get('basis') == 'source_relation' for side in sides.values()}) != 1:
            raise ValueError('display projection requires both endpoints of a relation-only support to be unobserved')
        for side, point in pair.items():
            if (not isinstance(point, list) or len(point) != 2
                    or any(type(n) not in (int, float) or not math.isfinite(n) or round(n, 2) != n for n in point)
                    or not all(6 <= point[i] < canvas[i] - 6 for i in (0, 1))):
                raise ValueError('display projection endpoint is outside the canvas')
            box = display_contact_box(point)
            source = sides[side]['source_box']
            if source is not None:
                frame = [transform[axis] + transform['scale'] * n
                         for axis, n in zip(('x', 'y', 'x', 'y'), source)]
                if not all(frame[i] <= box[i] < box[i + 2] <= frame[i + 2] for i in (0, 1)):
                    raise ValueError('display projection endpoint must be inside its registered source window')
            if any(all(max(b[i], box[i]) < min(b[i + 2], box[i + 2]) for i in (0, 1)) for b in windows):
                raise ValueError('display projection endpoints need distinct nonoverlapping windows')
            windows.append(box)
        if pair['lower'][1] - pair['upper'][1] < 16:
            raise ValueError('display projection must retain the upper-to-lower support order')


def validate_assembly_registration(registration, assets, bundle, plan):
 required={'view_id','source_region_id','source_image_sha256','transform','plate_part_ids','supports'}
 if not isinstance(registration,dict) or set(registration)-{'display_projection'}!=required:
  raise ValueError('assembly registration schema invalid')
 view=next((v for v in plan['views'] if v['id']==registration['view_id']),None)
 region=next((r for r in bundle['figure_candidates'] if r['id']==registration['source_region_id']),None)
 if not view or not region or view.get('source_region_id')!=region['id']:
  raise ValueError('assembly registration requires its current view/source region')
 plates=registration['plate_part_ids']
 if not isinstance(plates,dict) or set(plates)!={'upper','lower','vertical'} or len(set(plates.values()))!=3:
  raise ValueError('assembly registration requires three distinct source plates')
 by_part={a['part_id']:a for a in assets}
 for side,part in plates.items():
  asset=by_part.get(part)
  if (part not in view['parts'] or not asset or asset['source_region_id']!=region['id']
      or asset['source_image_sha256']!=registration['source_image_sha256']):
   raise ValueError('assembly registration plate/source image differs')
  if asset['outline_complete'] or (side!='vertical' and contour_has_cycle(asset['visible_segments'])):
   raise ValueError('assembly registration requires open source contours for unknown plate outlines')
 transform=registration['transform']
 if (not isinstance(transform,dict) or set(transform)!={'scale','x','y'}
     or any(type(n) not in (int,float) or not math.isfinite(n) or abs(n)>10000 for n in transform.values())
     or not .05<=transform['scale']<=20):
  raise ValueError('assembly registration requires a common positive scale and translation')
 supports=registration['supports']
 if not isinstance(supports,dict) or len(supports)!=4:
  raise ValueError('assembly registration requires four distinct supports')
 facts={f['id']:f for f in plan['facts'] if f.get('status')=='confirmed'}
 width,height=region['bbox'][2]-region['bbox'][0],region['bbox'][3]-region['bbox'][1]
 seen=set()
 relation_only=set()
 for ident,sides in supports.items():
  if not isinstance(ident,str) or not ident or not isinstance(sides,dict) or set(sides)!={'upper','lower'}:
   raise ValueError('assembly registration requires two sides for every support')
  for contact in sides.values():
   if not isinstance(contact,dict) or not {'source_box','fact_ids'}<=set(contact):
    raise ValueError('assembly registration requires contact source evidence')
   box,ids=contact['source_box'],contact['fact_ids']
   if contact.get('basis')=='source_relation':
    if (set(contact)!={'basis','source_box','fact_ids','convention_id'} or box is not None
        or not isinstance(contact['convention_id'],str)
        or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',contact['convention_id'])):
     raise ValueError('relation-only contact needs a convention and must not claim source pixels')
    relation_only.add(ident)
   else:
    if (set(contact)!={'source_box','fact_ids'} and
        (set(contact)!={'basis','source_box','fact_ids'} or contact['basis']!='source_pixels')):
     raise ValueError('unsupported contact evidence basis')
    if (not isinstance(box,list) or len(box)!=4 or any(type(n)is not int for n in box)
        or not (0<=box[0]<box[2]<=width and 0<=box[1]<box[3]<=height)
        or 2*(box[2]-box[0])>=width or 2*(box[3]-box[1])>=height or tuple(box) in seen):
     raise ValueError('assembly registration needs distinct local contact windows')
    seen.add(tuple(box))
   if (not isinstance(ids,list) or not ids or any(i not in facts for i in ids)
       or any(x['verdict']=='FAIL' for x in validate_facts(bundle,[facts[i] for i in ids]))):
    raise ValueError('assembly registration contact facts are unconfirmed')
 if len(relation_only)>1:
  raise ValueError('relation-only contacts require three directly registered supports')
 validate_display_projection(registration, view.get('expression_scene'))


def build_source_geometry(bundle:dict,plan:dict,approved_assets:list[dict],*,assembly_registration=None)->dict:
 scope=context(bundle,plan);validate_assets(approved_assets,bundle,plan)
 assets=copy.deepcopy(approved_assets)
 result={'contract':CONTRACT,'source_sha256':plan['source_sha256'],'scope_sha256':scope,'assets':assets,'assets_sha256':digest(assets),'coordinate_space':'source_crop_pixels_not_product_dimensions'}
 if assembly_registration is not None:
  validate_assembly_registration(assembly_registration,assets,bundle,plan)
  result['assembly_registration']=copy.deepcopy(assembly_registration)
 return result


def validate_source_geometry(contract:dict,bundle:dict,plan:dict)->list[dict]:
 try:
  if not isinstance(contract,dict)or set(contract)-{'assembly_registration'}!={'contract','source_sha256','scope_sha256','assets','assets_sha256','coordinate_space'}:raise ValueError('geometry schema changed')
  if contract['contract']!=CONTRACT or contract['coordinate_space']!='source_crop_pixels_not_product_dimensions':raise ValueError('geometry contract/coordinate identity changed')
  if contract['source_sha256']!=plan['source_sha256']or contract['scope_sha256']!=context(bundle,plan)or digest(contract['assets'])!=contract['assets_sha256']:raise ValueError('source geometry content changed')
  validate_assets(contract['assets'],bundle,plan)
  if 'assembly_registration' in contract:
   validate_assembly_registration(contract['assembly_registration'],contract['assets'],bundle,plan)
 except (ValueError,KeyError,TypeError,AttributeError)as e:return [{'rule':'SOURCE-GEOMETRY','verdict':'FAIL','message':str(e)}]
 return []
