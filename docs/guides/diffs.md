---
title: Diffs and comments
description: "Read a review's diff in unified or split view, select lines, and comment or suggest changes inline."
opens: "++2++ from a review"
---

The Diff tab is where you read a review and comment on it. Press ++2++ from
any review to open it.

<figure class="shot"><a href="/media/diff-poster.webp" aria-label="Open the full-size screenshot of the diff viewer"><div class="frame"><div class="bar" data-pagefind-ignore><span><b>tongs</b> &middot; diff</span><span class="meta"><span class="full">full size &#8599;</span></span></div><picture><source media="(max-width: 560px)" srcset="/media/diff-m.webp" width="640" height="560" /><img src="/media/diff-poster.webp" width="1280" height="640" alt="The tongs diff viewer with the changed-files tree on the left and a unified, syntax-highlighted Python diff on the right, with word-level highlights on the changed values."></picture></div></a><figcaption class="cap">demo data, cropped on small screens</figcaption></figure>

## Layout

The Diff tab has two panes:

- **File tree** on the left. It lists every changed file with a status letter
  (`M` modified, `A` added, `D` deleted, `R` renamed), its `+` and `-` line
  counts, and how many of its comment threads are open or resolved.
- **Diff** on the right. It shows the selected file with syntax highlighting,
  line numbers and gutter markers.

## Unified and split view

Press ++v++ to switch between unified and split view. Split view shows the old
and new versions side by side with their lines aligned. Press ++h++ to move
focus to the old side and ++l++ to move it to the new side.

When the terminal is too narrow for split view, tongs uses unified view and
tells you: "Split view selected; using unified at this width".

## Moving around

| Key | Action |
|-----|--------|
| ++j++ / ++k++ | Move the cursor down or up one line |
| ++n++ / ++shift+n++ | Next or previous file, wrapping at either end |
| ++close-bracket++ / ++open-bracket++ | Next or previous comment thread |

Click a file in the tree to jump straight to it.

## Selecting lines

Select a range of lines to comment on several lines at once or to suggest a
replacement.

| Key | Action |
|-----|--------|
| ++shift+j++ | Extend the selection down |
| ++shift+k++ | Extend the selection up |
| ++ctrl++ + click | Extend the selection to the clicked line |
| ++escape++ | Clear the selection |

## Reading the diff

- **Syntax highlighting.** tongs picks a highlighter from the file name, using
  Pygments.
- **Word-level changes.** In the unified view, the words that changed inside a
  changed line are bold and underlined, so a small edit in a long line stands out.
- **Unchanged lines.** The forge sends only the changed parts of a file, so
  the lines around them start out hidden behind a row such as
  `... 42 unchanged lines (e to expand) ...`. See
  [Expanding context](#expanding-context).
- **Markdown preview.** On a Markdown file, press ++m++ to switch between the
  diff and the rendered file.
- **Files without a diff.** When the forge does not expose a file's diff, the
  file stays in the tree with its line counts and the pane says why. Binary
  files, empty files, mode changes and pure renames get a short label instead
  of a diff. Press ++o++ to open the review in your browser. A diff that is too
  large for the forge can be generated in tongs instead; see
  [Large files](#large-files).

## Expanding context

A diff shows each change with a few lines around it. The rest of the file
hides behind gap rows: one above the first change, one between each pair of
changes and one below the last. Each row says how many lines it hides, for
example `... 120 unchanged lines (e to expand) ...`. The row below the last
change reads `... more lines below (e to expand) ...` until tongs knows how
long the file is. Long unchanged stretches inside a change fold the same way.

| Key | Action |
|-----|--------|
| ++e++ | Expand the gap row under the cursor, or collapse the expanded lines under it |
| ++shift+f++ | Show the whole file, or go back to what you had expanded |

- **One request per file.** The first ++e++ or ++shift+f++ on a file reads it
  from the forge at the review's latest commit, once. Later expands and the
  whole-file view use that copy. Opening a diff never reads a file, and a fold
  inside a change opens without any request.
- **Collapse.** ++e++ toggles. On an expanded line it hides that gap again and
  puts the cursor on its gap row. On a line shown from a fold inside a change
  it folds those lines again. Lines a change always showed stay as they are.
  Collapsing needs no request, and ++e++ on the gap row brings the lines back
  from the copy tongs already has. In the whole-file view, ++e++ on a line it
  showed turns the view off and hides that gap or fold, keeping the gaps you
  had expanded before.
- **Both views.** Expanding works the same in unified and split view, and
  ++v++ keeps what you expanded. Expanded lines show both line numbers and the
  same syntax highlighting as the rest of the file.
- **Whole file.** ++shift+f++ works per file. The file header shows
  **Full file** while it is on, and moving to another file turns it off.
- **Read-only lines.** You can't comment on an expanded line. GitHub accepts
  comments only on lines inside the changed parts, and tongs checks the same
  rule for every forge. ++c++, ++f3++, ++d++, ++r++ and ++shift+r++ on an
  expanded line say so. On a fold inside a change they ask you to press ++e++
  first. A selection can't cross a gap row between changes, but it can span a
  fold inside a change.
- **Limits.** tongs expands files up to 2 MiB and 20,000 lines. Past that,
  pressing ++e++ says the file is too large. ++shift+f++ also stops at 20,000
  lines on screen and asks you to expand single gaps instead.
- **Files that can't expand.** An added or deleted file already shows every
  line. Binary files, symlinks and submodules have no lines to expand, and
  tongs says why. A file the forge did not send expands once you generate its
  diff, as described in [Large files](#large-files).

## Large files

When a file's diff is too large for the forge to send, GitHub leaves it out and
GitLab marks it as too large. tongs keeps the file in the tree and shows:

> This file's diff is too large for the forge to send. Press e to generate it
> locally (downloads both versions of the file).

When the forge sends only part of a file's diff, the same notice stands above
the parts it sent, in both views. ++e++ on a fold inside those parts opens the
fold, and on a line the fold showed it folds them again; anywhere else it
generates the diff.

| Key | Action |
|-----|--------|
| ++e++ | Generate the diff of a large file, or cancel it while it runs |

- **How it works.** tongs downloads the file as it was where the branch
  started and as it is at the review's latest commit, compares the two on your
  computer, and shows the result like any other diff, in unified and split
  view. The file header shows **Generated locally**. GitLab already reports
  where the branch started; on GitHub, tongs asks the forge for it with one
  extra request. An added or deleted file downloads only the version it has.
- **Cancel.** While it runs, the notice reads "Generating diff locally
  (downloading 2 files). Press e again to cancel." Press ++e++ again to stop
  it. ++ctrl+r++, leaving the review and reloading the diff stop it too. The
  notice then says "Cancelled. Press e to try again."
- **Expanding a generated diff.** Gap rows, collapsing and ++shift+f++ work
  as described in [Expanding context](#expanding-context), with no further
  request, because tongs already has the file.
- **Kept while tongs runs.** tongs keeps the last 8 generated diffs in memory,
  so generating the same file again, for example after reopening the review,
  needs no download. A new commit on the review starts over.
- **Limits.** Each version of the file may be up to 5 MiB and 100,000 lines,
  and the generated diff up to 100,000 lines. Past that, or when a version is
  binary, tongs says so and leaves the notice in place. Press ++o++ to read the
  file in your browser instead.
- **No comments.** The forge has no diff for this file to attach a comment to,
  so it would refuse one. ++c++, ++f3++ and ++r++ on a generated diff say
  "Comments are not available on a diff generated locally.", also while a
  review draft is open. Comment on the file in the browser instead.

## Comment threads in the diff

Existing threads show as gutter markers next to their lines. Press ++d++ on a
marked line to expand or collapse its thread. On a line with a thread, press
++r++ to reply and ++shift+r++ to resolve or reopen it. tongs asks you to press
++shift+r++ a second time to confirm.

### Adding a comment

1. Move to the line, or select the lines, you want to comment on.
2. Press ++c++ to open the comment editor at the bottom of the screen.
3. Write your comment.
4. Press ++ctrl+s++ to submit, or ++escape++ to cancel. If the editor has text,
   press ++escape++ twice to discard it.

### Suggesting changes

1. Select the lines you want to replace. Suggestions work on the new side of
   the diff only.
2. Press ++f3++. tongs opens your editor with the selected code filled in.
3. Edit the code into the replacement you suggest.
4. Save and close the editor.
5. tongs posts the suggestion in the forge's own syntax: `` ```suggestion ``
   on GitHub and `` ```suggestion:-0+N `` on GitLab.

If you close the editor without changing the code, tongs cancels the
suggestion.

### External editor

Press ++f2++ in the comment editor to continue the comment in an external
editor. tongs moves the text to the editor and back when you save and close
it.

++f2++ and ++f3++ use `$VISUAL`, then `$EDITOR`, then the first of `nvim`,
`vim`, `vi` and `nano` found on your `PATH`. ++f2++ is not available on
Windows.

:::tip[Review drafts]
While a review draft is open (++ctrl+g++), ++c++, ++r++ and ++f3++ add to the
draft instead of posting right away. You submit the whole draft once. See
[Review drafts](/guides/review-drafts/).
:::
