"""A bounded illustrative layout compiled from privately reviewed typed roles.

Counts, coordinates and panel views are drawing choices, not source facts or
manufacturing dimensions. The builder never approves its own candidate.
"""
import copy


def build_cable_grounding_scene(contract: dict, roles: dict, part_ids: list) -> dict:
    required={'busbar_body','pcb','integral_pin','deformable_rib','plated_hole','external_wall',
              'partition_wall','signal_wire','rear_trough','shielded_cable','weld_opening','shield'}
    def fail(text):return {'status':'FAIL','issues':[text],'scene':None}
    if (not isinstance(roles,dict)or not required<=roles.keys()or set(roles)-required-{'drain_wire'}
            or not isinstance(part_ids,list)or any(not isinstance(v,str)or v not in part_ids for v in roles.values())):
        return fail('complete source role mapping to actual stable parts required')
    facts={f['predicate']:f for f in contract.get('facts',[])if f.get('certainty')=='confirmed'}
    kinds={'channel_partition','integral_pin','rib_in_plated_hole','conductive_path','cable_weld_access','trough_shield_contact'}
    if not kinds<=facts.keys():return fail('source-confirmed channel, fit, shield contact and cable access required')
    access=facts['cable_weld_access'];target='shield'if access['subject']==roles['shield']else'drain_wire'
    if target not in roles or access['subject']!=roles[target]or access['object']!=roles['busbar_body']:
        return fail('chosen weld access target must match the reviewed source relation')
    section=next((c for c in contract.get('conventions',[])if c.get('kind')=='local_section'
                  and c.get('not_physical_feature')is True
                  and {roles[r]for r in ['busbar_body','pcb','integral_pin','deformable_rib','plated_hole']}<=set(c.get('part_ids',[]))),None)
    if not section:return fail('bound nonphysical local fit section required')
    fid={k:facts[k]['id']for k in kinds};rows=[]
    def add(ident,role,shape,bounds,kinds,**extra):
        rows.append({'id':ident,'part_id':roles[role],'role':role,'shape':shape,'bounds':bounds,
                     'fact_ids':[fid[k]for k in kinds],**extra})
    add('body','busbar_body','solid',[-11,-5,6,8,5,7],['channel_partition','integral_pin','conductive_path','cable_weld_access'])
    extent=next((c for c in contract.get('conventions',[])if c.get('kind')=='local_omission'
                 and c.get('not_physical_feature')is True and roles['busbar_body']in c.get('part_ids',[])),None)
    if extent:rows[-1]['display_omission']={'convention_id':extent['id']}
    add('pcb','pcb','sectioned_board',[-9,-6,1,9,6,3],['rib_in_plated_hole'])
    add('pin','integral_pin','cylinder',[-1,-1,0,1,1,6],['integral_pin','rib_in_plated_hole','conductive_path'])
    add('rib','deformable_rib','axial_rib',[.8,-.25,1.5,1.5,.25,2.5],['rib_in_plated_hole'],parent_id='pin',
        footprint=[[.8,-.25],[1.5,-.08],[1.5,.08],[.8,.25]])
    add('hole','plated_hole','plated_bore',[-2,-2,1,2,2,3],['rib_in_plated_hole','conductive_path'],inner_radius=1.5)
    for ident,role,y in [('wall1','external_wall',-4),('partition','partition_wall',0),('wall2','external_wall',4)]:
        add(ident,role,'wall',[-7.5,y-1.5,7,5.5,y+.5,9],['channel_partition'],
            path=[[-7,y,7],[0,y,7],[5,y-1,7]],thickness=.6,height=2)
    for ident,y in [('wire1',-2),('wire2',2)]:
        add(ident,'signal_wire','wire',[-6.2,y-1.2,7.2,4.2,y+.2,7.8],['channel_partition'],
            path=[[-6,y,7.5],[0,y,7.5],[4,y-1,7.5]])
    add('cable','shielded_cable','cable_shell',[-13,-1,7,-9,1,9],['trough_shield_contact'])
    add('shield','shield','cable_band',[-9,-.9,7.1,-7,.9,8.9],['trough_shield_contact']+(['cable_weld_access']if target=='shield'else[]))
    for ident,x0,x1,y in [('trough1',-9,-8.7,1.15),('trough2',-7.3,-6,1.15),('trough3',-9,-6,-1.15)]:
        add(ident,'rear_trough','wall',[x0,y-.25,7,x1,y+.25,9.5],['trough_shield_contact','cable_weld_access'],
            path=[[x0,y,7],[x1,y,7]],thickness=.5,height=2.5)
    add('opening','weld_opening','access_gap',[-8.7,.9,7,-7.3,1.4,9.5],['cable_weld_access'])
    if target=='drain_wire':
        add('drain','drain_wire','wire',[-8.1,.1,7.9,-7.9,1,9],['cable_weld_access'],path=[[-8,.2,8.9],[-8,.9,8]])
    relations=[
        {'kind':'channel_partition','body':'body','walls':['wall1','wall2'],'partitions':['partition'],'wires':['wire1','wire2'],'fact_ids':[fid['channel_partition']]},
        {'kind':'integral_pin','body':'body','pin':'pin','fact_ids':[fid['integral_pin']]},
        {'kind':'rib_in_plated_hole','pin':'pin','hole':'hole','board':'pcb','ribs':['rib'],'fact_ids':[fid['rib_in_plated_hole']]},
        {'kind':'conductive_path','path':['body','pin','hole'],'fact_ids':[fid['conductive_path']]},
        {'kind':'trough_shield_contact','cable':'cable','shield':'shield','troughs':['trough1','trough2','trough3'],'fact_ids':[fid['trough_shield_contact']]},
        {'kind':'cable_weld_access','body':'body','opening':'opening','target':'shield'if target=='shield'else'drain',
         'troughs':['trough1','trough2','trough3'],'access_path':[[-8,4,8],[-8,1.15,8],[-8,.9,8]],'fact_ids':[fid['cable_weld_access']]}]
    scene={'schema_version':1,'coordinate_space':'illustrative_units','cardinality_scope':'illustrative_only',
           'canvas':[1536,1024],'projection':{'origin':[768,640],'scale':28,'tilt':.9},
           'render_mode':'opaque_surfaces','instances':rows,'relations':relations,
           'panels':[{'id':'assembly','projection':{'origin':[768,640],'scale':28,'tilt':.9},'target':[10,10,980,1014],'crop':[250,20,1300,1000]},
                     {'id':'contact','projection':{'origin':[768,700],'scale':30,'tilt':.35},'target':[1000,210,1526,990],
                      'crop':[640,480,900,760],'sections':[{'part_id':roles['busbar_body'],'convention_id':section['id'],'plane_y':0}]}]}
    return {'status':'CANDIDATE','scene':scene,'source_facts_unchanged':True,
            'source_sha256':contract.get('source_sha256'),'weld_target':roles[target],
            'scope':'illustrative observation layout only; actual image/source/professional acceptance pending'}
