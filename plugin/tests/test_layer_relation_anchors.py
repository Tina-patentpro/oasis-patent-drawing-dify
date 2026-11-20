from copy import deepcopy
from io import BytesIO
import json
from zipfile import ZipFile

from patent_drawing.diagrams import render_diagram
from patent_drawing.delivery_v2 import package_drawings
from patent_drawing.planner_contract import bind_existing_technical_objects


def test_layer_relationship_labels_point_to_the_disclosed_interface():
    quote = '封装基板与中介结构之间可设底部填充，和芯片热压接合层分开。'
    source = {'pages': [{'id': 'p1', 'text': quote}]}
    plan = {'technical_types': ['semiconductor'],
            'parts': [{'id': 'core', 'name': '中介基板', 'label': '41'},
                      {'id': 'fill', 'name': '底部填充', 'label': '26'}],
            'facts': [{'id': 'f', 'status': 'confirmed', 'evidence': [{'page_id':'p1','quote':quote}]}],
            'views': [{'id':'overview','parts':['core','fill'],'required_facts':['f'],
                       'engine':'vector','delivered_type':'relationship',
                       'diagram': {'width': 900, 'height': 750, 'primitives': [
                           {'type':'polyline','id':'base_edge','part':'core','points':[[280,260],[700,260]]},
                           {'type':'polyline','id':'fill_upper','part':'fill','points':[[280,360],[700,360]]},
                           {'type':'polyline','id':'fill_lower','part':'fill','points':[[280,480],[700,480]]},
                       ]}}]}
    untouched=deepcopy(plan)
    unconfirmed={'pages':[{'id':'p1','text':'材料边界待确认。'}]}
    assert bind_existing_technical_objects(unconfirmed, untouched)==[]
    assert untouched==plan
    bind_existing_technical_objects(source,plan)
    rendered=render_diagram(plan,plan['views'][0])
    assert [(label['part'],label['text']) for label in rendered['anchors']] == [('core','41'),('fill','26')]
    targets={row['part']:row['target'] for row in rendered['anchors']}
    assert targets['core'] == [280,260]
    assert targets['fill'][0] < 280 and targets['fill'][1] == 420


def test_layer_composite_mark_has_one_bracket_and_unshown_objects_leave_public_index():
    quote = '在基板上形成含导电图案和绝缘层的再分布结构。'
    source = {'pages':[{'id':'p1','text':quote}]}
    part = [{'id':key, 'name':name, 'label':str(i)}
            for i,(key,name) in enumerate([('group','再分布结构'),('wire','导电图案'),
                                           ('resin','绝缘层'),('mold','模制材料')],1)]
    plan = {'technical_types':['semiconductor'],'parts':part,'revision':1,
            'facts':[{'id':'stack','status':'confirmed','evidence':[{'page_id':'p1','quote':quote}]}],
            'views':[{'id':'cross','parts':['group','wire','resin'],'required_facts':['stack'],
                      'engine':'vector','delivered_type':'layer_section','diagram':{
                          'width':900,'height':720,'primitives':[
                              {'type':'rect','id':'stripe','part':'wire','box':[230,230,510,255]},
                              {'type':'rect','id':'insulation','part':'resin','box':[225,255,515,315]},
                          ]}}]}
    bind_existing_technical_objects(source,plan)
    view=plan['views'][0]
    output=render_diagram(plan,view)
    label,=output['anchors']
    assert label['part']=='group' and label['text']=='1'
    assert label['target'][0]>515 and label['target'][1] in range(230,315)
    archive=package_drawings(plan,[{'id':'cross','png':output['png'],'svg':output['svg']}])
    with ZipFile(BytesIO(archive['zip'])) as data:
        assert {item['id'] for item in json.loads(data.read('plan.json'))['parts']}=={'group','wire','resin'}
