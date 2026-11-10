"""One bounded PDFium process per ingestion; no PDFium threading or web fetch."""
import base64
import ctypes
import io
import json
import math
import re
import sys


def extract(data):
    import pypdfium2 as pdfium
    document = pdfium.PdfDocument(data)
    pages = []
    pixels = 0
    try:
        if not 1 <= len(document) <= 12:
            raise ValueError('PDF page capacity exceeded (12)')
        for index in range(len(document)):
            page = document[index]
            bitmap = textpage = None
            try:
                w, h = page.get_size()
                if not all(math.isfinite(x) and 0 < x < 20000 for x in [w, h]):
                    raise ValueError('invalid PDF page geometry')
                scale = min(1.8, 1800 / max(w, h))
                pixels += math.ceil(w * scale) * math.ceil(h * scale)
                if pixels > 20_000_000:
                    raise ValueError('PDF rendered pixel capacity exceeded')
                textpage = page.get_textpage()
                text = textpage.get_text_range()
                if len(text) > 32000:
                    raise ValueError('PDF page text capacity exceeded')
                graphics = []; vectors = []; nontext_seen = False; furniture = []
                for count, obj in enumerate(page.get_objects(), 1):
                    if count > 10000: raise ValueError('PDF object capacity exceeded')
                    if obj.type not in (2, 3): continue
                    left, bottom, right, top = obj.get_bounds()
                    if not all(math.isfinite(v) for v in [left,bottom,right,top]): raise ValueError('invalid PDF graphic bounds')
                    left, bottom, right, top = max(0,left), max(0,bottom), min(w,right), min(h,top)
                    if right < left or top < bottom: continue
                    if obj.type == 2:
                        fill=ctypes.c_int();stroke=ctypes.c_int()
                        pdfium.raw.FPDFPath_GetDrawMode(obj.raw,ctypes.byref(fill),ctypes.byref(stroke))
                        width,height=right-left,top-bottom
                        margin_rule=width>w*.7 and height<=3 and (top<h*.06 or bottom>h*.94)
                        text_background=fill.value and not stroke.value and width>w*.7 and height>0 and width/height>8 and bool(textpage.get_text_bounded(left,bottom,right,top).strip())
                        if margin_rule or text_background:
                            furniture.append({'bbox_pdf':[left,bottom,right,top],'reason':'margin_rule'if margin_rule else'text_background','retained_in_full_page':True});continue
                        nontext_seen = True
                        # Orthogonal line drawings are often many one-pixel paths.
                        # Preserve their joint extent rather than dropping every line.
                        vectors.append([left,bottom,right,top]);continue
                    nontext_seen = True
                    if len(graphics) >= 256: raise ValueError('PDF graphic capacity exceeded')
                    graphics.append({'kind':'image','bbox_pdf':[left,bottom,right,top],
                        'bbox':[max(0,int(left*scale)),max(0,int((h-top)*scale)),min(math.ceil(w*scale),math.ceil(right*scale)),min(math.ceil(h*scale),math.ceil((h-bottom)*scale))]})
                if vectors:
                    left=min(v[0]for v in vectors);bottom=min(v[1]for v in vectors);right=max(v[2]for v in vectors);top=max(v[3]for v in vectors)
                    graphics.append({'kind':'vector_paths','path_count':len(vectors),'bbox_pdf':[left,bottom,right,top],
                        'bbox':[max(0,int(left*scale)),max(0,int((h-top)*scale)),min(math.ceil(w*scale),math.ceil(right*scale)),min(math.ceil(h*scale),math.ceil((h-bottom)*scale))],
                        'region_note':'Union of page vector paths; source review required before treating this as a single figure.'})
                bitmap = page.render(scale=scale)
                image = bitmap.to_pil().convert('RGB')
                output = io.BytesIO(); image.save(output, format='PNG'); image.close()
                # Captions + nontext graphic prove a candidate, not its semantic correctness.
                caption = bool(re.search(r'图\s*[0-9一二三四五六七八九十]', text))
                presence = 'present' if graphics and caption and len(text) > 80 else 'unknown' if graphics or nontext_seen else 'absent'
                pages.append({'page': index + 1, 'text': text, 'width': bitmap.width, 'height': bitmap.height,
                    'pdf_size': [w, h], 'scale': scale, 'graphics': graphics, 'page_furniture': furniture, 'figure_presence': presence,
                    'png_base64': base64.b64encode(output.getvalue()).decode()})
            finally:
                if bitmap is not None: bitmap.close()
                if textpage is not None: textpage.close()
                page.close()
    finally:
        document.close()
    return pages


if __name__ == '__main__':
    try:
        if sys.platform.startswith('linux'):
            import resource
            resource.setrlimit(resource.RLIMIT_CPU, (18, 18))
            resource.setrlimit(resource.RLIMIT_AS, (500 * 1024 * 1024,) * 2)
        raw = sys.stdin.buffer.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024: raise ValueError('PDF input capacity exceeded')
        print(json.dumps({'pages': extract(raw)}, ensure_ascii=False, allow_nan=False))
    except Exception:
        print(json.dumps({'error': 'PDF解析失败、加密或资源超限；原件未截断，未调用模型'}))
        sys.exit(1)
