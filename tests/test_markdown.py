"""Task text is markdown: the board renders it, and nothing in it can run as HTML."""
from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess

import pytest

from test_board_status import render


def md(*texts):
    script = """
const fs = require('fs'), vm = require('vm');
const context = vm.createContext({});
const source = fs.readFileSync('acp/static/app.js', 'utf8');
vm.runInContext(source.slice(0, source.lastIndexOf('init().catch')), context);
context.texts = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(vm.runInContext('texts.map(t => [md(t), plain(t)])', context)));
"""
    result = subprocess.run(['node', '-e', script], input=json.dumps(texts), text=True, capture_output=True,
                            cwd=Path(__file__).parents[1], check=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize('source,html', [
    ('plain text\nwith lines', '<p>plain text<br>with lines</p>'),
    ('# Plan\n\nSome **bold**, *em*, ~~old~~ and `co<de>` text',
     '<h1>Plan</h1><p>Some <strong>bold</strong>, <em>em</em>, <del>old</del> and <code>co&lt;de&gt;</code> text</p>'),
    ('- a\n- [ ] todo\n- [x] done\n  - nested\n\n1. one\n2. two',
     '<ul><li>a</li><li class="task"><input type="checkbox" disabled> todo</li><li class="task"><input type="checkbox" disabled checked> done'
     '<ul><li>nested</li></ul></li></ul><ol><li>one</li><li>two</li></ol>'),
    ('> quoted\n> more\n\n---\n\n```js\nconst x = "<b>";\n```',
     '<blockquote><p>quoted<br>more</p></blockquote><hr><pre><code class="lang-js">const x = &quot;&lt;b&gt;&quot;;</code></pre>'),
    ('| a | b |\n|---|---|\n| 1 | **2** |',
     '<table><thead><tr><th>a</th><th>b</th></tr></thead><tbody><tr><td>1</td><td><strong>2</strong></td></tr></tbody></table>'),
    ('See https://example.com/x?a=1&b=2. Or [docs](https://ex.com/).',
     '<p>See <a href="https://example.com/x?a=1&amp;b=2" target="_blank" rel="noopener noreferrer">https://example.com/x?a=1&amp;b=2</a>. '
     'Or <a href="https://ex.com/" target="_blank" rel="noopener noreferrer">docs</a>.</p>'),
    ('snake_case_name, 2*3*4 and a_b stay literal', '<p>snake_case_name, 2*3*4 and a_b stay literal</p>'),
    ('[ask] The agent pauses with a question.', '<p>[ask] The agent pauses with a question.</p>'),
])
def test_markdown_renders(source, html):
    assert md(source)[0][0] == html


def test_evidence_links_show_their_local_path_without_becoming_attributes():
    (html, _), = md('Shot: ![board](</tmp/board shot.png>) and [report](</tmp/report.md>)')
    assert html == '<p>Shot: board <code>/tmp/board shot.png</code> and report <code>/tmp/report.md</code></p>'


class Tags(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags, self.attrs = [], []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs.extend(attrs)


def test_raw_html_and_unsafe_urls_never_reach_the_dom():
    (html, _), = md('<script>alert(1)</script> [x](javascript:alert(1)) ![y](data:text/html,hi) '
                    '<img src=x onerror=alert(1)> **<b>bold</b>**')
    parsed = Tags(html)
    assert parsed.tags == ['p', 'strong'] and parsed.attrs == []
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in html
    assert '[x](javascript:alert(1))' in html and '<strong>&lt;b&gt;bold&lt;/b&gt;</strong>' in html
    (html, _), = md('```\n<script>x</script>\n```')
    assert html == '<pre><code>&lt;script&gt;x&lt;/script&gt;</code></pre>'


def test_card_preview_strips_markdown_syntax():
    _, preview = md('# Plan\n- [x] **done** `fast`\n- see [docs](https://ex.com)')[0]
    assert preview == 'Plan\ndone fast\nsee docs'


def test_task_dialog_renders_description_comments_and_outputs():
    comment = {'id': 'c', 'body': '## Findings\n- one\n- two', 'author': 'agent', 'in_reply_to': None, 'created_at': 1700000002}
    result = render('ready_for_review', 'succeeded', extra={
        'description': 'Fix the **flaky** test\n\n```sh\nmake check\n```',
        'comments': [comment], 'blocker_comment_id': 'c',
        'artifacts': [{'kind': 'text', 'url': '', 'summary': 'Done. See `tests/`'}]})
    assert '<div class="task-description md"><p>Fix the <strong>flaky</strong> test</p><pre><code class="lang-sh">make check</code></pre></div>' in result['summary']
    assert '<div class="md"><h2>Findings</h2><ul><li>one</li><li>two</li></ul></div>' in result['summary']
    assert result['comments'].count('<div class="comment-body md"><h2>Findings</h2><ul><li>one</li><li>two</li></ul></div>') == 1
    assert '<div class="md"><p>Done. See <code>tests/</code></p></div>' in result['outputs']
    # Cards keep a plain preview; the code fence and emphasis markers are gone, not rendered.
    assert 'Fix the flaky test\nmake check' in result['board'] and '<strong>' not in result['board']
