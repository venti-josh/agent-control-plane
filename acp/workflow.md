Choose your working directory and any worktree using your normal workflow and
repository instructions. ACP does not manage branches or workspaces.

ACP connection: get_task reads current context; add_comment records progress;
request_input asks a blocking question; submit_for_review reports your result,
output location, and any verification performed. After requesting input or
submitting for review, end the turn so the user can respond. These tools are
scoped to this task and run; if the run is no longer active, stop.
