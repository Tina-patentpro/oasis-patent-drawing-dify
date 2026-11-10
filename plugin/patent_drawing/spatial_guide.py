"""Deterministic source-contour layout, never invented CAD or technical approval."""
import copy,io,math
from PIL import Image,ImageDraw
from .operation_store import digest

CONTRACT='spatial-guide-036-r1'


def build_expression_guide(contract: dict, scene: dict, *, _member_requirements=None,
                           _assembly_registration=None) -> dict:
 from .expression_guide import build_expression_guide as build
 return build(contract, scene, _member_requirements=_member_requirements,
              _assembly_registration=_assembly_registration)


def box_valid(b,w,h):
 return isinstance(b,list)and len(b)==4 and all(type(x)is int for x in b)and 0<=b[0]<b[2]<=w and 0<=b[1]<b[3]<=h


def overlap(a,b):return min(a[2],b[2])>max(a[0],b[0])and min(a[3],b[3])>max(a[1],b[1])


def check_guide_visibility(guide:dict)->list[dict]:
 issues=[]
 def fail(message):issues.append({'rule':'GUIDE-VISIBILITY','verdict':'FAIL','message':message})
 w,h=guide['canvas'];rows=guide['instance_map'];seen=set();regions=[]
 for row in rows:
  b=row['body']
  if row['instance_id']in seen or row.get('kind')!='support_body' or not box_valid(b,w,h):fail('distinct support bodies required')
  seen.add(row['instance_id'])
  if any(overlap(b,old)for old in regions):fail('body regions overlap')
  regions.append(b)
  for name in ['upper_contact','lower_contact']:
   c=row[name]
   if not box_valid(c,w,h)or not (b[0]<=c[0]<c[2]<=b[2]and b[1]<=c[1]<c[3]<=b[3]):fail('support interface outside actual body')
   if any(overlap(c,o)for o in guide.get('occluders',[])):fail('support interface occluded')
  if any(overlap(b,o)for o in guide.get('occluders',[])):fail('support body occluded')
 return issues


def make_guide(geometry:dict,layout:dict)->dict:
 if (geometry.get('contract')!='source-geometry-036-r1'or digest(geometry['assets'])!=geometry['assets_sha256']):raise ValueError('source geometry modified')
 if set(layout)!={'canvas','coordinate_space','instances','plates'}or layout['coordinate_space']!='illustrative_pixels':raise ValueError('guide cannot invent structure or product dimensions')
 canvas=layout['canvas']
 if not isinstance(canvas,list)or len(canvas)!=2 or any(type(x)is not int or not 400<=x<=2400 for x in canvas):raise ValueError('guide canvas capacity')
 w,h=canvas;assets={a['part_id']:a for a in geometry['assets']};instances=copy.deepcopy(layout['instances']);plates=layout['plates']
 if not isinstance(instances,list)or not 1<=len(instances)<=24 or not isinstance(plates,list)or len(plates)>24:raise ValueError('guide instance capacity')
 for row in instances:
  if set(row)!={'instance_id','part_id','kind','body','upper_contact','lower_contact'}or row['part_id']not in assets or not isinstance(row['instance_id'],str):raise ValueError('guide instance/source invalid')
 for pid,a in assets.items():
  n=sum(r['part_id']==pid for r in instances)+sum(r.get('part_id')==pid for r in plates);c=a['cardinality']
  if ('exact'in c and n!=c['exact'])or('minimum'in c and n<c['minimum']):raise ValueError('guide cardinality differs from source')
 occluders=[]
 for p in plates:
  if set(p)!={'part_id','box','display'}or p['part_id']not in assets or not box_valid(p['box'],w,h)or p['display']not in {'outline_only','source_opaque'}:raise ValueError('plate guide/source invalid')
  if p['display']=='source_opaque':
   if not assets[p['part_id']]['outline_complete']:raise ValueError('cannot close unseen source plate contour')
   occluders.append(p['box'])
 meta={'contract':CONTRACT,'canvas':canvas,'instance_map':instances,'occluders':occluders,'geometry_sha256':digest(geometry),'layout_sha256':digest(layout),'technical_shape_accuracy':'not_verified','coordinate_space':'illustrative_pixels'}
 if check_guide_visibility(meta):raise ValueError('guide visibility fails')
 im=Image.new('RGB',(w,h),'white');draw=ImageDraw.Draw(im);mark=Image.new('RGBA',(w,h),(0,0,0,0));md=ImageDraw.Draw(mark);svg=[]
 def trace(pid,target,fill=False,painter=None,emit_svg=True):
  painter=painter or draw
  paths=assets[pid]['visible_segments'];xs=[xy[0]for p in paths for xy in p];ys=[xy[1]for p in paths for xy in p]
  dx=max(xs)-min(xs);dy=max(ys)-min(ys)
  if dx<=0 or dy<=0:raise ValueError('source contour has no spatial extent')
  scale=min((target[2]-target[0])/dx,(target[3]-target[1])/dy)
  ox=target[0]+((target[2]-target[0])-dx*scale)/2;oy=target[1]+((target[3]-target[1])-dy*scale)/2
  for path in paths:
   points=[(round(ox+(x-min(xs))*scale,2),round(oy+(y-min(ys))*scale,2))for x,y in path]
   if fill:painter.polygon(points,fill='white')
   painter.line(points,fill='black',width=3)
   if emit_svg:svg.append('<polyline points="'+' '.join(str(x)+','+str(y)for x,y in points)+'" fill="none" stroke="black" stroke-width="3"/>')
 for p in plates:trace(p['part_id'],p['box'],p['display']=='source_opaque')
 rendered=[]
 for row in instances:
  layer=Image.new('L',(w,h),255)
  trace(row['part_id'],row['body'],painter=ImageDraw.Draw(layer),emit_svg=False)
  contacts={name:layer.crop(row[name]).getextrema()[0]<128 for name in ['upper_contact','lower_contact']}
  if not all(contacts.values()):raise ValueError('rendered support contact is not visible in actual source-contour pixels')
  ink=layer.point(lambda p:255 if p<128 else 0)
  rendered.append({'instance_id':row['instance_id'],'actual_ink_bbox':list(ink.getbbox()),'contacts_have_component_ink':contacts})
  trace(row['part_id'],row['body']);md.rectangle(row['body'],outline=(255,0,0,220),width=2)
  for name in ['upper_contact','lower_contact']:md.rectangle(row[name],outline=(0,0,255,220),width=2)
 def png(image):out=io.BytesIO();image.save(out,format='PNG');return out.getvalue()
 meta['rendered_instance_map']=rendered
 meta.update(png=png(im),markup=png(mark),svg=('<svg xmlns="http://www.w3.org/2000/svg" width="'+str(w)+'" height="'+str(h)+'">'+''.join(svg)+'</svg>').encode())
 return meta


def default_layout(geometry):
 """Candidate placement only; operator must review actual derived guide."""
 supports=[a for a in geometry['assets']if a['properties']['external_form']in {'closed_sleeve','open_spring'}]
 plates=[a for a in geometry['assets']if a not in supports]
 if len(supports)!=1 or len(plates)>2:raise ValueError('needs_source: provide an explicitly reviewed component layout')
 a=supports[0];n=a['cardinality'].get('exact',a['cardinality'].get('minimum'))
 if n not in {1,4}:raise ValueError('needs_source: no automatic layout for unspecified component distribution')
 positions=[(500,250)]if n==1 else[(400,180),(1170,300),(240,430),(1010,550)]
 xs=[x for path in a['visible_segments'] for x,y in path];ys=[y for path in a['visible_segments'] for x,y in path]
 if max(xs)==min(xs) or max(ys)==min(ys):raise ValueError('source contour lacks body extent')
 body_height=round(90*(max(ys)-min(ys))/(max(xs)-min(xs)))
 if not 30<=body_height<=450:raise ValueError('needs_source: source contour not suitable for default guide')
 contact_height=min(15,body_height//4)
 rows=[{'instance_id':'instance_'+str(i),'part_id':a['part_id'],'kind':'support_body','body':[x,y,x+90,y+body_height],
        'upper_contact':[x,y,x+90,y+contact_height],'lower_contact':[x,y+body_height-contact_height,x+90,y+body_height]}for i,(x,y)in enumerate(positions,1)]
 slots=[[180,110,1360,580],[170,430,1400,930]]
 return {'canvas':[1536,1024],'coordinate_space':'illustrative_pixels','instances':rows,
         'plates':[{'part_id':p['part_id'],'box':slots[i],'display':'outline_only'}for i,p in enumerate(plates)]}
