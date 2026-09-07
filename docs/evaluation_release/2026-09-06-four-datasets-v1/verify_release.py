"""Verify this public four-dataset report. Standard library, no model or private data."""
import argparse
import csv
import hashlib
import json
import math
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

PACKAGE = Path(__file__).resolve().parent
ROOT = PACKAGE.parents[2]
REPORT = ROOT / 'docs/four_dataset_evaluation_report.md'
PROHIBITED = {'api_key', 'authorization', 'base_url', 'endpoint', 'endpoint_sha256',
              'raw_output', 'raw_response', 'candidate_code', 'solution', 'solution_trace',
              'private_test_cases', 'hidden_tests', 'messages', 'prompt', 'test_cases'}
PRIVATE = re.compile(r'/Users/|/home/|[A-Za-z]:[/\\]|\bsk-[A-Za-z0-9_-]{12,}|\bghp_[A-Za-z0-9]{12,}|Bearer\s+\S+')

def require(condition, message):
    if not condition:
        raise ValueError(message)

def read(name):
    return json.loads((PACKAGE / name).read_text(encoding='utf-8'))

def close(a, b):
    return math.isclose(a, b, abs_tol=1e-12)

def wilson(k, n):
    z = 1.959963984540054
    p = k / n
    denominator = 1 + z*z/n
    center = (p + z*z/(2*n)) / denominator
    half = z * math.sqrt(p*(1-p)/n + z*z/(4*n*n)) / denominator
    return [center-half, center+half]

def check_tree(obj):
    if isinstance(obj, dict):
        require(not PROHIBITED.intersection(obj), 'Prohibited field in public JSON')
        if 'numerator' in obj and 'denominator' in obj:
            k, n = obj['numerator'], obj['denominator']
            require(0 <= k <= n, 'Invalid numerator or denominator')
            estimate = obj.get('value', obj.get('estimate'))
            if n and estimate is not None:
                require(close(k/n, estimate), 'Fraction mismatch')
        for value in obj.values():
            check_tree(value)
    elif isinstance(obj, list):
        for value in obj:
            check_tree(value)
    elif isinstance(obj, str):
        require(not PRIVATE.search(obj), 'Private value in public JSON')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-index', action='store_true')
    args = parser.parse_args()
    entries = {}
    for line in (PACKAGE / 'SHA256SUMS').read_text().splitlines():
        digest, name = line.split('  ', 1)
        path = ROOT / name
        require(re.fullmatch('[a-f0-9]{64}', digest), 'Invalid digest')
        require(path.resolve().is_relative_to(ROOT) and path.is_file(), 'Invalid file path')
        require(name not in entries, 'Duplicate checksum path')
        require(hashlib.sha256(path.read_bytes()).hexdigest() == digest, f'Hash mismatch: {name}')
        entries[name] = digest
    expected = {str(p.relative_to(ROOT)).replace('\\', '/') for p in PACKAGE.iterdir()
                if p.is_file() and p.name != 'SHA256SUMS'}
    expected.update({'docs/four_dataset_evaluation_report.md', 'docs/.gitattributes'})
    require(set(entries) == expected, 'Incomplete or unexpected checksum coverage')
    links = 0
    for name in entries:
        path = ROOT / name
        raw = path.read_bytes()
        require(b'\r' not in raw, f'Expected LF text: {name}')
        text = raw.decode('utf-8')
        require(all(line == line.rstrip(' \t') for line in text.splitlines()), f'Trailing whitespace: {name}')
        if path.suffix == '.md':
            require(not PRIVATE.search(text), f'Private text: {name}')
            for url in re.findall(r'(?<!!)\[[^\]]+\]\(([^)]+)\)', text):
                parsed = urlsplit(url)
                if parsed.scheme:
                    require(parsed.scheme == 'https', 'Unexpected external URL')
                    continue
                target = (path.parent / unquote(parsed.path)).resolve()
                require(target.is_relative_to(ROOT) and target.is_file(), f'Broken relative link: {url}')
                require(not target.relative_to(ROOT).as_posix().startswith(('artifacts/', 'data/')), 'Private artifact link')
                links += 1
    results, configs, sources, verification = [read(name) for name in
        ['results.json', 'configurations.json', 'source_hashes.json', 'verification_evidence.json']]
    for value in [results, configs, sources, verification]:
        check_tree(value)
    require(results['status'] == 'INTERIM_FOUR_DATASETS_LCB_INCOMPLETE', 'Incorrect completeness label')
    tri = json.loads((ROOT / 'docs/evaluation_release/2026-09-06/results.json').read_text())
    for dataset in ['humanevalplus', 'mbppplus', 'codejudge_eval']:
        require(results[dataset] == tri[dataset], f'Existing results changed: {dataset}')
    he = results['humanevalplus']['execution']
    require((he['actual_execution_count'], he['base_pass_count'], he['base_plus_pass_count']) == (164, 163, 157), 'HumanEval counts')
    mb = results['mbppplus']
    require(mb['metrics']['complete'] and mb['metrics']['actual_execution_n'] == 120, 'MBPP completeness')
    require(mb['base_extra_status_counts'] == {'pass_pass': 95, 'pass_fail': 20, 'pass_timeout': 2, 'fail_fail': 3}, 'MBPP counts')
    lcb = results['livecodebench']['metrics']
    require(not lcb['complete'] and lcb['planned_n'] == 60 and lcb['actual_execution_n'] == 58, 'LCB completeness')
    require(lcb['execution_status_counts'] == {'passed': 53, 'failed': 3, 'compile_error': 1, 'timeout': 1}, 'LCB counts')
    require(lcb['generation_status_counts'] == {'success': 58, 'provider_error': 2}, 'LCB generation counts')
    require(lcb['provider_attempts'] == 74, 'LCB retry count')
    for fraction in list(results['derived_humaneval_metrics'].values()) + [
        mb['metrics']['base_plus_pass_full_denominator'], lcb['pass_full_denominator'], lcb['pass_conditional_on_execution']]:
        require(all(close(a,b) for a,b in zip(wilson(fraction['numerator'], fraction['denominator']), fraction['wilson_95'])), 'Wilson interval mismatch')
    a = results['codejudge_eval']['experiments']['v3a_complete']
    c = a['binary_metrics']['valid_confusion']
    require(c == {'tp': 59, 'tn': 49, 'fp': 11, 'fn': 1}, 'Judge confusion matrix')
    require(close((c['tp']/60+c['tn']/60)/2, a['primary']['balanced_accuracy_full_denominator']), 'Judge balanced accuracy')
    b = results['codejudge_eval']['experiments']['v3b']
    require(b['analysis_population']['complete_triplet_n'] == 50, 'Judge paired sample count')
    for condition, native in [('easy',48), ('middle',42), ('hard',36)]:
        row = b['per_condition'][condition]
        require(row['functional_accuracy_full_denominator']['numerator'] == 49 and row['native_label_exact_match_full_denominator']['numerator'] == native, 'Judge B counts')
    with (PACKAGE / 'summary.csv').open(encoding='utf-8', newline='') as stream:
        rows = list(csv.DictReader(stream))
    require(len(rows) == 4, 'Summary needs four datasets')
    for row, expected_value in zip(rows, [157/164, 95/120, 53/60, .9]):
        require(close(float(row['primary_value']), expected_value), 'Summary CSV mismatch')
    ids = {row['source_id'] for row in sources['sources']}
    require(len(ids) == len(sources['sources']), 'Duplicate source identity')
    def references(obj):
        if isinstance(obj, dict):
            if 'source_ids' in obj:
                require(set(obj['source_ids']).issubset(ids), 'Unresolved source reference')
            for value in obj.values():
                references(value)
        elif isinstance(obj, list):
            for value in obj:
                references(value)
    references(results)
    report_text = REPORT.read_text(encoding='utf-8')
    for term in ['95.73%', '79.17%', '88.33%', '90.00%', 'complete=false', '91.38%', '23 个链接']:
        require(term in report_text, 'Required result or verification statement missing')
    if args.check_index:
        for name in [*entries, str((PACKAGE/'SHA256SUMS').relative_to(ROOT)).replace('\\', '/')]:
            blob = subprocess.run(['git','show', ':'+name], cwd=ROOT, capture_output=True)
            require(blob.returncode == 0 and blob.stdout == (ROOT/name).read_bytes(), f'Index differs: {name}')
    print(json.dumps({'status':'PASS', 'hashed_public_files':len(entries), 'local_links_checked':links,
                      'source_references':len(ids), 'datasets_checked':4, 'index_checked':args.check_index,
                      'scope':'Public integrity and aggregate consistency; not a new model run or complete private-data reproduction.',
                      'real_model_api_calls':0}, ensure_ascii=False))

if __name__ == '__main__':
    main()
