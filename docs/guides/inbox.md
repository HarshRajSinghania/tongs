---
title: Inbox
description: "Open pull requests and merge requests from GitHub and GitLab in one list, found from the repositories on your disk."
---

The inbox is the first screen tongs shows. It lists open pull requests and
merge requests from GitHub and GitLab in one table. tongs calls both of them
reviews.

## How repo discovery works

On startup, tongs walks your scan root (`~/git` by default) up to `scan_depth`
levels deep, five by default. Each directory with a `.git` folder is a
repository. tongs skips hidden directories, symlinks and repositories nested
inside another repository.

For each repository, tongs reads the git remotes and works out:

- whether the remote points to GitHub or GitLab. Hosts named in `[hosts.*]`
  count, and so do hostnames that contain `github` or `gitlab`.
- the project namespace and name.

Repositories with no recognized remote, such as local-only or Bitbucket
repositories, are skipped without a message. Set the scan root and depth in
the [configuration](/reference/configuration/).

## Inbox tabs

The inbox has three tabs. Switch between them with the number keys.

| Key | Tab | Shows |
|-----|-----|-------|
| ++1++ | My Reviews | Reviews where you are a requested reviewer |
| ++2++ | My MRs | Reviews you authored |
| ++3++ | All Open | Every open review in the discovered repositories |

My Reviews and My MRs ask each forge host that your repositories point to.
All Open lists the open reviews of each discovered repository. A tab loads the
first time you open it, and tongs queries repositories and hosts in parallel.
If one host fails, the others still load and tongs names the host that was
skipped.

## The review list

Each row shows these columns:

| Column | Shows |
|--------|-------|
| CI | Pipeline status: passed, failed, running, pending, canceled or skipped |
| # | Pull request or merge request number |
| Title | Title, with a dim `D` in front of draft reviews |
| Author | Author's username |
| Repo | Project path, such as `acme/api` |
| Updated | Time since the last update, such as `3h ago` |

With `ascii_mode` on, the CI column shows `OK`, `FAIL`, `RUN`, `PEND`, `CANC`
and `SKIP` instead of symbols.

Press ++s++ to cycle the sort order: updated, title, CI status, author.
Press ++enter++ on a row to open the review, and ++o++ to open it in your
browser.

## Per-repo scoped inbox

Filter the inbox to a single project from the repo list.

<figure class="shot"><a href="/media/repos.webp" aria-label="Open the full-size screenshot of the repo list"><div class="frame"><div class="bar" data-pagefind-ignore><span><b>tongs</b> &middot; repos</span><span class="meta"><span class="full">full size &#8599;</span></span></div><picture><source media="(max-width: 560px)" srcset="/media/repos-m.webp" width="500" height="452" /><img src="/media/repos.webp" width="760" height="452" loading="lazy" decoding="async" alt="The tongs repo list with GitHub and GitLab repositories sorted by forge."></picture></div></a><figcaption class="cap">demo data, cropped on small screens</figcaption></figure>

1. Press ++r++ to open the repo list.
2. Press ++slash++ to filter repositories by name. ++escape++ closes the
   filter and clears it.
3. Press ++f++ to cycle the forge filter: All, GH, GL.
4. Press ++s++ to cycle the sort order: name, forge, host.
5. Press ++enter++ on a repository to open an inbox for that project only.

The scoped inbox has the same three tabs and hides the Repo column. Press ++r++
or ++escape++ to go back to the repo list, and ++escape++ again to return to
the full inbox.

## Refreshing

Press ++ctrl+r++ to reload the current tab from the forge. All Open normally
reads through the local cache, which keeps each list for `mr_list_ttl` seconds
(60 by default); a refresh skips that cache and stores the new list in its
place. Retrying or cancelling a pipeline also drops that repository's cached
list, so its CI status is read again. A refresh also retries a sign-in that
failed a moment ago, so a fixed token works without waiting. The desktop app's
Refresh reviews button does the same. In the repo list, ++ctrl+r++ scans the
scan root again.
