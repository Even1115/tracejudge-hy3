"""Audit the existing LCB v2 run and export public metadata only; no network."""
import csv
import hashlib
import json
import logging
import socket
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path('/home/even/projects/tracejudge-hy3-lcb60-windows-20260906')
RUNTIME = ROOT / 'artifacts/benchmark-runtime/f4249a0448427578'
RUN_ID = 'lcb60-windows-20260906-v2'
RUN = RUNTIME / 'artifacts/experiments/livecodebench' / RUN_ID
OUT = Path(__file__).resolve().parent
READINESS = OUT.parents[1] / 'benchmark-readiness'
sys.dont_write_bytecode = True
sys.path.insert(0, str(RUNTIME / 'src'))
logging.disable(logging.CRITICAL)

def deny(*args, **kwargs):
    raise RuntimeError('Network is disabled for report generation')

socket.socket.connect = deny
socket.socket.connect_ex = deny
from tracejudge_hy3.benchmark.contracts import BenchmarkTask, canonical_sha256
from tracejudge_hy3.lcb.experiment import load_events, summarize, source_identity, execution_identity
from tracejudge_hy3.lcb.generation import prompt_bundle_hash, STDIO_PROMPT_VERSION

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def inventory():
    return {str(p.relative_to(RUN)): digest(p) for p in sorted(RUN.rglob('*')) if p.is_file()}

def read(path):
    return json.loads(path.read_text())

before = inventory()
manifest = read(RUN / 'manifest.json')
tasks = [BenchmarkTask.model_validate(t) for t in read(ROOT / 'artifacts/datasets/livecodebench/tasks60.json')]
events, generations, executions = load_events(RUN, tasks, RUN_ID)
report = read(RUN / 'report.json')
assert report == summarize(tasks, generations, executions, events)
receipt = read(RUN / 'completion_receipt.json')
assert receipt['event_count'] == len(events)
assert receipt['manifest_sha256'] == digest(RUN / 'manifest.json')
assert receipt['report_sha256'] == digest(RUN / 'report.json')
assert receipt['last_event_sha256'] == events[-1]['sha256']
assert receipt['complete'] == report['complete']
identity = manifest['identity']
assert identity['source'] == source_identity(RUNTIME)
assert identity['tasks_sha256'] == canonical_sha256([t.model_dump(mode='json') for t in tasks])
assert identity['selection'] == read(ROOT / 'artifacts/datasets/livecodebench/selection60.json')
assert identity['prompt_sha256'] == prompt_bundle_hash()
smoke = read(ROOT / 'artifacts/benchmark-readiness/lcb.json')
assert smoke['ready'] and smoke['image'] == identity['image']
assert smoke['execution_source_sha256'] == execution_identity(RUNTIME, 'lcb')
assert subprocess.check_output(['git', 'status', '--porcelain'], cwd=RUNTIME).strip() == b''
commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=RUNTIME, text=True).strip()
assert commit == '0096a408f7703aabf1fadd7dd38400290d74438d'

rows = []
for index, task in enumerate(tasks, 1):
    tid = task.identity.task_id
    generation = generations.get(tid, {})
    execution = executions.get(tid)
    history = [e['event'] for e in events if e['event']['kind'] == 'generation' and e['event']['task_id'] == tid]
    row = {
        'index': index, 'task_id': tid, 'difficulty': task.difficulty.value,
        'generation_status': generation.get('status', 'pending'),
        'generation_event_count': len(history),
        'provider_attempt_count_total': sum(e.get('attempt_count', 0) for e in history),
        'execution_status': execution.status.value if execution else 'not_run',
        'failure_kind': execution.failure_kind if execution else None,
        'execution_duration_seconds': execution.duration_seconds if execution else None,
    }
    rows.append(row)
assert sum(r['provider_attempt_count_total'] for r in rows) == report['provider_attempts']
first = read(READINESS / 'windows-lcb-v2-results-20260906T1006Z/result_audit.json')
previous = read(READINESS / 'windows-lcb-v2-repeat-20260906T1048Z/result_files_sha256.json')
repair = read(READINESS / 'windows-lcb-codefix-20260906T0621Z/final_evidence.json')
outcomes = Counter(o for e in events for o in e['event'].get('attempt_outcomes', []))
first_count = first['report']['generation_events'] + first['report']['execution_events']
added = events[first_count:]
phase_start = min(e['event']['started_at'] for e in added) if added else None
phase_end = max(e['event']['completed_at'] for e in added) if added else None
source = identity['source']
selection = identity['selection']
provider_keys = ['provider', 'model', 'reasoning_effort', 'reasoning_effort_enabled',
                 'timeout_seconds', 'max_retries', 'max_parse_repairs', 'stdio_prompt_sha256']
assert before == inventory(), 'Run changed during read-only audit'

result = {
    'audit_time_utc': datetime.now(timezone.utc).isoformat(),
    'run_id': RUN_ID, 'run_directory': str(RUN), 'runtime': str(RUNTIME), 'runtime_commit': commit,
    'report': report,
    'identity': {
        'source_sha256': canonical_sha256(source), 'python': source['python'],
        'dependencies': source['dependencies'],
        'provider': {k: identity['provider'].get(k) for k in provider_keys},
        'limits': identity['limits'], 'per_test_timeout_seconds': identity['per_test_timeout_seconds'],
        'prompt_version': STDIO_PROMPT_VERSION, 'prompt_sha256': identity['prompt_sha256'],
        'tasks_sha256': identity['tasks_sha256'], 'image': identity['image'],
        'selection_protocol': selection.get('protocol'),
        'ordered_ids_sha256': selection['selected_task_ids_sha256'],
        'selection_file_sha256': digest(ROOT / 'artifacts/datasets/livecodebench/selection60.json'),
        'uv_lock_sha256': digest(RUNTIME / 'uv.lock'),
        'smoke_receipt_sha256': digest(ROOT / 'artifacts/benchmark-readiness/lcb.json'),
        'execution_source_sha256': execution_identity(RUNTIME, 'lcb'),
        'provider_source_sha256': digest(RUNTIME / 'src/tracejudge_hy3/providers/hy3_openai.py'),
        'freeze_manifest_sha256': digest(RUNTIME / 'FREEZE.json'),
    },
    'timeline': {
        'initial_started_at': manifest['started_at'], 'initial_finished_at': first['completed_at'],
        'initial_elapsed_seconds': first['elapsed_seconds'],
        'resume_started_at': phase_start, 'resume_last_event_completed_at': phase_end,
        'resume_elapsed_seconds': (datetime.fromisoformat(phase_end)-datetime.fromisoformat(phase_start)).total_seconds() if added else 0,
        'latest_receipt_updated_at': receipt['updated_at'],
        'initial_provider_attempts': first['report']['provider_attempts'],
        'resume_provider_attempts': sum(e['event'].get('attempt_count', 0) for e in added),
    },
    'provider_attempt_outcomes': dict(outcomes),
    'non_pass_tasks': [r for r in rows if r['execution_status'] != 'passed'],
    'task_statuses': rows,
    'checks': {
        'event_chain_valid': True, 'recomputed_report_matches': True,
        'receipt_matches': True, 'source_selection_tasks_prompt_and_smoke_match': True,
        'runtime_git_clean': True, 'result_file_count': len(before),
        'results_unchanged_during_audit': True, 'unchanged_since_previous_audit': before == previous,
        'provider_and_execution_source_match_pre_run_validation':
            digest(RUNTIME / 'src/tracejudge_hy3/providers/hy3_openai.py') == repair['provider_source_sha256']
            and execution_identity(RUNTIME, 'lcb') == repair['execution_source_sha256'],
    },
    'boundaries': {'real_model_api_calls': 0, 'env_file_read': False,
                   'private_tests_decoded': False, 'candidate_executed': False,
                   'formal_code_config_or_results_modified': False},
}
with (OUT / 'audit.json').open('x', encoding='utf-8') as handle:
    json.dump(result, handle, ensure_ascii=False, indent=2)
with (OUT / 'result_files_sha256.json').open('x', encoding='utf-8') as handle:
    json.dump(before, handle, indent=2)
with (OUT / 'task_statuses.csv').open('x', encoding='utf-8-sig', newline='') as handle:
    writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
print(json.dumps({k: v for k, v in result.items() if k not in ['task_statuses']}, ensure_ascii=False, indent=2))
