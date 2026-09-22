"""Read-only, fixed-catalog onboarding from the installed kit and project."""
from pathlib import Path
from version import line, report

DOCUMENTS = {
    'start': 'docs/WORKER_START.md',
    'workflow': 'docs/WORKFLOW.md',
    'worker-guide': 'docs/WORKER_GUIDE.md',
    'sessions': 'docs/SESSIONS.md',
    'reviews': 'docs/REVIEWS.md',
    'contribution-template': 'templates/CONTRIBUTION.json',
    'review-request-template': 'templates/REVIEW_REQUEST.json',
    'review-response-template': 'templates/REVIEW_RESPONSE.json',
    'handoff-template': 'templates/HANDOFF.json',
    'briefings': 'docs/BRIEFINGS.md',
    'cli-contract': 'docs/CLI_CONTRACT.md',
    'operations': 'docs/OPERATIONAL_WORKFLOW.md',
    'checkpoint-template': 'templates/CHECKPOINT.json',
    'task-template': 'templates/TASK.md',
    'report-template': 'templates/REPORT.md',
    'decision-template': 'templates/DECISION.md',
}
PROJECT_LIMIT = 8000

def write_project(path, text):
    import os
    import tempfile
    if not isinstance(text, str) or not text.strip() or len(text.encode('utf-8')) > PROJECT_LIMIT:
        raise ValueError('Project onboarding must be nonempty UTF-8 text up to 8000 bytes')
    path = Path(path)
    if path.is_symlink():raise ValueError('Project onboarding must not be a symlink')
    fd, temporary = tempfile.mkstemp(prefix='.onboarding-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text);f.flush();os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)

def read_document(base, relative, limit=64000):
    base = Path(base).resolve()
    path = base / relative
    if any(p.is_symlink() for p in [path, *path.parents] if p != base and base in p.parents):
        raise ValueError('Onboarding document must not use symlinks')
    if not path.resolve().is_relative_to(base):
        raise ValueError('Document outside configured root')
    if not path.is_file():
        raise ValueError('Onboarding document missing; ask the operator to configure/update this installation')
    with path.open('rb') as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise ValueError('Onboarding document exceeds size limit; operator must shorten it (no partial instructions returned)')
    result = data.decode('utf-8-sig')
    if not result.strip():raise ValueError('Onboarding document is empty')
    return result

def execute(kit, project_path, project, actor, action, args):
    if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
        raise ValueError('Expected argument list')
    catalog = '\n'.join('  docs '+key for key in ['project', *DOCUMENTS])
    if action == 'docs':
        if not args:return 'Installed onboarding documents (use your existing client prefix):\n'+catalog+'\n'
        if len(args) != 1 or args[0] not in {'project', *DOCUMENTS}:
            raise ValueError('Unknown document; run docs for the fixed catalog')
        if args[0] == 'project':return read_document(project_path, 'ONBOARDING.md', PROJECT_LIMIT)
        return read_document(kit, DOCUMENTS[args[0]])
    if action != 'onboard' or args:raise ValueError('Use onboard without arguments')
    # Read both before constructing output: never return a plausible but incomplete start.
    entry = read_document(project_path, 'ONBOARDING.md', PROJECT_LIMIT)
    start = read_document(kit, DOCUMENTS['start'], 8000)
    metadata = report(kit)
    return (f'# Orchestra onboarding\n\nProject: {project}\nSession actor: {actor}\n'
            f'{line(metadata)}\n\n'
            + start+'\n\n# Project entry point\n\n'+entry
            +'\n\n# Supporting documents\n\n'+catalog+'\n')
