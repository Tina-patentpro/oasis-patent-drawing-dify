"""Ordered, source-bound raster references; no user-authored approval flags."""
import copy,hashlib,re
from .images import prepare_reference,prepare_markup,prepare_mask
from .operation_store import digest


def prepare_reference_bundle(items:list[dict],markup:bytes|None,mask:bytes|None)->dict:
 if not isinstance(items,list)or not 1<=len(items)<=16 or(markup is not None and mask is not None):raise ValueError('reference capacity or mutually exclusive overlays')
 refs=[];rows=[];source=None
 for i,row in enumerate(items):
  if i==0 and(row.get('role')!='primary_guide'or row.get('origin')!='derived_guide'):raise ValueError('derived guide must be explicitly first')
  if i>0 and(row.get('role')not in {'source_overview','source_component','section_reference'}or row.get('origin')!='source'or not row.get('source_region_id')):raise ValueError('reference role/source invalid')
  raw=row.get('blob');sha=hashlib.sha256(raw).hexdigest()if isinstance(raw,bytes)else None
  if not sha or row.get('raw_sha256')!=sha or not re.fullmatch('[a-f0-9]{64}',row.get('source_sha256','')):raise ValueError('reference raw/source identity invalid')
  source=source or row['source_sha256']
  if row['source_sha256']!=source:raise ValueError('reference belongs to another source')
  ref=prepare_reference(raw);refs.append(ref)
  rows.append({k:row[k]for k in ['role','origin','raw_sha256','source_sha256','source_region_id','crop_bbox']if k in row}|{'normalized_sha256':ref.sha256,'size':[ref.width,ref.height]})
 overlay=prepare_markup(markup,refs[0])if markup is not None else None;m=prepare_mask(mask,refs[0])if mask is not None else None
 receipt={'contract':'reference-bundle-036-r1','items':rows,'markup_sha256':overlay.sha256 if overlay else None,'mask_sha256':m.sha256 if m else None}
 return {'receipt':receipt,'bundle_sha256':digest(receipt),'references':tuple(refs),'markup':overlay,'mask':m}


def compose_parent_guide_bundle(guide_bundle:dict,parent_reference,binding:dict)->dict:
 """Compose only after Workspace privately loads/rechecks the actual parent.

 The approved guide receipt stays immutable. This separate request receipt binds
 the actual parent, normalized bytes, and scene derivation used at dispatch.
 """
 refs=guide_bundle['references'];outer=guide_bundle['receipt'];receipt=outer['receipt']
 if guide_bundle.get('mask')is not None or guide_bundle.get('markup')is not None:
  raise ValueError('parent plus guide cannot silently reuse mask or markup coordinates')
 if not refs or len(refs)+1>16:raise ValueError('combined reference capacity exceeded')
 required={'source_sha256','parent_view_id','parent_spec_sha256','parent_base_sha256',
           'parent_base_spec_sha256','parent_reference_sha256','parent_review_sha256','scene_derivation'}
 if not isinstance(binding,dict)or set(binding)!=required:raise ValueError('actual parent binding required')
 hashes=required-{'parent_view_id','scene_derivation'}
 if any(not isinstance(binding[k],str)or not re.fullmatch('[a-f0-9]{64}',binding[k])for k in hashes):
  raise ValueError('actual parent hash binding invalid')
 if not binding['parent_view_id']or binding['parent_reference_sha256']!=parent_reference.sha256:
  raise ValueError('actual parent normalized bytes differ')
 derivation=binding['scene_derivation']
 if not isinstance(derivation,dict)or derivation.get('status')!='PASS':raise ValueError('validated scene derivation required')
 rows=receipt['items']
 if len(refs)!=len(rows)or any(row.get('source_sha256')!=binding['source_sha256']
                               or row.get('normalized_sha256')!=ref.sha256 for row,ref in zip(rows,refs)):
  raise ValueError('guide references belong to another source or normalized identity')
 ordered=(refs[0],parent_reference,*refs[1:])
 if len({r.sha256 for r in ordered})!=len(ordered):raise ValueError('duplicate combined reference')
 parent_row={'role':'actual_parent','origin':'actual_generated_parent',
             'source_sha256':binding['source_sha256'],'raw_sha256':binding['parent_base_sha256'],
             'normalized_sha256':parent_reference.sha256,'size':[parent_reference.width,parent_reference.height]}
 result=copy.deepcopy(outer)
 result['receipt']={**copy.deepcopy(receipt),'contract':'reference-bundle-parent-guide-039-r1',
                    'items':[copy.deepcopy(rows[0]),parent_row,*copy.deepcopy(rows[1:])],
                    'parent_binding':copy.deepcopy(binding)}
 return {'references':ordered,'mask':None,'markup':None,'receipt':result,
         'bundle_sha256':digest(result['receipt'])}
