Choose your working directory and any worktree using your normal workflow and
repository instructions. ACP does not manage branches or workspaces.

ACP connection: get_task reads current context; add_comment records progress;
request_input asks a blocking question; submit_for_review reports your result,
output location, and any verification performed. After requesting input or
submitting for review, end the turn so the user can respond. These tools are
scoped to this task and run; if the run is no longer active, stop.

Put review evidence in ACP comments or the review summary, not only in your chat
reply. Include screenshots as ![description](</absolute/path/screenshot.png>) and
documents as [description](</absolute/path/report.md>). Use absolute local paths
with angle brackets, or existing HTTPS links. For a primary file deliverable, use
output_url with kind="file". ACP snapshots explicitly linked local files when you
report them and uploads them to the linked Linear issue during comment sync.
Each file must exist and be readable when first submitted (20 MiB per file,
50 MiB total per comment). Include only intended evidence, never credentials or
unrelated files. Plain paths, relative links, and chat-only attachments are not
uploaded. Report actual verification and any missing evidence honestly.
