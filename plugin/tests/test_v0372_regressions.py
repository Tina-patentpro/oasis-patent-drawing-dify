import pytest


def test_structural_confirmation_and_history_commands_reach_workspace():
    from patent_drawing.intake import command

    assert command('确认结构示意改号')
    assert command('导出结构示意版本 ' + 'a' * 64)


def test_preflight_stops_before_disabled_image_after_available_local_figures(tmp_path, monkeypatch):
    from patent_drawing.operation_store import OperationStore
    from patent_drawing.workspace import Workspace

    w = Workspace(OperationStore(tmp_path / 'state'), 'owner')
    wid = w.ingest([{'name': 'source.txt', 'blob': b'source'}])['workspace_id']
    state = w.read(wid)
    state['work'] = {'plan': {'views': [{'id': 'image', 'engine': 'image'}]},
                     'base_specs': {'image': 'base'}, 'specs': {'image': 'full'}}
    w.save(wid, state)
    monkeypatch.setattr(w, 'execution_state', lambda _: {'route': 'execute', 'blocking_state': None})
    monkeypatch.setattr(w, 'resume', lambda _: {'queue': ['image']})
    monkeypatch.setattr(w, 'partial', lambda _: {'figures': {}, 'missing_ids': ['image']})
    result = w.preflight(wid)
    assert result['blocking_state'] == 'model_execution_disabled'
    assert result['route'] == 'reply' and result['model_calls'] == 0


def test_view_prompt_keeps_explicit_technical_scope_out_of_workflow_prose():
    from patent_drawing.visual_request import compile_visual_request

    use = '屏蔽电缆到电路板的接地装配'
    fact = '外壁和分隔壁限定通道'
    plan = {'source_sha256': 'source', 'title': use,
            'facts': [{'id': 'wall', 'status': 'confirmed', 'text': fact,
                       'evidence': [{'page_id': 'p1', 'quote': fact}]}],
            'constraints': ['本轮仅形成规划候选，不启动图像生成。', '未知厚度不绘定。'],
            'unresolved': ['连续壁延伸待核']}
    view = {'id': 'total', 'purpose': '表达电缆侧通道', 'required_facts': ['wall']}
    result = compile_visual_request({'pages': [{'id': 'p1', 'text': fact}]}, plan, view, None)
    assert use in result['prompt'] and fact in result['prompt']
    assert '本轮仅形成规划候选' not in result['prompt']
    assert '未知厚度不绘定' in result['prompt']
    assert '连续壁延伸待核' in result['prompt']
    assert 'page_id' not in result['prompt']
    assert result['fact_coverage'] == ['wall']


def test_flare_uses_four_ray_guide_with_matching_canvas_and_no_labels():
    from patent_drawing.spatial_brief import panel_beam_reference

    reference = panel_beam_reference()
    assert (reference.width, reference.height) == (1536, 1024)
    assert reference.mime_type == 'image/png'


def test_opening_label_endpoint_is_inside_the_schematic_gap():
    from patent_drawing.relation_details import _opening

    _, labels = _opening()
    _, leader = labels[-1]
    x, y = leader[-1]
    assert 480 < x < 605 and 183 < y < 211
    assert (x, y) != (545, 199)


def test_linked_history_does_not_render_on_retrieval(tmp_path, monkeypatch):
    from patent_drawing.operation_store import OperationStore
    from patent_drawing.workspace import Workspace

    workspace = Workspace(OperationStore(tmp_path / 'history'), 'owner')
    wid = workspace.ingest([{'name': 'source.txt', 'blob': b'source'}])['workspace_id']
    state = workspace.read(wid)
    packed = {'pdf': b'drawing', 'zip': b'bundle', 'reference_revision': 3}
    workspace._store_engineering_export(state, packed, audit=False)
    workspace.save(wid, state)
    restarted = Workspace(OperationStore(tmp_path / 'history'), 'owner')
    monkeypatch.setattr(restarted, '_engineering_reference_context',
                        lambda *args, **kwargs: pytest.fail('history must not re-render'))
    assert restarted.package_engineering_reference(wid, revision=3) == packed
