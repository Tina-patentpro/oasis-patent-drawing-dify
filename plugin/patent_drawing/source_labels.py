"""Operator-reviewed isolated number overlay; original raster base never rewritten."""
import base64,hashlib,io,re,copy
from PIL import Image,ImageChops,ImageDraw
from .annotations import outside_regions_unchanged,png,regular_font

def apply(base,receipt):
 if hashlib.sha256(base).hexdigest()!=receipt.get('base_sha256'):raise ValueError('migration source SHA mismatch')
 if not receipt.get('reviewer')or not re.fullmatch(r'[0-9]{1,12}',receipt.get('old_label',''))or not re.fullmatch(r'[0-9]{1,12}',receipt.get('new_label','')):raise ValueError('reviewed numeric migration required')
 with Image.open(io.BytesIO(base))as opened:im=opened.convert('RGB')
 roi=receipt.get('roi');at=receipt.get('at');size=receipt.get('font_size')
 if not isinstance(roi,list)or len(roi)!=4 or any(type(x)is not int for x in roi)or not(0<=roi[0]<roi[2]<=im.width and 0<=roi[1]<roi[3]<=im.height):raise ValueError('invalid migration ROI')
 if type(size)is not int or not 14<=size<=40 or not isinstance(at,list)or len(at)!=2 or any(type(x)is not int for x in at):raise ValueError('invalid migration text anchor')
 l,t,r,b=roi;gray=im.convert('L')
 # A line crossing a boundary or a nonwhite background is unsafe; only a
 # reviewer-confirmed isolated number in an otherwise white patch is allowed.
 if any(min(im.getpixel((x,y)))<250 for x,y in [(x,t)for x in range(l,r)]+[(x,b-1)for x in range(l,r)]+[(l,y)for y in range(t,b)]+[(r-1,y)for y in range(t,b)]):raise ValueError('migration ROI boundary touches ink/geometry')
 ink={(x,y)for x in range(l,r)for y in range(t,b)if gray.getpixel((x,y))<245};seen=set();components=0
 for point in sorted(ink):
  if point in seen:continue
  components+=1;stack=[point];seen.add(point)
  while stack:
   x,y=stack.pop()
   for v in [(x-1,y),(x+1,y),(x,y-1),(x,y+1)]:
    if v in ink and v not in seen:seen.add(v);stack.append(v)
 if components!=len(receipt['old_label']):raise ValueError('isolated number component count differs; manual split not supported')
 # Near-white antialiasing is accepted; no colored/photo texture in the patch.
 if any(max(im.getpixel((x,y)))-min(im.getpixel((x,y)))>3 for x in range(l,r)for y in range(t,b)):raise ValueError('non-monochrome migration background')
 face=regular_font(size);layer=Image.new('RGBA',im.size,(0,0,0,0));draw=ImageDraw.Draw(layer);box=draw.textbbox(at,receipt['new_label'],font=face,anchor='lt')
 if not(l<box[0]<box[2]<r and t<box[1]<box[3]<b):raise ValueError('new number does not fit reviewed white patch')
 draw.rectangle([l,t,r-1,b-1],fill='white');draw.text(at,receipt['new_label'],font=face,fill='black',anchor='lt');im.paste(layer,mask=layer.getchannel('A'))
 mask=Image.new('L',im.size,0);ImageDraw.Draw(mask).rectangle([l,t,r-1,b-1],fill=255)
 return {'png':png(im),'regions':[{'text_box':list(box),'mask':base64.b64encode(png(mask)).decode()}],'receipt':copy.deepcopy(receipt)}


def validate_guide_label_regions(before, after, regions):
    """Check the exact reviewed guide replacement against its display regions."""
    if not isinstance(after, bytes) or len(after) > 16 * 1024 * 1024:
        raise ValueError('engineering label PNG capacity exceeded')
    if not isinstance(regions, list) or not 1 <= len(regions) <= 16:
        raise ValueError('reviewed guide label regions required')
    with Image.open(io.BytesIO(before), formats=['PNG']) as original, Image.open(
        io.BytesIO(after), formats=['PNG']
    ) as candidate:
        width, height = original.size
        if candidate.size != original.size or width * height > 12_000_000:
            raise ValueError('engineering label PNG geometry changed')
        for box in regions:
            if (not isinstance(box, list) or len(box) != 4 or any(type(x) is not int for x in box)
                    or not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height)):
                raise ValueError('invalid engineering label region')
        area = sum((r-l)*(b-t) for l,t,r,b in regions)
        if area > width * height // 10:
            raise ValueError('engineering label regions exceed the display area limit')
        diff = ImageChops.difference(original.convert('RGB'), candidate.convert('RGB'))
        if any(diff.crop(box).getbbox() is None for box in regions):
            raise ValueError('a reviewed engineering label region did not change')
    inclusive = [[l, t, r-1, b-1] for l, t, r, b in regions]
    if not outside_regions_unchanged(before, after, inclusive):
        raise ValueError('engineering label edit changed pixels outside reviewed regions')
