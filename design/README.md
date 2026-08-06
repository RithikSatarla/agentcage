# design/

Visual reference. **Not source, and not deployed.**

These five pages are self-extracting bundles: React, fonts and every style are
compressed and inlined into each file, which is why they are ~820KB each. They render
client-side, so the static text in the HTML is about 105 characters and everything else
is assembled by script at runtime.

That is why they are kept here rather than served. The site's argument is that its
numbers are checkable, and none of the figures in these files are present in the markup
a reader or a crawler receives without executing JavaScript. `website/` is the
hand-written site that Vercel actually serves, and the doc-consistency tests read it.
Those tests glob `website/*.html` and do not recurse, so nothing here is scanned.

Kept for the layout, the ambient hero, the state-model panel and the booking-page
structure, which were ported into `website/` by hand.

| File | Corresponds to |
|---|---|
| `index.html` | `/` |
| `demo.html` | `/demo` |
| `book.html` | `/book` |
| `how-it-works.html` | folded into `/explained` |
| `install.html` | folded into the README quickstart |
| `og.png` | link-preview image, 1200x630, not yet wired up |

Two things in these files that did **not** come across, deliberately: they link to
`/paper/agentcage.pdf`, which has never existed in this repository (the compiled paper
is `website/agentcage-paper.pdf`), and their booking form posts to Formspree, which
`website/book.html` now does directly.

## vercel.json

This folder is also deployed on its own, as a separate Vercel project whose Root
Directory is `design/`. The repository-root `vercel.json` belongs to the other project
and does not apply here, which is why there is a second one in this folder.

It exists because the bundles and the host disagreed about URL shape. Every internal
link in these pages is file-relative with the extension (`href="demo.html"`), but the
deployment was serving the extensionless form only: `/demo` returned 200 and
`/demo.html` returned 404, so the homepage worked and every nav click failed.

`cleanUrls` is therefore off, so the `.html` paths the pages actually link to resolve.
The rewrites keep the extensionless paths working as well, so any URL already shared
stays alive. Fixing it here rather than in the markup keeps the bundles byte-identical
to what was exported.
