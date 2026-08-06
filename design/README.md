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
