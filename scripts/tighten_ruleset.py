#!/usr/bin/env python3
"""Plan or explicitly narrow one repository-role bypass to PR-only (never loosen).

Requires an operator's GitHub CLI administration credential. No merge operation.
Docs: https://docs.github.com/en/rest/repos/rules#update-a-repository-ruleset
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import subprocess


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def plan(current, repository, ruleset_id, actor_id):
    if (current.get('id') != ruleset_id or current.get('source') != repository or
            current.get('source_type') != 'Repository' or current.get('target') != 'branch' or
            current.get('enforcement') != 'active'):
        raise ValueError('expected the explicit active repository branch ruleset')
    actors = current.get('bypass_actors')
    if not isinstance(actors, list):
        raise ValueError('bypass actors not visible; administration access is required')
    desired = copy.deepcopy(actors)
    matches = [a for a in desired if a.get('actor_type') == 'RepositoryRole' and a.get('actor_id') == actor_id]
    if len(matches) != 1 or matches[0].get('bypass_mode') not in {'always', 'pull_request'}:
        raise ValueError('expected exactly one always/PR-only repository-role actor')
    matches[0]['bypass_mode'] = 'pull_request'
    return {'repository': repository, 'ruleset_id': ruleset_id, 'expected_digest': digest(current),
            'changed': desired != actors, 'body': {'bypass_actors': desired}}


def apply(request, path, current, proposed, backup):
    if digest(request('GET', path)) != proposed['expected_digest']:
        raise ValueError('ruleset changed; review a fresh plan')
    # Exclusive backup creation prevents overwriting an earlier recovery snapshot.
    with backup.open('x', encoding='utf-8') as handle:
        handle.write(json.dumps(current, indent=2) + '\n')
    backup.chmod(0o600)
    request('PUT', path, proposed['body'])  # Only bypass_actors, never replace protection rules.
    after = request('GET', path)
    if after.get('bypass_actors') != proposed['body']['bypass_actors']:
        raise RuntimeError('bypass readback mismatch; do not retry blindly')
    for key in ('name', 'target', 'enforcement', 'conditions', 'rules'):
        if after.get(key) != current.get(key):
            raise RuntimeError('concurrent protection change detected; review backup and live state')
    return {'status': 'applied', 'ruleset_id': after['id'], 'backup': str(backup)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('repository')
    parser.add_argument('--ruleset-id', type=int, required=True)
    parser.add_argument('--actor-id', type=int, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--expected-digest')
    parser.add_argument('--backup', type=Path)
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repository):
        parser.error('expected owner/repository')
    if args.ruleset_id <= 0 or args.actor_id <= 0:
        parser.error('IDs must be positive')
    def request(method, path, body=None):
        command = ['gh', 'api', '--method', method, path, '-H', 'X-GitHub-Api-Version: 2026-03-10']
        if body is not None:
            command += ['--input', '-']
        return json.loads(subprocess.run(command, input=json.dumps(body) if body is not None else None,
                         text=True, capture_output=True, check=True, timeout=30).stdout)
    path = f'repos/{args.repository}/rulesets/{args.ruleset_id}'
    current = request('GET', path)
    proposed = plan(current, args.repository, args.ruleset_id, args.actor_id)
    if args.apply and proposed['changed']:
        if args.expected_digest != proposed['expected_digest'] or args.backup is None:
            parser.error('--apply needs the reviewed --expected-digest and a new --backup path')
        result = apply(request, path, current, proposed, args.backup)
    else:
        result = proposed
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
