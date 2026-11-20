import base64
import io
import json
import zipfile
from copy import deepcopy
from datetime import datetime, timezone

import httpx
import pytest
from PIL import Image, ImageDraw

from patent_drawing.client import OpenAIImagesClient
from patent_drawing.annotations import outside_regions_unchanged
from patent_drawing.errors import SourceCoverageIncomplete
from patent_drawing.operation_store import OperationStore, digest
from patent_drawing.spatial_brief import requirements
from patent_drawing.visibility import check_record_review
from patent_drawing.workspace import Workspace


ORIENTATION = '使邻接鳞片彼此接触，并使其长轴主要沿膜面方向取向'
NETWORK = '利用鳞片面内导热特性与相互接触形成连续传热网络，把局部热量沿膜面扩散'
CONTRAST = '，而不是主要依赖厚度方向导热'
NETWORK_REGION = [90, 200, 670, 310]
CONTACTS = [[[280, 211], [280, 299]], [[470, 211], [470, 299]]]


def fixture(tmp_path, *, detached=False, cited_contrast=True, split_orientation=False,
            source_orientation=ORIENTATION, requests=None, instruction='用二维定性关系表达热流及方向。',
            parallel=None, bent_contacts=False, split_join=False):
    image = Image.new('RGB', (1000, 600), 'white')
    draw = ImageDraw.Draw(image)
    if bent_contacts:
        draw.rectangle((100, 210, 660, 300), outline='black', width=3)
        for x in (280, 470):
            draw.line([(x, 211), (x-12, 255), (x, 299)], fill='black', width=3)
    else:
        for left, right in [(100, 280), (280, 470), (470, 660)]:
            draw.rectangle((left, 210, right, 300), outline='black', width=3)
    y = 370 if detached else 255
    draw.line((160, y, 600, y), fill='black', width=4)
    draw.line([(180, y-10), (160, y), (180, y+10)], fill='black', width=4)
    draw.line([(580, y-10), (600, y), (580, y+10)], fill='black', width=4)
    if split_join:
        draw.rectangle((256, 253, 280, 253), fill='white')
    draw.line([(760, 270), (760, 120)], fill='black', width=4)
    draw.line([(760, 270), (890, 270)], fill='black', width=4)
    output = io.BytesIO()
    image.save(output, format='PNG')
    request_id = 'req_offline_material'
    def respond(request):
        if requests is not None:
            requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, headers={'x-request-id': request_id},
                              json={'data': [{'b64_json': base64.b64encode(output.getvalue()).decode()}]})

    transport = httpx.MockTransport(respond)
    workspace = Workspace(OperationStore(tmp_path), 'owner', allow_models=True,
                          image_client=OpenAIImagesClient('offline-fixture', transport=transport))
    types = ['chemical_material']
    source = '导热膜使用鳞片状碳材料。' + source_orientation + '。' + NETWORK + CONTRAST + '。'
    wid = workspace.ingest([{'name': 'film.txt', 'blob': source.encode()}], technical_types=types)['workspace_id']
    state = workspace.read(wid)
    page = state['bundle']['pages'][0]['id']
    orientation_refs = [('orientation', ORIENTATION)]
    if split_orientation:
        contact, orientation = ORIENTATION.split('，')
        orientation_refs = [('contact', contact), ('orientation', orientation)]
    facts = [{'id': ident, 'status': 'confirmed', 'text': quote,
              'evidence': [{'page_id': page, 'quote': quote}]}
             for ident, quote in [*orientation_refs, ('network', NETWORK + (CONTRAST if cited_contrast else ''))]]
    plan = {'schema_version':'drawing-set-2', 'set_id':'film', 'revision':1, 'source_sha256':state['source_sha256'],
            'title':'面内导热膜', 'patent_type':'invention', 'technical_types':types, 'parts':[],
            'facts':facts, 'views':[{'id':'network','title':'鳞片相接与面内热扩散','engine':'image',
                      'requested_type':'relationship','delivered_type':'relationship','parts':[],
                      'purpose':'表示鳞片相接与连续面内扩散','instruction':instruction,
                      'required_facts':[fact['id'] for fact in facts]}], 'constraints':[], 'unresolved':[]}
    if parallel:
        plan['part_mark_mode'] = 'numbered'
        plan['parts'] = [{'id':'film','name':'导热膜','label':'1'}]
        plan['views'][0]['parts'] = ['film']
        plan['views'].append({**plan['views'][0],'id':'parallel','engine':'vector',
                              'title':'材料关系图','purpose':'表示并列条件','diagram':parallel})
    workspace.stage(wid,plan,image_budget=1)
    record=workspace.render(wid,'network')['record']
    contract=workspace.read(wid)['work']['plan']['visibility_contract']['network']
    review={'view_id':'network','base_sha256':record['base']['sha256'],'spec_sha256':record['base_spec_sha256'],
            'contract_sha256':digest(contract),'request_id':request_id,
            'reviewer':'offline engineering test','reviewed_at':datetime.now(timezone.utc).isoformat(),
            'features':[
                {'feature_id':'source-fidelity','verdict':'PASS','region':[90,110,900,390],'note':'source scope'},
                {'feature_id':'material-flake-contact','verdict':'PASS','region':NETWORK_REGION,
                 'contacts':CONTACTS,'note':'two observed joins between adjacent symbols'},
                {'feature_id':'material-in-plane-heat-path','verdict':'PASS','region':NETWORK_REGION,'note':'two paths from one schematic point',
                 'heat_path':{'origin':[380,255],'arrows':[{'tip':[160,255],'head':[[180,245],[180,265]]},
                                                        {'tip':[600,255],'head':[[580,245],[580,265]]}]}},
                {'feature_id':'material-thickness-direction','verdict':'PASS','region':[735,100,915,295],
                 'direction_axes':{'origin':[760,270],'plane_tip':[890,270],'thickness_tip':[760,120]},
                 'axis_label_positions':{'plane':[773,290],'thickness':[776,144]},
                 'note':'separate directional axes'}]}
    if bent_contacts:
        review['features'][1]['contacts'] = [
            {'upper': [[x, 211], [x-11, 252]], 'lower': [[x-11, 258], [x, 299]]}
            for x in (280, 470)]
    return workspace,wid,contract,review


def test_material_contract_rejects_a_generic_pass_and_unobserved_ink(tmp_path):
    workspace,wid,contract,review=fixture(tmp_path,detached=True)
    generic={**review,'features':review['features'][:1]}
    with pytest.raises(ValueError,match='every feature'):
        workspace.review_visibility(wid,'network',generic)
    with pytest.raises(ValueError,match='material-in-plane-heat-path'):
        workspace.review_visibility(wid,'network',review)
    strict=deepcopy(review)
    strict['features'][2].update(verdict='FAIL',note='double arrow is below the contact network')
    assert workspace.review_visibility(wid,'network',strict)['technical_review']=='BLOCKED'
    assert workspace.preflight(wid)['blocking_state']=='technical_failed'
    assert workspace.package_partial(wid)['quality']['technical_status']=='FAIL'
    assert workspace.read(wid)['current'] is None
    with pytest.raises(ValueError,match='visual review required'):
        workspace.package(wid)


def test_verified_contact_path_and_direction_axes_allow_an_engineering_package(tmp_path):
    workspace,wid,contract,review=fixture(tmp_path)
    with pytest.raises(ValueError,match='request identity'):
        workspace.review_visibility(wid,'network',{**review,'request_id':'req_different'})
    with pytest.raises(ValueError,match='material-thickness-direction'):
        invalid=deepcopy(review)
        invalid['features'][3]['direction_axes']['thickness_tip']=[810,270]
        workspace.review_visibility(wid,'network',invalid)
    assert workspace.review_visibility(wid,'network',review)['technical_review']=='PASS'
    package=workspace.package(wid)
    assert package['manifest']['technical_status']=='UNKNOWN' and not package['manifest']['filing_ready']
    strict=deepcopy(review)
    strict['features'][2].update(verdict='FAIL',note='independent reassessment')
    workspace.review_visibility(wid,'network',strict)
    assert workspace.current_review_block(wid)['blocking_state']=='technical_failed'
    with pytest.raises(ValueError,match='no longer supports complete export'):
        workspace.package(wid,export_current=True)


def test_bent_joins_can_be_recorded_with_visible_branches_above_and_below_shaft(tmp_path):
    workspace, wid, _, review = fixture(tmp_path, bent_contacts=True)
    assert workspace.review_visibility(wid, 'network', review)['technical_review'] == 'PASS'
    package = workspace.package(wid)
    assert not package['manifest']['filing_ready']


def test_bent_join_with_disconnected_branch_cannot_pass_on_coordinates_alone(tmp_path):
    workspace, wid, _, review = fixture(tmp_path, bent_contacts=True, split_join=True)
    with pytest.raises(ValueError, match='material-in-plane-heat-path'):
        workspace.review_visibility(wid, 'network', review)
    assert workspace.preflight(wid)['blocking_state'] == 'awaiting_visual_review'


def test_limited_material_view_keeps_preexisting_generic_review_contract(tmp_path):
    _,_,contract,_=fixture(tmp_path,cited_contrast=False)
    assert [x['feature_id'] for x in contract['features']]==['source-fidelity']


def test_axis_labels_require_safe_placement_and_bind_complete_exports(tmp_path):
    workspace,wid,contract,review=fixture(tmp_path)
    original=workspace.partial(wid)['figures']['network']['record']
    before=workspace.store.get_artifact(workspace.owner,original['png'])
    incomplete=deepcopy(review)
    del incomplete['features'][3]['axis_label_positions']
    legacy={key:value for key,value in contract.items() if key!='material_axis_labels'}
    assert not check_record_review({**incomplete,'contract_sha256':digest(legacy)},original,legacy)
    with pytest.raises(ValueError,match='axis label'):
        workspace.review_visibility(wid,'network',incomplete)
    overlapping=deepcopy(review)
    overlapping['features'][3]['axis_label_positions']['plane']=[773,269]
    with pytest.raises(ValueError,match='axis label'):
        workspace.review_visibility(wid,'network',overlapping)
    assert workspace.preflight(wid)['blocking_state']=='awaiting_visual_review'
    assert workspace.review_visibility(wid,'network',review)['model_calls']==0
    draft=workspace.package_partial(wid)
    complete=workspace.package(wid)
    record=workspace.read(wid)['current']['records']['network']
    assert record['base']==original['base']
    layout=record['material_axis_labeling']
    boxes=[label['box'] for label in layout['labels'].values()]
    assert {label['text'] for label in layout['labels'].values()}=={'膜面方向','厚度方向'}
    with zipfile.ZipFile(io.BytesIO(draft['zip'])) as partial, zipfile.ZipFile(io.BytesIO(complete['zip'])) as full:
        drawn=full.read('figures/network.png')
        assert drawn!=before and drawn==partial.read('figures/network.png')
        assert outside_regions_unchanged(before,drawn,boxes)
        assert '横轴表示膜面方向，竖轴表示厚度方向' in full.read('figure-descriptions.txt').decode()
    assert complete['zip']==workspace.package(wid,export_current=True)['zip']
    changed=deepcopy(review)
    changed['features'][3]['axis_label_positions']['plane']=[777,295]
    with pytest.raises(ValueError,match='completed'):
        workspace.review_visibility(wid,'network',changed)
    assert complete['zip']==workspace.package(wid,export_current=True)['zip']


@pytest.mark.parametrize('contact_label,network_label,spreading_label', [
    ('邻接鳞片彼此接触','形成连续面内传热网络','局部热量沿膜面扩散'),
    ('邻接鳞片彼此接触','利用鳞片面内导热特性\n形成连续传热网络','局部热量沿膜面扩散\n（预期作用）'),
    pytest.param('邻接的鳞片状碳材料\n彼此接触','在{{film.name}}内\n形成连续传热网络',
                 '局部热量沿膜面扩散\n（定性机理）', id='source-bound-natural-phrasing'),
])
@pytest.mark.parametrize('reversed_effect', [False, True])
def test_material_caption_uses_rendered_direction_and_separates_planned_marks(
        tmp_path, reversed_effect, contact_label, network_label, spreading_label):
    diagram={'width':1000,'height':800,'nodes':[
        {'id':ident,'type':'process','box':box,'text':text} for ident,box,text in (
            ('orientation',[30,40,470,140],'鳞片长轴主要沿膜面方向取向'),
            ('contact',[530,40,970,140],contact_label),
            ('network',[260,300,740,400],network_label),
            ('spreading',[260,560,740,660],spreading_label),
        )],'edges':[
        {'source':'orientation','target':'network','points':[[350,140],[350,300]]},
        {'source':'contact','target':'network','points':[[650,140],[650,300]]},
        {'source':'network','target':'spreading','points':[[500,400],[500,560]]},
    ]}
    if reversed_effect:
        diagram['edges'][2]={'source':'spreading','target':'network','points':[[500,560],[500,400]]}
        with pytest.raises(SourceCoverageIncomplete) as failure:
            fixture(tmp_path,parallel=diagram)
        assert [row['rule'] for row in failure.value.findings]==['MATERIAL-HEAT-EFFECT-DIRECTION']
        return
    workspace,wid,_,review=fixture(tmp_path,parallel=diagram)
    workspace.review_visibility(wid,'network',review)
    drawn=workspace.render(wid,'parallel')
    packed=workspace.package(wid)
    with zipfile.ZipFile(io.BytesIO(packed['zip'])) as package:
        assert package.read('figures/parallel.png')==drawn['png']
        descriptions=package.read('figure-descriptions.txt').decode().splitlines()
        assert '由连续传热网络指向局部热量沿膜面扩散' in descriptions[1]
        assert '部件登记（图面编号待核）：1—导热膜' in descriptions[0]
        assert packed['manifest']['figures'][0]['part_mark_status']=='pending'
        assert packed['manifest']['figures'][1]['part_mark_status']=='pending'
    assert packed['zip']==workspace.package(wid,export_current=True)['zip']


def test_material_generation_requests_one_shared_heat_stroke_without_input_label(tmp_path):
    requests = []
    instruction = '局部热量仅用文字起点标识。图内保留“局部热量”“面内热扩散”。'
    workspace,wid,_,_=fixture(tmp_path, requests=requests, instruction=instruction)
    (path, payload), = requests
    prompt = payload['prompt']
    assert path == '/v1/images/generations'
    assert '一根不间断的水平双头箭' in prompt and '两个接合处的内侧' in prompt
    assert '“局部热量”和“面内热扩散”写在图说' in prompt
    assert instruction not in prompt
    assert NETWORK + CONTRAST in prompt
    assert workspace.read(wid)['work']['plan']['views'][0]['instruction'] == instruction


@pytest.mark.parametrize('variant', [
    'node_marks', 'reversed', 'other_use', 'specification', 'view_mark', 'physical_view_mark',
    'embedded_network', 'embedded_reverse', 'embedded_explicit', 'unverified_roles',
    'whole_material_sentence',
])
def test_natural_material_plan_separates_relation_nodes_from_reference_marks(tmp_path, variant):
    from tools.workspace import preview_message

    class Planner:
        def plan(self, system, user, **kwargs):
            return {'text': json.dumps(proposal, ensure_ascii=False)}

    workspace = Workspace(OperationStore(tmp_path), 'owner', allow_models=True, text_client=Planner())
    types = ['chemical_material']
    orientation_quote = ORIENTATION
    if variant == 'whole_material_sentence':
        orientation_quote = '选用由多个石墨烯层组成的鳞片状碳材料制成导热膜，' + orientation_quote
    source = '导热膜使用鳞片状碳材料。' + orientation_quote + '。' + NETWORK + CONTRAST + '。'
    wid = workspace.ingest([{'name': 'film.txt', 'blob': source.encode()}], technical_types=types)['workspace_id']
    state = workspace.read(wid)
    facts = [{'id': key, 'status': 'confirmed', 'text': text,
              'evidence': [{'page_id': state['bundle']['pages'][0]['id'], 'quote': text}]}
             for key, text in [('orientation_contact', orientation_quote), ('heat', NETWORK + CONTRAST)]]
    nodes = {
        'orientation': ('长轴主要沿膜面取向', [40, 40, 460, 140]),
        'contact': ('邻接鳞片彼此接触', [540, 40, 960, 140]),
        'network': ('连续传热网络', [270, 280, 730, 380]),
        'spreading': ('局部热量沿膜面扩散', [270, 520, 730, 620]),
    }
    parts = [{'id': 'film', 'label': '1', 'name': '导热膜'},
             {'id': 'flake', 'label': '2', 'name': '鳞片状碳材料'}]
    parts.extend({'id': key, 'label': str(index), 'name': name}
                 for index, (key, (name, _)) in enumerate(nodes.items(), 3))
    main = {'id': 'main', 'title': '材料热流', 'engine': 'image', 'requested_type': 'relationship',
            'delivered_type': 'relationship', 'parts': [p['id'] for p in parts],
            'purpose': '定性相接和热流', 'required_facts': [f['id'] for f in facts]}
    edges = [('orientation', 'network'), ('contact', 'network'),
             ('spreading', 'network') if variant == 'reversed' else ('network', 'spreading')]
    diagram = {'width': 1000, 'height': 800,
               'nodes': [{'id': 'node_'+key, 'type': 'process', 'box': box, 'text': '{{'+key+'.name}}'}
                         for key, (_, box) in nodes.items()],
               'edges': [{'source': 'node_'+a, 'target': 'node_'+b} for a, b in edges]}
    relationship = {**main, 'id': 'relation', 'engine': 'vector', 'parts': list(nodes), 'diagram': diagram}
    proposal = {'schema_version': 'drawing-set-2', 'title': '导热膜', 'patent_type': 'invention',
                'technical_types': types, 'part_mark_mode': 'numbered', 'parts': parts,
                'facts': facts, 'views': [main, relationship], 'constraints': [], 'unresolved': []}
    embedded = variant.startswith('embedded_')
    if embedded:
        parts[:] = parts[:2] + [{'id': 'network', 'label': '3', 'name': '连续传热网络'}]
        main['parts'] = ['film', 'flake', 'network']
        relationship['parts'] = ['flake', 'network']
        texts = ['{{flake.name}}\n长轴主要沿膜面方向取向', '{{flake.name}}\n邻接鳞片彼此接触',
                 '利用鳞片面内导热特性\n形成{{network.name}}', '局部热量沿膜面扩散']
        for node, text in zip(diagram['nodes'], texts, strict=True):
            node['text'] = text
        if variant == 'embedded_reverse':
            diagram['edges'][-1] = {'source': 'node_spreading', 'target': 'node_network'}
        if variant == 'embedded_explicit':
            main['labels'] = ['3']
    if variant == 'unverified_roles':
        diagram['nodes'][-1]['text'] = '热扩散作用尚需辨识'
    if variant == 'other_use':
        main['instruction'] = '保留{{network.name}}的实际外形引用。'
    if variant == 'specification':
        proposal['specification_labels'] = [p['label'] for p in parts]
    if variant in {'view_mark', 'physical_view_mark'}:
        main['labels'] = ['3'] if variant == 'view_mark' else ['1']
    raw = deepcopy(proposal)
    result = workspace.plan(wid, '规划一套专利附图。')
    assert proposal == raw
    if variant in {'reversed', 'embedded_reverse', 'unverified_roles'}:
        assert result['blocking_state'] == 'planning_coverage_incomplete'
        rule = ('MATERIAL-HEAT-ROLE-UNVERIFIED' if variant == 'unverified_roles'
                else 'MATERIAL-HEAT-EFFECT-DIRECTION')
        assert result['domain_findings'][0]['rule'] == rule
        assert workspace.read(wid)['work'] is None
        return
    if variant in {'other_use', 'specification', 'view_mark', 'embedded_explicit'}:
        assert result['plan']['parts'] == parts
        return
    plan = result['plan']
    assert [part['id'] for part in plan['parts']] == ['film', 'flake']
    assert [view['parts'] for view in plan['views']] == [['film', 'flake'], ['flake'] if embedded else []]
    if variant == 'physical_view_mark':
        assert plan['views'][0]['labels'] == ['1']
    expected_texts = ([text.replace('{{network.name}}', '连续传热网络') for text in texts] if embedded
                      else [name for name, _ in nodes.values()])
    assert [n['text'] for n in plan['views'][1]['diagram']['nodes']] == expected_texts
    assert plan['views'][1]['diagram']['edges'] == diagram['edges'] and plan['facts'] == facts
    assert len(result['adaptation']['material_relationship_marks']['removed']) == (1 if embedded else 4)
    assert workspace.plan(wid, '规划一套专利附图。')['model_calls'] == 0
    offline = Workspace(workspace.store, workspace.owner)
    raw.update(set_id=plan['set_id'], source_sha256=plan['source_sha256'], revision=1)
    with pytest.raises(SourceCoverageIncomplete, match='MATERIAL-RELATION-MARKS'):
        offline.stage(wid, raw, image_budget=1)
    assert offline.read(wid)['work'] is None
    assert offline.stage(wid, plan, image_budget=1)['required_image_calls'] == 1
    contract = offline.read(wid)['work']['plan']['visibility_contract']['main']
    assert contract['contract'] == 'visibility-material-heat-v1'
    summary = preview_message(offline.preview(wid))
    assert '部件登记（规划标号）' in summary
    assert '关系节点保留图中文字' in summary and '图面编号待核' in summary
    assert '3—长轴' not in summary and '3—连续传热网络' not in summary



@pytest.mark.parametrize(('qualifier', 'reverse', 'expected'), [
    ('利用面内导热特性', False, True),
    ('利用面内导热特性', True, False),
    ('利用厚度方向导热', False, None),
    ('利用面内导热特性得到实测值', False, None),
])
def test_material_parallel_orientation_with_source_bound_qualifier(qualifier, reverse, expected):
    from patent_drawing.coverage import material_parallel_heat

    plan = {'parts': [{'id': 'flake', 'label': '2', 'name': '鳞片状碳材料'}]}
    view = {
        'engine': 'vector', 'delivered_type': 'relationship', 'required_facts': ['source_heat'],
        'diagram': {
            'nodes': [
                {'id': 'orientation', 'text': '{{flake.name}}\n长轴主要沿膜面取向\n' + qualifier},
                {'id': 'contact', 'text': '邻接鳞片状碳材料\n彼此接触'},
                {'id': 'network', 'text': '形成连续传热网络'},
                {'id': 'spreading', 'text': '局部热量沿膜面扩散\n（预期作用）'},
            ],
            'edges': [
                {'source': 'orientation', 'target': 'network'},
                {'source': 'contact', 'target': 'network'},
                {'source': 'spreading', 'target': 'network'} if reverse
                else {'source': 'network', 'target': 'spreading'},
            ],
        },
    }
    assert material_parallel_heat(view, plan, bindings={'image': {'source_heat'}}) is expected
    assert material_parallel_heat(view, plan, bindings={}) is None


def test_material_contract_recovers_split_citations_on_one_source_clause(tmp_path):
    workspace,wid,contract,review=fixture(tmp_path,split_orientation=True)
    assert contract['contract']=='visibility-material-heat-v1'
    assert {row['feature_id'] for row in contract['features']}=={row['feature_id'] for row in review['features']}
    state=workspace.read(wid)
    plan=state['work']['plan']
    brief=requirements(state['bundle'],plan,plan['views'][0])
    source_ids={row['id']:row['source_fact_ids'] for row in brief['features']}
    assert source_ids['material-flake-contact']==['contact','orientation']
    assert source_ids['material-in-plane-heat-path']==['contact','network','orientation']


@pytest.mark.parametrize('source_orientation',[
    '不'+ORIENTATION,
    ORIENTATION.replace('，','。'),
    ORIENTATION+'的条件尚待确认',
])
def test_material_contract_does_not_join_disconnected_or_unconfirmed_fragments(tmp_path,source_orientation):
    _,_,contract,_=fixture(tmp_path,split_orientation=True,source_orientation=source_orientation)
    assert [x['feature_id'] for x in contract['features']]==['source-fidelity']
