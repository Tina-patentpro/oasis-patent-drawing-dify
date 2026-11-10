"""Atomic public SDK adapters. No model calls for validation/vector/package paths."""
import hashlib
import re

from .client import MODEL, OpenAIImagesClient
from .diagrams import render_diagram
from .errors import DrawingError, ToolInvokeError
from .file_input import FileSettings, read_file
from .images import MAX_OUTPUT, MAX_REFERENCE, prepare_reference
from .set_delivery import MAX_TOTAL_BYTES, package_set
from .sets import (affected_figures, build_figure_request, canonical, fail,
                   figure_spec_hash, plan_hash, topological_order, validate_plan)


MAX_SET_INPUT_BYTES=MAX_TOTAL_BYTES

def _settings(tool):
    c=tool.runtime.credentials
    return FileSettings(c.get('dify_file_origin')or'',c.get('dify_internal_files_url')or'')


def _validate(tool, parameters, plan):
    old=parameters.get('previous_plan_json')
    affected=affected_figures(old,plan)if old else topological_order(plan)
    for fid in affected:
        if next(f['engine']for f in plan['figures']if f['id']==fid)=='image':
            build_figure_request(plan,fid,'edit'if old else'generate',parameters.get('edit_instruction')or'')
    ids=[f['id']for f in plan['figures']]
    covered={r for f in plan['figures']for r in f['covers']}
    result={'success':True,'plan_json':canonical(plan),'plan_sha256':plan_hash(plan),
        'affected_ids':affected,'unaffected_ids':[fid for fid in ids if fid not in affected],
        'queue':[{'figure_id':fid,'engine':next(f['engine']for f in plan['figures']if f['id']==fid)}for fid in affected],
        'uncovered_relation_ids':[r['id']for r in plan['relations']if r['id']not in covered],
        'structural_validation_passed':True,'professional_review_status':'not_reviewed',
        'requires_human_review':True}
    ref=parameters.get('reference_image')
    if ref is not None and ref!={}:
        fid=parameters.get('reference_figure_id');expected=parameters.get('expected_reference_sha256')
        if fid not in ids or not isinstance(expected,str)or not re.fullmatch('[0-9a-f]{64}',expected):fail('补传引用身份无效')
        raw=read_file(ref,MAX_REFERENCE,_settings(tool));actual=hashlib.sha256(raw).hexdigest()
        if actual!=expected:fail('补传PNG不是指定原图的相同字节')
        prepare_reference(raw)
        result.update(reference_verified=True,reference_figure_id=fid,reference_sha256=actual)
    return [tool.create_json_message(result)]


def _render(tool,parameters,plan):
    fid=parameters.get('figure_id');figure=next((f for f in plan['figures']if f['id']==fid),None)
    if figure is None:fail('图件不存在')
    operation=parameters.get('operation','generate')
    if operation not in {'generate','edit'}:fail('绘图操作无效')
    reference=None;reference_hash=None;normalized_hash=None
    ref=parameters.get('reference_image');refid=parameters.get('reference_figure_id')
    if isinstance(ref,dict)and not ref:ref=None
    expected=parameters.get('expected_reference_sha256')
    edit_instruction=parameters.get('edit_instruction')or''
    if operation=='edit':
        if ref is None or refid!=fid or not isinstance(edit_instruction,str)or not edit_instruction.strip():
            fail('关联修改需要对应原图和明确修改说明')
    if ref is not None:
        if operation=='generate'and refid not in figure['depends_on']:fail('派生原图不是本图的依赖')
        if not isinstance(expected,str)or not re.fullmatch('[0-9a-f]{64}',expected):fail('缺少原图SHA256')
        raw=read_file(ref,MAX_REFERENCE,_settings(tool));reference_hash=hashlib.sha256(raw).hexdigest()
        if reference_hash!=expected:fail('原图SHA256与指定图件不符')
        reference=prepare_reference(raw);normalized_hash=reference.sha256
    request_id,usage=None,None
    if figure['engine']=='vector':
        rendered=render_diagram(plan,figure);blob=rendered['png'];svg=rendered['svg']
        width,height=rendered['width'],rendered['height']
        checks={'label_anchor_count':len(rendered['anchors']),'edge_count':len(rendered['edges']),
                'vector_spec_checked':True,'visual_review_status':'not_reviewed'}
    else:
        request=build_figure_request(plan,fid,operation,edit_instruction)
        response=OpenAIImagesClient(tool.runtime.credentials.get('openai_api_key')).render(request,reference)
        blob=response.image.blob;svg=None;width,height=response.image.width,response.image.height
        request_id,usage=response.request_id,response.usage
        checks={'file_validation_passed':True,'visual_review_status':'not_reviewed'}
    png_hash=hashlib.sha256(blob).hexdigest()
    filename=f'{plan["set_id"]}-r{plan["revision"]}-{fid}-{png_hash[:12]}.png'
    metadata={'filename':filename,'file_name':filename,'mime_type':'image/png','width':width,'height':height}
    record={'figure_id':fid,'title':figure['title'],'engine':figure['engine'],'operation':operation,
        'spec_sha256':figure_spec_hash(plan,fid),'png_sha256':png_hash,'file_name':filename,
        'width':width,'height':height,'byte_count':len(blob),'reference_figure_id':refid,
        'reference_sha256':reference_hash,'normalized_reference_sha256':normalized_hash,
        'request_id':request_id,'usage':usage,'checks':checks,
        'model':MODEL if figure['engine']=='image'else None,
        'professional_review_status':'not_reviewed','requires_human_review':True}
    messages=[tool.create_blob_message(blob=blob,meta=metadata)]
    if svg is not None:
        svgname=filename.removesuffix('.png')+'.svg';record['svg_sha256']=hashlib.sha256(svg).hexdigest()
        messages.append(tool.create_blob_message(blob=svg,meta={'filename':svgname,'file_name':svgname,'mime_type':'image/svg+xml'}))
    messages.append(tool.create_json_message({'success':True,'record':record,
        'plan_sha256':plan_hash(plan),'professional_review_status':'not_reviewed','requires_human_review':True}))
    return messages


def _package(tool,parameters,plan):
    files=parameters.get('images')
    if not isinstance(files,list)or len(files)!=len(plan['figures']):fail('请提供当前图组全部PNG')
    blobs=[];total=0
    for file in files:
        blob=read_file(file,MAX_OUTPUT,_settings(tool));total+=len(blob)
        if total>MAX_SET_INPUT_BYTES:fail('图组PNG合计超过48MiB；已停止读取后续文件')
        blobs.append(blob)
    result=package_set(plan,parameters.get('manifest_json'),blobs)
    prefix=plan['set_id']+'-r'+str(plan['revision'])
    messages=[tool.create_blob_message(blob=result['pdf'],meta={'filename':prefix+'.pdf','mime_type':'application/pdf'}),
              tool.create_blob_message(blob=result['zip'],meta={'filename':prefix+'.zip','mime_type':'application/zip'})]
    messages.append(tool.create_json_message({'success':True,'manifest':result['manifest'],
        'pdf_sha256':hashlib.sha256(result['pdf']).hexdigest(),'zip_sha256':hashlib.sha256(result['zip']).hexdigest(),
        'member_hashes':result['member_hashes'],'requires_human_review':True,
        'professional_review_status':'not_reviewed'}))
    return messages


def invoke_set(tool,parameters,operation):
    try:
        if not isinstance(parameters,dict):fail('工具参数必须为对象')
        plan=validate_plan(parameters.get('plan_json'))
        functions={'validate':_validate,'render':_render,'package':_package}
        if operation not in functions:fail('不支持的图组操作')
        messages=functions[operation](tool,parameters,plan)
    except DrawingError as error:
        raise ToolInvokeError(str(error))from None
    except Exception:
        raise ToolInvokeError('[set_processing_failed] 图组处理或文件封装失败；未输出成功结果，未自动重试')from None
    yield from messages
