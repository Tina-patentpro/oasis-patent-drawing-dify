import json
import re

from dify_plugin import AgentStrategy
from dify_plugin.entities.tool import ToolProviderType


class ComplexDrawingAgent(AgentStrategy):
    def _invoke(self, parameters):
        bound = {key: parameters[key] for key in ('context_id', 'workspace_id', 'scope')}
        if parameters.get('durable_work') == 'yes':
            revision = parameters.get('expected_revision')
            if (getattr(self.session, 'conversation_id', None) not in (None, '', bound['context_id'])
                    or not isinstance(revision, str) or not re.fullmatch(r'[1-9][0-9]{0,9}', revision)
                    or not re.fullmatch(r'[a-f0-9]{64}', bound['scope'])):
                raise ValueError('复杂图准备交接范围或修订无效。')
            # Submit via the workflow's next Tool node, which has the verified caller and session.
            yield self.create_text_message(json.dumps({**bound, 'expected_revision': revision}, ensure_ascii=False))
            return

        bound['action'] = 'prepare_complex_images'
        messages = self.session.tool.invoke(
            ToolProviderType.BUILT_IN,
            'tina-patentpro/oasisintwrite_patent_drawing/oasisintwrite_patent_drawing',
            'patent_drawing_workspace',
            bound,
        )
        rows = [row.message.json_object for row in messages if row.type == row.MessageType.JSON]
        if len(rows) != 1 or rows[0].get('scope') != bound['scope']:
            raise ValueError('未收到当前图单的有效准备回执。')
        keys = ('scope', 'views')
        proposal = {key: rows[0][key] for key in keys}
        yield self.create_text_message(json.dumps(proposal, ensure_ascii=False))
