"""Two bounded observations of one immutable scene, never two designs.

Occluded ink stays unknown in each observation. Only actually retained ink
from the same instance can supply a whole-guide witness after composition.
"""
import copy
import hashlib
import io
from xml.etree import ElementTree as ET

from PIL import Image, ImageChops

from .operation_store import digest
from .projected_relations import observe_projected_relations
from .structural_semantics import finite, validate_structure


def build_panel_guide(contract: dict, scene: dict, *, _member_requirements=None) -> dict:
    def fail(text):
        return {'status':'FAIL','issues':[{'rule':'GUIDE-PANEL','verdict':'FAIL','message':text}]}
    panels=scene.get('panels');canvas=scene.get('canvas')
    if (not isinstance(panels,list)or not 1<=len(panels)<=2 or scene.get('render_mode')!='opaque_surfaces'
            or not isinstance(canvas,list)or len(canvas)!=2
            or any(type(x)is not int or not 400<=x<=2400 for x in canvas)):
        return fail('bounded opaque same-scene panels required')
    def box(value):
        return (isinstance(value,list)and len(value)==4 and all(type(x)is int for x in value)
                and 0<=value[0]<value[2]<=canvas[0]and 0<=value[1]<value[3]<=canvas[1])
    targets=[];ids=set()
    for p in panels:
        if (not isinstance(p,dict)or set(p)-{'id','projection','target','crop','sections'}
                or not isinstance(p.get('id'),str)or not p['id']or p['id']in ids
                or not box(p.get('target'))or not box(p.get('crop'))):
            return fail('panel identity/crop/target invalid; instances cannot change per panel')
        ids.add(p['id']);b=p['target']
        if any(min(b[2],a[2])>max(b[0],a[0])and min(b[3],a[3])>max(b[1],a[1])for a in targets):
            return fail('panel targets overlap; visibility cannot be added through an overpainted region')
        targets.append(b)
        if not isinstance(p.get('projection'),dict)or set(p['projection'])-{'origin','scale','tilt'}:
            return fail('panel projection cannot add geometry')
        if not isinstance(p.get('sections',[]),list)or len(p.get('sections',[]))>8:
            return fail('bounded local sections required')
        for section in p.get('sections',[]):
            if not isinstance(section,dict)or set(section)!={'part_id','convention_id','plane_y'}:
                return fail('section may only select an approved display plane')
            convention=next((c for c in contract.get('conventions',[])if c.get('id')==section['convention_id']),None)
            region=(convention or{}).get('region',[])
            if (not convention or convention.get('kind')!='local_section' or convention.get('not_physical_feature')is not True
                    or section['part_id']not in convention.get('part_ids',[])or len(region)!=4
                    or not all(region[i]<=b[i]for i in (0,1))or not all(b[i]<=region[i]for i in (2,3))):
                return fail('local panel cut is not covered by the bound nonphysical convention')
    from .expression_guide import build_expression_guide, _contact_ink, _paired_contact_masks
    image=Image.new('RGB',tuple(canvas),'white')
    vector=ET.Element('svg',{'xmlns':'http://www.w3.org/2000/svg','width':str(canvas[0]),'height':str(canvas[1])})
    ET.SubElement(vector,'rect',{'width':'100%','height':'100%','fill':'white'})
    best={};source_bindings={};panel_checks=[];contact_regions=[];display_conventions=[]
    projected_channels, projected_fits = [], []
    for p in panels:
        clone=copy.deepcopy(scene);clone.pop('panels');clone['projection']=copy.deepcopy(p['projection'])
        for section in p.get('sections',[]):
            matched=[r for r in clone['instances']if r['part_id']==section['part_id']and r['shape']=='solid']
            if not matched:return fail('section part has no canonical solid')
            for row in matched:row['display_section']={k:section[k]for k in ('convention_id','plane_y')}
        evidence={};g=build_expression_guide(contract,clone,_evidence=evidence,
                                            _member_requirements=_member_requirements)
        if g['status']=='FAIL':return fail('panel geometry invalid: '+str(g.get('issues')))
        crop=p['crop'];target=p['target'];cw,ch=crop[2]-crop[0],crop[3]-crop[1]
        scale=min((target[2]-target[0])/cw,(target[3]-target[1])/ch)
        size=(max(1,round(cw*scale)),max(1,round(ch*scale)))
        offset=(target[0]+(target[2]-target[0]-size[0])//2,target[1]+(target[3]-target[1]-size[1])//2)
        sx,sy=size[0]/cw,size[1]/ch
        with Image.open(io.BytesIO(g['png']))as raw:
            tile=raw.crop(crop).resize(size,Image.Resampling.LANCZOS);image.paste(tile,offset)
        child=ET.SubElement(vector,'svg',{'x':str(offset[0]),'y':str(offset[1]),'width':str(size[0]),'height':str(size[1]),
                                       'viewBox':' '.join(map(str,[crop[0],crop[1],cw,ch]))})
        for node in ET.fromstring(g['svg']):child.append(node)
        owner=evidence['ownership']
        final_owner=owner.crop(crop).resize(size,Image.Resampling.NEAREST)
        panel_contacts = []
        for contact in g.get('contact_regions',[]):
            b=contact['region'];inside=bool(b)and all(crop[i]<=b[i]for i in (0,1))and all(b[i]<=crop[i]for i in (2,3))
            local=([round((b[0]-crop[0])*sx),round((b[1]-crop[1])*sy),
                    round((b[2]-crop[0])*sx),round((b[3]-crop[1])*sy)]if b else[])
            if inside and contact.get('kind')=='supports_at':
                body,plate=contact['instance_ids'];part=contact['member_part_id']
                original=ImageChops.multiply(evidence['member_layers'][body][part]['mask'],
                    owner.point([255 if i==evidence['indices'][body]else 0 for i in range(256)]))
                member=original.crop(crop).resize(size,Image.Resampling.NEAREST).crop(local)
                common=final_owner.crop(local).point([255 if i==evidence['indices'][plate]else 0 for i in range(256)])
                actual=_paired_contact_masks([member,common],contact['instance_ids'])
            else:
                actual=_contact_ink(final_owner,evidence['indices'],local,contact['instance_ids'])if inside else{'visible':False,'ink_pixels':{}}
            panel_contacts.append({**contact,**actual,'panel_id':p['id'],
                                    'region':[n+offset[i%2]for i,n in enumerate(local)]if local else[],
                                    'fully_retained':inside})
        contact_regions.extend(panel_contacts)
        def panel_project(point):
            x, y = evidence['project'](point)
            return (x-crop[0])*sx, (y-crop[1])*sy
        channels, fits = observe_projected_relations(clone, panel_project, final_owner, evidence['indices'],
                                                     tile, panel_contacts, panel_id=p['id'], offset=offset)
        projected_channels.extend(channels)
        projected_fits.extend(fits)
        for r in g['regions']:
            ident=r['instance_id'];index=evidence['indices'][ident]
            full_owned=owner.point([255 if i==index else 0 for i in range(256)])
            owned=full_owned.crop(crop)
            original_visible=owned.histogram()[255]
            owned=owned.resize(size,Image.Resampling.NEAREST)
            own=evidence['layers'][ident].point(lambda v:255 if v<128 else 0).crop(crop).resize(size,Image.Resampling.NEAREST)
            n=owned.histogram()[255];total=own.histogram()[255];bbox=owned.getbbox()
            fraction=min(original_visible/max(1,r['ink_pixels']),n/max(1,total))
            row={**copy.deepcopy(r),'panel_id':p['id'],'ink_pixels':total,'visible_fraction':fraction,
                 'ink_bbox':[bbox[0]+offset[0],bbox[1]+offset[1],bbox[2]+offset[0],bbox[3]+offset[1]]if bbox else None}
            row['contacts']={k:[round((b[0]-crop[0])*sx)+offset[0],round((b[1]-crop[1])*sy)+offset[1],
                                round((b[2]-crop[0])*sx)+offset[0],round((b[3]-crop[1])*sy)+offset[1]]for k,b in r.get('contacts',{}).items()}
            row['contacts_visible']={k:(all(crop[i]<=b[i]for i in (0,1))and all(b[i]<=crop[i]for i in (2,3))
                                        and full_owned.crop(b).getextrema()[1]>128)
                                     for k,b in r.get('contacts',{}).items()}
            if ident in evidence.get('member_layers',{}):
                row['member_regions']=[]
                for part,info in evidence['member_layers'][ident].items():
                    retained=ImageChops.multiply(info['mask'],full_owned).crop(crop)
                    fraction_member=retained.histogram()[255]/max(1,info['total_ink'])
                    final_member=retained.resize(size,Image.Resampling.NEAREST)
                    pixels=final_member.histogram()[255];mb=final_member.getbbox()
                    original_box=info['original_ink'].getbbox()
                    whole=(bool(original_box)and all(crop[i]<=original_box[i]for i in (0,1))
                           and all(original_box[i]<=crop[i]for i in (2,3)))
                    row['member_regions'].append({'part_id':part,'role':info['role'],'ink_pixels':pixels,
                        'visible_fraction':fraction_member,
                        'projection_fully_retained':whole,
                        'occluded_fraction_by_part':{
                            other:ImageChops.multiply(info['original_ink'],occluder['occupied']).crop(crop).histogram()[255]/max(1,info['total_ink'])
                            for other,occluder in evidence['member_layers'][ident].items()
                            if occluder['index']>info['index']},
                        'ink_bbox':[mb[0]+offset[0],mb[1]+offset[1],mb[2]+offset[0],mb[3]+offset[1]]if mb else None})
                    for side,b in r.get('contacts',{}).items():
                        if r['contact_members'][side]!=part:continue
                        inside=all(crop[i]<=b[i]for i in (0,1))and all(b[i]<=crop[i]for i in (2,3))
                        local=[round((b[0]-crop[0])*sx),round((b[1]-crop[1])*sy),
                               round((b[2]-crop[0])*sx),round((b[3]-crop[1])*sy)]
                        row['contacts_visible'][side]=(inside and any(c.get('kind')=='supports_at'
                            and c.get('panel_id')==p['id'] and c.get('side')==side
                            and c.get('instance_ids',[None])[0]==ident and c.get('visible')is True
                            for c in contact_regions))
            if ident not in best or fraction>best[ident]['visible_fraction']:best[ident]=row
        source_bindings.update({b['instance_id']:b for b in g.get('source_region_bindings',[])})
        for convention in g.get('display_conventions',[]):
            observed=next((r for r in g['regions']if r['instance_id']==convention['instance_id']),None)
            if observed and observed.get('ink_bbox'):
                box=observed['ink_bbox']
                retained=[max(box[0],crop[0]),max(box[1],crop[1]),min(box[2],crop[2]),min(box[3],crop[3])]
                if retained[0]<retained[2]and retained[1]<retained[3]:
                    display_conventions.append({**convention,'panel_id':p['id']})
        panel_checks.append({'panel_id':p['id'],'local_status':g['status'],'source_scene_sha256':digest(scene),
                             'same_instances':True,'crop':crop,'target':target,'sections':p.get('sections',[])})
    png=io.BytesIO();image.save(png,format='PNG');svg=ET.tostring(vector)
    regions=list(best.values())
    result={'status':'UNKNOWN','png':png.getvalue(),'svg':svg,'regions':regions,'visible_regions':copy.deepcopy(regions),
            'roles':{r['instance_id']:r['role']for r in regions},'relations':copy.deepcopy(scene['relations']),
            'panel_ids':[p['id']for p in panels],'panels':panel_checks,'scene_sha256':digest(scene),'contract_sha256':digest(contract),
            'contact_regions':contact_regions,
            'technical_shape_accuracy':'not_verified','coordinate_space':'illustrative_units',
            'visibility_model':'opaque_surfaces_with_ink_ownership','source_unknowns':copy.deepcopy(contract.get('unknowns',[])),
            'source_region_bindings':list(source_bindings.values())}
    if _member_requirements is not None:
        result['member_visibility_requirements']=copy.deepcopy(_member_requirements)
    if display_conventions:result['display_conventions']=display_conventions
    if projected_channels:result['projected_channels']=projected_channels
    if projected_fits:result['projected_pin_fits']=projected_fits
    checked=validate_structure(scene,result,contract,member_requirements=_member_requirements);result.update(status=checked['status'],issues=checked['checks'])
    result['guide_sha256']=digest({'svg':hashlib.sha256(svg).hexdigest(),'png':hashlib.sha256(result['png']).hexdigest(),
                                   'regions':regions,'scene_sha256':result['scene_sha256'],'contract_sha256':result['contract_sha256'],
                                   'panels':panel_checks,'contact_regions':contact_regions,
                                   **{k:result[k] for k in ('projected_channels','projected_pin_fits') if k in result}})
    return result
