"""Small Dify adapter for plugin-owned intake references and field values."""


def intake_result(items):
    if not isinstance(items, list):
        raise ValueError('采集工具结果应为数组')
    rows = [row for row in items if isinstance(row, dict) and 'intake_revision' in row]
    if len(rows) != 1:
        raise ValueError('缺少唯一采集回执，请核对原运行')
    row = rows[0]
    fields = ['workspace_id', 'intake_revision', 'message', 'route',
              'patent_type', 'technical_type', 'application_stage']
    if any(not isinstance(row.get(k), str) for k in fields):
        raise ValueError('采集回执字段无效')
    if row['route'] not in {'reply', 'forward'}:
        raise ValueError('采集路由无效')
    if row['route'] == 'forward' and not row['workspace_id']:
        raise ValueError('缺少已确认材料工作区')
    return {k: row[k] for k in fields}
