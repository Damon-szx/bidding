"""Count new observations, not plan edits or cosmetic changes, as execution progress."""
from bidding.model import encoded


class ProgressWatch:
    def __init__(self):
        self.observations = set()
        self.previous = 0
        self.stalled_rounds = 0
        self.recoveries = 0
        self.business_observations = set()
        self.business_previous = 0
        self.stage_stalled_rounds = 0

    def observe(self, name, arguments, output):
        if output.get('error'):
            return
        value = None
        if name in ('render_tender_page', 'navigate_tender_entry', 'fetch_tender_detail'):
            value = output.get('source_url')
        elif name == 'read_tool_result':
            if output.get('value') not in (None, '', [], {}):
                value = [output.get('original_result_id'), output.get('field'), output.get('offset'),
                    arguments.get('contains'), output.get('value')]
        elif name == 'record_project_assessment':
            if output.get('status') == 'needs_navigation':
                return
            value = [arguments.get('announcement_id'),
                sorted((item.get('criterion_id'), item.get('verdict')) for item in arguments.get('judgments', [])),
                sorted((item.get('url'), item.get('document_type')) for item in arguments.get('files') or []),
                arguments.get('valid_until')]
        elif name == 'discover_tender_files':
            value = [arguments.get('announcement_id'), output.get('status'), output.get('files', output.get('attachments'))]
        elif name == 'scan_tender_listing':
            value = [item.get('fingerprint') for item in output.get('pages', [])] or None
        elif name == 'inspect_document_access':
            value = [arguments.get('announcement_id'), arguments.get('entry_id'), output.get('document_access_status'), output.get('access_observation')]
        elif name == 'search_tenders':
            value = sorted(item.get('source_url') or item.get('title', '') for item in output.get('candidates', [])) or None
        elif name == 'plan_acquisition':
            value = output.get('batch_id')
        elif name == 'download_tender_file':
            value = output.get('file_id')
        if value:
            self.observations.add(encoded([name, value]))
            if name in ('record_project_assessment', 'inspect_document_access', 'plan_acquisition', 'download_tender_file'):
                self.business_observations.add(encoded([name, value]))

    def checkpoint(self):
        current = len(self.observations)
        delta = current - self.previous
        self.stalled_rounds = 0 if delta else self.stalled_rounds + 1
        self.previous = current
        business = len(self.business_observations)
        business_delta = business - self.business_previous
        self.stage_stalled_rounds = 0 if business_delta else self.stage_stalled_rounds + 1
        self.business_previous = business
        return {'new_observations': delta, 'total_observations': current,
            'new_business_observations': business_delta, 'stage_stalled_rounds': self.stage_stalled_rounds,
            'stalled_rounds': self.stalled_rounds, 'recovery_rounds': self.recoveries}
