"""Explicit planner replies for transport fixtures; production always uses the model."""
import json

import httpx


def planning_response(request, action='research', file_goal='none', expected_count=None, criteria=None, questions=None, control_action=None, steps=None, files_per_project=None, file_formats=None):
    body = json.loads(request.content)
    if not body['messages'][0]['content'].startswith('TASK_PLANNING'):
        return None
    current = json.loads(body['messages'][-1]['content'])['current_request']
    spec = {'goal': current, 'action': action, 'criteria': criteria or [], 'expected_count': expected_count,
        'result_kind': 'projects' if criteria or expected_count else 'answer',
        'file_goal': file_goal, 'files_per_project': files_per_project,
        'authorization_quote': current if action in ('download', 'control', 'resume') else '',
        'control_action': control_action, 'questions': questions or [], 'assumptions': [],
        'complete_when': ['Produce the requested verified outcome'], 'steps': steps or []}
    if file_formats is not None:
        spec['file_formats'] = file_formats
    return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': json.dumps(spec)}}],
        'usage': {'prompt_tokens': 100, 'completion_tokens': 20}})
