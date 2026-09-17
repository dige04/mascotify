# Landing page

The marketing page for mascotify, hosted at
[hieudinh.dev/tools/mascotify](https://hieudinh.dev/tools/mascotify/).

Two files and two images, no build step — open `index.html` directly, or:

```sh
python3 -m http.server 8799 -d landing
```

## Why it is static

The app itself cannot be hosted. Its free path works by driving *your* signed-in
coding agent, which only exists on your own machine; on a server that route
disappears and every visitor would need an API key. So the page is a page, and
the tool stays local.

## The assets are real output

`assets/mascot.webp` and `assets/sheet.jpg` came out of an actual run — the
animation is the 12-frame loop from `.mascotify/wave/preview.webp`, and the sheet
is the one quoted in [SPIKE.md](../SPIKE.md) as passing validation at 0.5% size
spread. Regenerate them rather than editing by hand:

```sh
mascotify serve                       # make a mascot, then copy its preview.webp
cp .mascotify/<job>/preview.webp landing/assets/mascot.webp
```

## Deploying

The page ships as part of hieudinh.dev via its CLI, which copies this directory
into the site's `public/tools/mascotify/`:

```sh
hieudinh new tool "Mascotify" --dist ./landing ...   # first time
hieudinh deploy                                      # publish
```

Re-running `--dist` re-copies it after a change.
