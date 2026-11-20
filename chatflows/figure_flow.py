"""Code-node adapters for a multi-select Decisions result."""

import json


def figure_selection(items):
    rows = [row for row in items if isinstance(row, dict) and 'workspace_id' in row]
    if len(rows) != 1:
        raise ValueError('缺少唯一图型判断回执。')
    result = rows[0]
    if result.get('state') == 'skipped' or result.get('blocking_state') == 'intake_revision_changed':
        return {'operation_id': '', 'selected': []}
    operation = result.get('operation_id')
    selected = result.get('selected', [])
    if not isinstance(operation, str) or not operation:
        raise ValueError('缺少图型判断操作标识。')
    if not isinstance(selected, list) or any(not isinstance(name, str) for name in selected):
        raise ValueError('图型判断必须返回图型列表。')
    return {'operation_id': operation, 'selected': selected}


def figure_instruction(selected, family, instruction):
    return {'instruction': instruction if family in selected else ''}


def merge_figure_instructions(operation_id, **instructions):
    if not operation_id:
        return {'routing_json': ''}
    return {'routing_json': json.dumps({
        'operation_id': operation_id,
        'instructions': {family: text for family, text in instructions.items() if text},
    }, ensure_ascii=False)}


def durable_status(items):
    fields = ('route', 'message', 'scope', 'task_id', 'job_status', 'blocking_state', 'resume_action')
    rows = [row for row in items if isinstance(row, dict) and 'workspace_id' in row] if isinstance(items, list) else []
    if len(rows) != 1:
        return {'route': 'reply', 'message': '任务回执待核，请在原会话发送“查看进度”。',
                'scope': '', 'task_id': '', 'job_status': '', 'prepared': 'no',
                'blocking_state': 'missing_tool_receipt', 'resume_action': 'inspect_work'}
    row = rows[0]
    status = {name: str(row.get(name) or '') for name in fields}
    status['message'] = coverage_message(row, status['message'])
    status['prepared'] = 'yes' if row.get('preparation_status') == 'prepared' and not row.get('file_hashes') else 'no'
    return status
