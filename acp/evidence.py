"""Snapshot explicitly linked evidence before the asynchronous Linear delivery."""
from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import stat
from pathlib import Path
from urllib.parse import unquote, urlparse

from markdown_it import MarkdownIt
from markdown_it.rules_inline import StateInline

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_COMMENT_BYTES = 50 * 1024 * 1024
# Inline links only. Code examples are deliberately excluded; angle brackets
# support filenames with spaces and arbitrary parentheses without ambiguity.
LINK_START = re.compile(r'(?<![\\!])!?\[')
DESTINATION = re.compile(
    r'\([ \t]*(?:<(?P<angle>[^>\n]+)>|'
    r'(?P<plain>(?:\\.|[^()\n]|\([^()\n]*\))+?))'
    r'''(?:[ \t]+(?:"[^"\n]*"|'[^'\n]*'|\([^()\n]*\)))?[ \t]*\)'''
)
INLINE_NON_PROSE = re.compile(
    r'<!--[\s\S]*?(?:-->|$)|'
    r'(?<!`)(?P<ticks>`+)(?!`)[\s\S]*?(?<!`)(?P=ticks)(?!`)'
)
MARKDOWN = MarkdownIt('commonmark')


def prose_only(body):
    """Use CommonMark block boundaries, retaining source offsets for rewriting."""
    lines = body.splitlines(keepends=True)
    for token in MARKDOWN.parse(body):
        if token.type in {'fence', 'code_block', 'html_block'} and token.map:
            start, end = token.map
            for index in range(start, end):
                lines[index] = re.sub(r'[^\n]', ' ', lines[index])
    # Whichever opens first wins: comments may contain backticks, and code
    # spans may contain literal comment openers. Preserve offsets in both.
    return INLINE_NON_PROSE.sub(lambda m: re.sub(r'[^\n]', ' ', m[0]), ''.join(lines))


def local_links(body):
    prose = prose_only(body)
    state = StateInline(prose, MARKDOWN, {}, [])
    position, destinations = 0, []
    while opener := LINK_START.search(prose, position):
        position = opener.end()
        if any(m.start() <= opener.start() < m.end() for m in destinations):
            continue
        label_end = MARKDOWN.helpers.parseLinkLabel(state, opener.end() - 1, True)
        if label_end < 0:
            continue
        match = DESTINATION.match(prose, label_end + 1)
        if not match:
            continue
        destinations.append(match)
    # Continue into labels to find linked images, but never scan a destination
    # or title as another link. Rewrite in source order even for nested labels.
    for match in sorted(destinations, key=lambda m: m.start()):
        group = 'angle' if match['angle'] is not None else 'plain'
        target = match[group]
        value = re.sub(r'\\([\\ ()\[\]])', r'\1', target)
        if value.startswith('file://'):
            parsed = urlparse(value)
            if parsed.netloc not in {'', 'localhost'}:
                continue
            value = unquote(parsed.path)
        elif value.startswith('/') and not value.startswith('//'):
            value = unquote(value)
        else:
            continue
        # Source-code line references are navigation links, not attachments.
        if re.search(r'(?::\d+(?::\d+)?|#L\d+(?:-L\d+)?)$', value):
            continue
        yield match.start(group), match.end(group), target, Path(value)


def snapshot_files(db, comment_id, body):
    """Called in the comment transaction; any invalid file rolls back the report."""
    from .service import DomainError

    previous = {r['target']: dict(r) for r in db.execute(
        'SELECT * FROM comment_files WHERE comment_id=?', (comment_id,))}
    snapshots, total = {}, 0
    for _, _, target, path in local_links(body):
        if target in snapshots:
            continue
        old = previous.get(target)
        try:
            # NONBLOCK plus fstat rejects pipes/devices without waiting for data.
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError('must be a regular file')
                if info.st_size > MAX_FILE_BYTES:
                    raise ValueError('exceeds the 20 MiB file limit')
                content = stream.read(MAX_FILE_BYTES + 1)
            if len(content) > MAX_FILE_BYTES:
                raise ValueError('exceeds the 20 MiB file limit')
        except FileNotFoundError:
            if not old:
                raise DomainError(f'Cannot read evidence file: {path}', 422)
            content = old['content']
        except (OSError, ValueError) as exc:
            raise DomainError(f'Cannot read evidence file {path}: {exc}', 422) from exc
        total += len(content)
        if total > MAX_COMMENT_BYTES:
            raise DomainError('Linked evidence exceeds the 50 MiB comment limit', 422)
        digest = hashlib.sha256(content).hexdigest()
        mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        asset_url = old['asset_url'] if old and old['digest'] == digest else None
        snapshots[target] = (comment_id, target, path.name, mime, content, digest, asset_url)
    db.execute('DELETE FROM comment_files WHERE comment_id=?', (comment_id,))
    db.executemany('INSERT INTO comment_files VALUES(?,?,?,?,?,?,?)', snapshots.values())


def replace_links(body, urls):
    for start, end, target, _ in reversed(list(local_links(body))):
        if target in urls:
            body = body[:start] + urls[target] + body[end:]
    return body
