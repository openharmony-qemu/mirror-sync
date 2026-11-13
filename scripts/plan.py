#!/usr/bin/env python3
import json
import os
from pathlib import Path


def main():
    requested = {x.strip() for x in os.environ.get('SELECT_REPOSITORIES', '').split(',') if x.strip()}
    active = [x for x in json.loads(Path('repositories.json').read_text())['repositories'] if x['enabled']]
    unknown = requested - {x['name'] for x in active}
    if unknown:
        raise ValueError('Unknown or disabled repositories: ' + ', '.join(sorted(unknown)))
    matrix = {'include': [{'repository': x['name']}
                          for x in active if not requested or x['name'] in requested]}
    if not matrix['include'] or len(matrix['include']) > 256:
        raise ValueError('Matrix must contain between 1 and 256 repositories')
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write('matrix=' + json.dumps(matrix, separators=(',', ':')) + '\n')


if __name__ == '__main__':
    main()
