"""Exercise the real board renderer without a browser dependency in make check."""
import json
from html.parser import HTMLParser
from pathlib import Path
import subprocess

import pytest

from acp.config import STATES, LABELS


class Text(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.parts = []
        self.feed(html)

    def handle_data(self, data):
        self.parts.append(data)


def render(state, outcome, started=True):
    script = """
const fs = require('fs'), vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const nodes = {};
const context = vm.createContext({document: {
  getElementById: id => nodes[id] ||= {dataset:{}, value:'', innerHTML:''},
  querySelectorAll: () => []
}});
const source = fs.readFileSync('acp/static/app.js', 'utf8');
vm.runInContext(source.slice(0, source.lastIndexOf('init().catch')), context);
context.input = input;
vm.runInContext(`
config = {states:input.states};
selected = {id:'task', title:'Recovered task', description:'', provider:'codex',
  state:input.state, comments:[], artifacts:[], runs:[{id:'run', status:input.outcome,
  trigger:'todo', created_at:1700000000, started_at:input.started ? 1700000001 : null,
  finished_at:['running','queued'].includes(input.outcome) ? null : 1700000060,
  error: input.outcome === 'failed' ? 'Historical <timeout>' : null,
  desktop: input.outcome === 'running' ? {phase:'dispatched'} : null}]};
tasks = [{...selected, run_status:input.outcome, run_started_at:selected.runs[0].started_at}];
drawBoard(); drawTask();
`, context);
process.stdout.write(JSON.stringify({board:nodes.board.innerHTML,
  history:nodes['task-runs'].innerHTML,
  outcome:vm.runInContext('selected.runs[0].status',context)}));
"""
    result = subprocess.run(['node', '-e', script], input=json.dumps({
        'state': state, 'outcome': outcome, 'started': started,
        'states': [{'id': s, 'label': label} for s, label in zip(STATES, LABELS)],
    }), text=True, capture_output=True, cwd=Path(__file__).parents[1], check=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize('state,label', list(zip(STATES, LABELS)))
def test_card_uses_current_task_state_despite_failed_run(state, label):
    result = render(state, 'failed')
    card = result['board'].split('<button class="card"', 1)[1].split('</button>', 1)[0]
    text = Text(card).parts
    assert label in text
    assert 'failed' not in text
    assert result['outcome'] == 'failed'


def test_successful_turn_does_not_imply_task_completion():
    result = render('waiting_on_input', 'succeeded')
    card = result['board'].split('<button class="card"', 1)[1].split('</button>', 1)[0]
    assert 'Waiting on Input' in Text(card).parts
    assert 'succeeded' not in Text(card).parts


def test_failed_run_stays_dated_and_labeled_in_history():
    result = render('done', 'failed')
    assert 'Past run: failed' in result['history']
    assert 'Finished:' in result['history']
    assert 'Run error: Historical &lt;timeout&gt;' in result['history']
    assert '<timeout>' not in result['history']
    assert result['outcome'] == 'failed'


@pytest.mark.parametrize('started,label', [(False, 'Awaiting dispatcher'), (True, 'running')])
def test_active_run_is_distinguished_from_history(started, label):
    result = render('in_progress' if started else 'todo', 'running', started)
    assert 'Current run: ' + label in result['history']
    assert 'Past run:' not in result['history']
    assert 'Finished:' not in result['history']
