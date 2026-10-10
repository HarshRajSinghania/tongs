# The `www.tongs.tools` site

Astro 7 and Starlight 0.42. The homepage is `src/pages/index.astro`; the docs
are the Markdown files in the repository's `docs/`, reached through the
committed symlink `src/content/docs -> ../../../docs`.

```bash
npm ci --prefix site
npm run build --prefix site     # strict: broken links or anchors fail it; output in site/dist
npm run dev --prefix site       # local server with reload
```

Run Node under the repository's memory guard (see `.agents/testing/README.md`).

## Where things live

| Path | What |
|---|---|
| `src/data/nav.mjs` | The one IA: sidebar, header links, homepage routes, footer, redirects |
| `src/data/home.mjs` | Homepage key grid and review draft steps |
| `src/plugins/keys.mjs` | `++key++` parsing and keycap rendering |
| `src/plugins/markdown.mjs` | Sätteri plugins: keycaps, table `data-label` for phones |
| `src/components/starlight/` | Starlight overrides: Head, Header, SiteTitle, ThemeSelect, ThemeProvider (blocked-storage-safe theme bootstrap), PageTitle, Footer, PageFrame |
| `src/styles/tokens.css` | Brand and semantic tokens, shared components |
| `src/styles/docs.css` | `--sl-*` token bridge and docs chrome |
| `src/styles/home.css` | Homepage and 404 only |
| `ec.config.mjs` | Expressive Code: one Monokai theme with AAA lifts |
| `THIRD_PARTY.md` | Licenses of fonts, theme and media |

## Writing docs

Front matter replaces the Markdown `# H1`:

```yaml
---
title: Pipelines and logs          # the h1 and the browser title
description: One sentence for search results and social cards.
opens: "++5++ from a review"      # optional: "open with [5] from a review" in the eyebrow
lead: Optional larger first paragraph under the title.
eyebrow: pipelines                 # optional: last segment of "$ docs / review / pipelines"
---
```

- Keys: `++ctrl+g++`, `++shift+r++`, `++enter++`, `++escape++`, `++slash++`,
  `++f2++`. Modifier first. A lone letter keeps its case (`++r++` shows `r`);
  in a chord it is capitalized (`Ctrl + G`).
- Asides: `:::note`, `:::tip`, `:::caution`, `:::danger`, with an optional
  title as `:::caution[Second press acts at once]`, closed by `:::`.
- Links: site URLs with a trailing slash, `/guides/pipelines/#search`. Relative
  `.md` links fail the build.
- Code titles: ` ```toml title="~/.config/tongs/config.toml" `.
- Tabs need an `.mdx` file and
  `import { Tabs, TabItem } from '@astrojs/starlight/components';`.
- `SDLC.md`, `site-plan.md` and `work/` are never published.
