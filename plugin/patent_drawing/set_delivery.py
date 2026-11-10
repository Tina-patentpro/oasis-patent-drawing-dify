"""Bind actual PNGs to current specs and build an offline, raster A4 PDF/ZIP."""
import base64
import hashlib
import json
import re
import zipfile
import zlib
from io import BytesIO

from PIL import Image, ImageDraw

from .client import safe_request_id, safe_usage
from .diagrams import _lines, font, render_diagram
from .images import prepare_output
from .sets import canonical, fail, figure_spec_hash, parse_object, plan_hash, validate_plan

MAX_TOTAL_BYTES = 48 * 1024 * 1024
MAX_PDF_BYTES = 64 * 1024 * 1024


def _caption(value, face_factory=None, centered=False):
    face_factory = face_factory or font
    image=Image.new('RGB',(1400,140),'white');draw=ImageDraw.Draw(image)
    lines=_lines(value,face_factory(30),1360)
    if len(lines)>3:fail('图题过长，无法排版')
    for i,line in enumerate(lines):draw.text(((1400-face_factory(30).getlength(line))/2 if centered else 20,10+i*40),line,font=face_factory(30),fill='black',anchor='lt')
    out=BytesIO();image.save(out,format='PNG');return out.getvalue()


def _pdf(pages, cn_layout=False):
    objects=[b'',b''];page_ids=[];compressed_total=0
    def add(value):objects.append(value);return len(objects)
    def stream(data,attributes=b''):
        return b'<< '+attributes+b' /Length '+str(len(data)).encode()+b' >>\nstream\n'+data+b'\nendstream'
    def image_object(blob):
        nonlocal compressed_total
        with Image.open(BytesIO(blob))as source:
            source.load()
            if 'A' in source.getbands():
                image=Image.new('RGB',source.size,'white');image.paste(source,mask=source.getchannel('A'))
            else:image=source.convert('RGB')
            width,height=image.size;data=zlib.compress(image.tobytes(),6)
        compressed_total+=len(data)
        if compressed_total>MAX_PDF_BYTES:fail('PDF输出容量超限')
        return add(stream(data,f'/Type /XObject /Subtype /Image /Width {width} /Height {height} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode'.encode())),width,height
    for i,(blob,caption)in enumerate(pages,1):
        from .annotations import regular_font
        iid,width,height=image_object(blob);cid,_,_=image_object(_caption(caption,regular_font if cn_layout else None,centered=cn_layout))
        if cn_layout:
            # A4: left/top 25mm, right/bottom 15mm. Caption BELOW drawing.
            scale=min(480/width,630/height);w,h=width*scale,height*scale;x=71+(480-w)/2;y=130+(630-h)/2
            commands=(f'q {w:.3f} 0 0 {h:.3f} {x:.3f} {y:.3f} cm /I Do Q\n'
                      f'q 480 0 0 48 71 {y-55:.3f} cm /C Do Q\n'
                      f'BT /F1 10 Tf 305 48 Td ({i}) Tj ET').encode()
        else:
            scale=min(525/width,650/height);w,h=width*scale,height*scale;x=(595.276-w)/2;y=80+(650-h)/2
            commands=(f'q {w:.3f} 0 0 {h:.3f} {x:.3f} {y:.3f} cm /I Do Q\n'
                      f'q 525 0 0 52.5 35 767 cm /C Do Q\n'
                      f'BT /F1 9 Tf 285 30 Td ({i} / {len(pages)}) Tj ET').encode()
        content=add(stream(commands))
        page_ids.append(add(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595.276 841.89] /Resources << /XObject << /I {iid} 0 R /C {cid} 0 R >> /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> /Contents {content} 0 R >>'.encode()))
    objects[0]=b'<< /Type /Catalog /Pages 2 0 R >>'
    objects[1]=f'<< /Type /Pages /Count {len(page_ids)} /Kids ['.encode()+b' '.join(f'{p} 0 R'.encode()for p in page_ids)+b'] >>'
    out=bytearray(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n');offsets=[0]
    for i,obj in enumerate(objects,1):
        offsets.append(len(out));out.extend(f'{i} 0 obj\n'.encode()+obj+b'\nendobj\n')
    start=len(out);out.extend(f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]:out.extend(f'{offset:010d} 00000 n \n'.encode())
    out.extend(f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n'.encode())
    if len(out)>MAX_PDF_BYTES:fail('PDF输出容量超限')
    return bytes(out)


def package_set(plan, manifest, images):
    plan=validate_plan(plan);manifest=parse_object(manifest)
    if (manifest.get('schema_version')!='drawing-set-result-1' or manifest.get('set_id')!=plan['set_id']
            or manifest.get('revision')!=plan['revision'] or manifest.get('plan_sha256')!=plan_hash(plan)):
        fail('图组结果与当前计划版本不匹配')
    if not isinstance(images,list)or len(images)!=len(plan['figures'])or any(not isinstance(b,bytes)for b in images):
        fail('实际PNG图件缺失或数量不匹配')
    if sum(len(b)for b in images)>MAX_TOTAL_BYTES:fail('图组PNG合计超过48MiB')
    records=manifest.get('figures')
    if not isinstance(records,list)or len(records)!=len(plan['figures']):fail('图组结果清单不完整')
    record_map={}
    for record in records:
        if not isinstance(record,dict)or record.get('figure_id')in record_map:fail('图件结果无效或重复')
        record_map[record.get('figure_id')]=record
    if set(record_map)!={f['id']for f in plan['figures']}:fail('图组包含错误图件身份')
    data_map={hashlib.sha256(blob).hexdigest():blob for blob in images}
    payloads={};exported=[];pages=[];total_pixels=0
    for index,figure in enumerate(plan['figures'],1):
        fid=figure['id'];record=record_map[fid]
        if record.get('spec_sha256')!=figure_spec_hash(plan,fid)or record.get('engine')!=figure['engine']:
            fail('图件规格过期或引擎不匹配')
        blob=data_map.get(record.get('png_sha256'))
        if blob is None:fail('实际图件SHA256与清单不符')
        validated=prepare_output(base64.b64encode(blob).decode(),'png')
        total_pixels+=validated.width*validated.height
        if total_pixels>80_000_000:fail('图组解码像素总量超过8000万')
        filename='figures/'+fid+'.png';payloads[filename]=blob
        if figure['engine']=='vector':
            drawing=render_diagram(plan,figure)
            if drawing['png']!=blob:fail('确定性图件与当前规格不符')
            payloads['figures/'+fid+'.svg']=drawing['svg']
        evidence={}
        operation=record.get('operation')
        if operation is not None and operation not in {'generate','edit'}:fail('执行操作证据无效')
        evidence['operation']=operation
        refid=record.get('reference_figure_id')or None
        if refid is not None and refid not in record_map:fail('参考图号证据无效')
        evidence['reference_figure_id']=refid
        for key in ['reference_sha256','normalized_reference_sha256']:
            value=record.get(key)
            if value is not None and (not isinstance(value,str)or not re.fullmatch('[0-9a-f]{64}',value)):fail('参考SHA证据无效')
            evidence[key]=value
        if operation=='edit'and (refid!=fid or not evidence['reference_sha256']):fail('关联修改证据不完整')
        model=record.get('model')
        if model is not None and model!='gpt-image-2.6-flare':fail('模型证据无效')
        evidence['model']=model
        checks=record.get('checks')or{}
        if not isinstance(checks,dict):fail('检查证据无效')
        evidence['checks']={'visual_review_status':'not_reviewed'}
        for key in ['file_validation_passed','vector_spec_checked']:
            if key in checks:
                if type(checks[key])is not bool:fail('检查证据类型无效')
                evidence['checks'][key]=checks[key]
        for key in ['label_anchor_count','edge_count']:
            if key in checks:
                if type(checks[key])is not int or not 0<=checks[key]<=600:fail('检查计数证据无效')
                evidence['checks'][key]=checks[key]
        if figure['engine']=='vector':evidence.update(svg_file_name='figures/'+fid+'.svg',svg_sha256=hashlib.sha256(drawing['svg']).hexdigest())
        exported.append({**evidence,'figure_id':fid,'title':figure['title'],'engine':figure['engine'],
            'spec_sha256':record['spec_sha256'],'png_sha256':record['png_sha256'],
            'file_name':filename,'width':validated.width,'height':validated.height,
            'request_id':safe_request_id(record.get('request_id')),'usage':safe_usage(record.get('usage')),
            'professional_review_status':'not_reviewed','requires_human_review':True})
        pages.append((blob,f'图{index}  '+figure['title']))
    normalized={'schema_version':'drawing-set-result-1','set_id':plan['set_id'],
        'revision':plan['revision'],'plan_sha256':plan_hash(plan),'figures':exported,
        'file_validation_passed':True,'professional_review_status':'not_reviewed',
        'requires_human_review':True,'history':manifest.get('history',[]),
        'pdf_representation':'raster PNG layout; vector originals delivered as SVG where available'}
    descriptions=[plan['title'],'']+[f'图{i}：{f["title"]}。{f["purpose"]}'for i,f in enumerate(plan['figures'],1)]
    descriptions+=['','标号表：']+[p['label']+'：'+p['name']for p in plan['parts']]
    review=['文件校验通过，技术图示及跨图关系仍需人工核查。','声明的关系覆盖不等于图像内容已经验证。',
            'PDF为栅格图排版；可用矢量原件另附SVG，SVG文字显示依赖查看端中文字体。',
            '待确认：']+plan['unresolved']+['约束：']+plan['constraints']
    payloads.update({'drawings.pdf':_pdf(pages),'plan.json':(canonical(plan)+'\n').encode(),
        'manifest.json':(canonical(normalized)+'\n').encode(),
        'figure-descriptions.txt':('\n'.join(descriptions)+'\n').encode(),
        'review.txt':('\n'.join(review)+'\n').encode()})
    payloads['SHA256SUMS.json']=(canonical({name:hashlib.sha256(blob).hexdigest()for name,blob in payloads.items()})+'\n').encode()
    out=BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED)as archive:
        for name,blob in payloads.items():archive.writestr(name,blob)
    return {'pdf':payloads['drawings.pdf'],'zip':out.getvalue(),'manifest':normalized,
            'member_hashes':{n:hashlib.sha256(b).hexdigest()for n,b in payloads.items()}}
