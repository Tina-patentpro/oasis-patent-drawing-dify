"""Bounded Responses transport; one POST, shared approved provider key."""
import json
import base64
import httpx
from .client import OpenAIImagesClient, _status_error, safe_request_id, safe_usage, project_usage
from .errors import DrawingError
from .file_input import bounded_body, close_http_resources, run_with_deadline

TEXT_MODEL='gpt-6.1-sol'
DEADLINE=300


class OpenAITextClient(OpenAIImagesClient):
    @staticmethod
    def prepare(system,user,images=None):
        if (not isinstance(system,str)or not system.strip()or len(system)>16000
            or not isinstance(user,str)or not user.strip()or len(user)>240000
            or len(system)+len(user)>260000):
            raise DrawingError('parameter_error','规划提示为空或超限')
        payload={'model':TEXT_MODEL,'input':[{'role':'system','content':system},{'role':'user','content':user}],
                 'text':{'format':{'type':'json_object'}},'max_output_tokens':24000,'store':False}
        if images:
            from .images import PreparedImage
            if not isinstance(images,list) or len(images)>12 or any(not isinstance(i,PreparedImage)for i in images):
                raise DrawingError('parameter_error','规划页图必须为已核原图，最多12页')
            payload['input'][1]['content']=[{'type':'input_text','text':user}]+[{'type':'input_image','image_url':i.data_url}for i in images]
        return payload

    def plan(self,system,user,images=None,evidence_sink=None):
        payload=self.prepare(system,user,images)
        if evidence_sink:evidence_sink('request',json.dumps(payload,ensure_ascii=False).encode())
        request_id=None
        def fetch(deadline):
            nonlocal request_id
            client=httpx.Client(transport=self._transport,timeout=httpx.Timeout(DEADLINE,connect=20),follow_redirects=False)
            response=None
            try:
                outgoing=client.build_request('POST','https://api.openai.com/v1/responses',json=payload,
                    headers={'Authorization':'Bearer '+self._api_key,'Accept':'application/json','Accept-Encoding':'identity'})
                response=client.send(outgoing,stream=True);request_id=safe_request_id(response.headers.get('x-request-id'))
                body=bounded_body(response,2*1024*1024,max_seconds=DEADLINE,deadline=deadline)
                if evidence_sink:evidence_sink('response',body)
                if response.status_code!=200:raise _status_error(response.status_code,body,request_id)
                return body
            finally:close_http_resources(response,client)
        try:body=run_with_deadline(fetch,DEADLINE)
        except DrawingError as error:
            if error.category in {'response_timeout','response_too_large'}:raise DrawingError('result_unknown','规划未取得最终结果；费用状态未知，未自动重试',request_id)from None
            raise
        except httpx.HTTPError:raise DrawingError('result_unknown','规划连接中断；费用状态未知，未自动重试',request_id)from None
        try:
            result=json.loads(body)
            if result.get('status')!='completed':raise DrawingError('planner_incomplete','规划响应未完成；未执行绘图，未自动重试',request_id)
            content=[c for o in result.get('output',[])if o.get('type')=='message'for c in o.get('content',[])]
            if any(c.get('type')=='refusal'for c in content):raise DrawingError('content_refused','模型拒绝规划此材料；未执行绘图',request_id)
            text=''.join(c.get('text','')for c in content if c.get('type')=='output_text')
            if not text or len(text.encode())>240000:raise DrawingError('planner_invalid_output','规划未返回有效长度的JSON文本',request_id)
            return {'success':True,'text':text,'model':TEXT_MODEL,'request_id':request_id,'usage':safe_usage(result.get('usage')),'usage_projection':project_usage(result.get('usage')),'raw_response':result,'raw_response_base64':base64.b64encode(body).decode(),'professional_review_status':'not_reviewed'}
        except DrawingError as error:
            error.http_status=200;error._response_body=body
            raise
        except (ValueError,TypeError,AttributeError):raise DrawingError('planner_invalid_output','规划响应无法解析；未执行绘图',request_id,http_status=200,response_body=body)from None

# Pure preflight alias remains available when tests inject a client factory.
prepare_plan_payload = OpenAITextClient.prepare
