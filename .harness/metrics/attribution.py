"""Explicit repository-local task/PR attribution. No network, transcript content or background work."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import tempfile

ID = re.compile(r'[A-Za-z0-9_./:#@+\-]{1,160}\Z')
LIMIT = 2_000_000


class AttributionError(ValueError):
    pass


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise AttributionError('invalid task/session identifier')
    if re.search(r'(?i)(?:sk-|ghp_|github_pat_|glpat-)[A-Za-z0-9_-]{12,}', value):
        raise AttributionError('credential-like identifier refused')
    return value


def pr_number(value):
    if not isinstance(value, str) or not re.fullmatch(r'[#!]?[1-9][0-9]{0,8}', value):
        raise AttributionError('PR must be a positive repository-local number')
    return value.lstrip('#!')


def moment(value):
    try:
        result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError()
        return result.astimezone(dt.timezone.utc)
    except (ValueError, AttributeError, TypeError):
        raise AttributionError('timestamp must include a timezone') from None


def read(path):
    if not path.exists():
        return None
    try:
        if path.stat().st_size > LIMIT:
            raise ValueError()
        value = json.loads(path.read_text(encoding='utf8'))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError, UnicodeError):
        raise AttributionError('invalid attribution/binding file; summary stopped') from None


def state_path(common, target):
    value = common.git(target, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    if not value:
        raise AttributionError('repository common Git directory unavailable')
    return Path(value.strip()) / 'harness' / 'attribution.json'


def state(path):
    data = read(path)
    if data is None:
        data = {'schema_version': 1, 'bindings': [], 'tasks': {}}
    if data.get('schema_version') != 1 or not isinstance(data.get('bindings'), list) or not isinstance(data.get('tasks'), dict):
        raise AttributionError('unsupported attribution state')
    for task, pr in data['tasks'].items():
        identifier(task)
        pr_number(pr)
    for b in data['bindings']:
        validate_binding(b)
    return data


def validate_binding(b):
    if not isinstance(b, dict):
        raise AttributionError('invalid binding')
    identifier(b.get('session'))
    identifier(b.get('task'))
    start = moment(b.get('since'))
    if b.get('until') is not None and moment(b['until']) <= start:
        raise AttributionError('binding end must follow start')
    if b.get('pr') is not None:
        pr_number(b['pr'])
    return b


def bindings(common, target, roots=()):
    """Read #141 interval histories without changing or requiring agentops.py."""
    local = state_path(common, target)
    data = state(local)
    found = [dict(b, pr=pr_number(data['tasks'][b['task']]) if b['task'] in data['tasks'] else None) for b in data['bindings']]
    git_dir = local.parent.parent
    # Worktree metadata already lives here; avoid spawning Git for each worktree.
    paths = [git_dir / 'harness' / 'agentops' / 'bindings.json']
    paths += sorted((git_dir / 'worktrees').glob('*/harness/agentops/bindings.json'))
    for path in paths:
        foreign = read(path)
        if foreign is None:
            continue
        for session, active in foreign.items():
            identifier(session)
            if not isinstance(active, dict) or not isinstance(active.get('history', []), list):
                raise AttributionError('invalid #141 binding history')
            for item in [*active.get('history', []), active]:
                if not isinstance(item, dict):
                    raise AttributionError('invalid #141 binding interval')
                if item.get('task') == 'unbound':
                    continue
                b = {k: item.get(k) for k in ('task', 'since', 'until', 'pr')}
                b['session'] = session
                validate_binding(b)
                linked = data['tasks'].get(b['task'])
                if linked and b['pr'] and linked != pr_number(b['pr']):
                    raise AttributionError('task PR conflicts with #141 binding')
                b['pr'] = linked or (pr_number(b['pr']) if b['pr'] else None)
                found.append(b)
    # Identical copies from worktrees are one connection, not extra usage.
    unique = {}
    for b in found:
        unique[(b['session'],b['task'],moment(b['since']),moment(b['until']) if b.get('until') else None,b.get('pr'))] = b
    return list(unique.values())


def change(common, target, action, *, task=None, session=None, pr=None, since=None):
    path = state_path(common, target)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    stamp = since or now.isoformat()
    when = moment(stamp)
    if when > now:
        raise AttributionError('future binding refused')
    if task is not None:
        identifier(task)
    if action in ('bind', 'link'):
        identifier(task)
        if task == 'unbound':
            raise AttributionError('unbound is not an explicit task')
    if action == 'link' and pr is None:
        raise AttributionError('link requires a PR number')
    if action == 'unbind' and pr is not None:
        raise AttributionError('unbind cannot change PR mapping')
    if pr is not None:
        pr = pr_number(pr)
    if action in ('bind', 'unbind'):
        identifier(session)
    with common._file_lock(path):
        data = state(path)
        if action == 'link':
            known = [b for b in bindings(common, target) if b['task'] == task]
            if not known:
                raise AttributionError('task has no explicit binding')
            if any(b.get('pr') and b['pr'] != pr for b in known):
                raise AttributionError('task already linked to another PR')
        elif action in ('bind', 'unbind'):
            active = [b for b in data['bindings'] if b['session'] == session and b.get('until') is None]
            if len(active) > 1:
                raise AttributionError('overlapping active bindings')
            previous = active[0] if active else None
            if previous and previous['task'] == task and action == 'bind':
                if since and moment(since) != moment(previous['since']):
                    raise AttributionError('existing boundary is immutable')
            else:
                if previous:
                    if when <= moment(previous['since']):
                        raise AttributionError('new boundary must follow active start')
                    previous['until'] = stamp
                elif action == 'unbind':
                    raise AttributionError('session has no active binding')
                if action == 'bind':
                    for b in data['bindings']:
                        if b['session'] == session and (b.get('until') is None or moment(b['until']) > when):
                            raise AttributionError('historical interval overlap')
                    data['bindings'].append(dict(session=session, task=task, since=stamp, until=None))
        else:
            raise AttributionError('unknown binding action')
        if pr is not None:
            previous_pr = data['tasks'].get(task)
            if previous_pr and previous_pr != pr:
                raise AttributionError('task already linked to another PR; use a distinct task')
            data['tasks'][task] = pr
        # Check serialized size before replacing, so invalid input never destroys existing state.
        encoded = (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode()
        if len(encoded) > LIMIT:
            raise AttributionError('attribution state too large')
        fd, temporary = tempfile.mkstemp(prefix='.attribution-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(encoded)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return data


def scoped(collect, records, intervals, *, task=None, pr=None):
    """Difference cumulative values before selecting scope. Ambiguous windows stay unassigned."""
    if (task is None) == (pr is None):
        raise AttributionError('choose exactly one of task or PR')
    if task is not None:
        identifier(task)
    if pr is not None:
        pr = pr_number(pr)
    selected = [b for b in intervals if (b['task'] == task if task else b.get('pr') == pr)]
    scope = 'task:' + task if task else 'pr:' + pr
    if not selected:
        raise AttributionError('no explicit binding for requested scope')
    by_session = {}
    for b in intervals:
        by_session.setdefault(b['session'], []).append(b)
    events = {}
    seen = set()
    for record in records:
        key = json.dumps({k: record.get(k) for k in ('session','ts','event','usage','parent','verify','usage_error')}, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        parsed = collect.read_session_record(json.dumps(record))
        if parsed is None or parsed == 'malformed':
            raise AttributionError('invalid source session record')
        session, ts, kind, _, raw = parsed
        events.setdefault(session, []).append((ts, kind, raw))
    parents = {s: next((raw['parent'] for _, _, raw in rows if raw.get('parent')), None) for s, rows in events.items()}

    def candidates(session, ts):
        visited = set()
        while session not in by_session and parents.get(session):
            if session in visited:
                raise AttributionError('cyclic parent session')
            visited.add(session)
            session = parents[session]
        return [b for b in by_session.get(session, []) if moment(b['since']) <= ts and
                (not b.get('until') or ts < moment(b['until']))]

    def picked(b):
        return b in selected

    rewritten, usage, uncertain = [], {}, {}
    observed = set()
    active_seconds = 0
    for session, rows in events.items():
        cumulative = {}
        previous = None
        prior_event = None
        for ts, kind, raw in sorted(rows, key=lambda item: item[0]):
            matches = candidates(session, ts)
            # Even two connections to the same task need one unambiguous interval.
            match = matches[0] if len(matches) == 1 else None
            own = match is not None and picked(match)
            if own:
                observed.add(id(match))
            if own and not raw.get('parent') and prior_event:
                previous_ts, previous_matches = prior_event
                if previous_matches == matches and collect.continues(previous_ts, ts, kind):
                    active_seconds += (ts - previous_ts).total_seconds()
            prior_event = (ts, matches)
            rewritten.append(json.dumps({**raw, 'branch': scope if own else '__unassigned__', 'usage': None}))
            current = collect.clean_usage(raw.get('usage'))
            if current is None:
                if raw.get('usage') is not None:
                    raise AttributionError('invalid source token snapshot')
                if previous is None:
                    previous = ts
                continue
            delta, cumulative = collect.usage_delta(current, cumulative, isinstance(raw.get('usage_error'), str))
            # Without an earlier observation inside this exact interval, an increment
            # spanning a task switch cannot safely be charged to the later task.
            safe = own and previous is not None and moment(match['since']) <= previous
            if safe:
                collect.add_usage(usage, delta)
                if raw.get('usage_error'):
                    uncertain[session] = ['partial-source-snapshot']
            elif any(picked(b) for b in matches) or (match is None and any(b['session'] == session for b in selected)):
                uncertain[session] = ['unassigned-boundary-or-overlap']
            previous = ts
    result = collect.summarize_sessions(rewritten, scope)
    for b in selected:
        if id(b) not in observed:
            uncertain[b['session']] = ['missing-source-or-scope-observation']
    result['usage'] = usage
    result['active_sec'] = int(active_seconds)
    result['usage_errors'].update(uncertain)
    result['attribution_scope'] = scope
    return result
